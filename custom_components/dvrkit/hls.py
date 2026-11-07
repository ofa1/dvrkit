"""Capability-authenticated sequential HLS playback of DVR recordings.

The EVENT playlist grows as complete GOPs arrive. Seeking uses a new playback
URL; older fragments can be evicted from the bounded memory cache.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError

from .fmp4 import FMP4Muxer, TIMESCALE
from .tvt import ERR_NO_RECORD, ERR_PLAY_COMPLETE, TVTError

_LOGGER = logging.getLogger(__name__)
_DOMAIN_DATA = "dvrkit_hls"
_IDLE_SECONDS = 60
_MAX_WINDOW_SECONDS = 3600
_MAX_FRAGMENT_BYTES = 16 * 1024 * 1024
_MAX_CACHE_BYTES = 64 * 1024 * 1024
_MAX_INIT_BYTES = 4 * 1024 * 1024
_MAX_FRAGMENTS = 20000
_TARGET_DURATION = 10
_PREFETCH = 3
_MANAGERS: dict[str, "_Manager"] = {}

class _CapacityError(HomeAssistantError):
    """All configured DVR playback streams are in use."""

class _LongGOPError(HomeAssistantError):
    """A GOP cannot fit the advertised HLS target duration."""




@dataclass(slots=True)
class _Fragment:
    timestamp: float
    duration: float
    init: int
    gap: bool = False
    discontinuity: bool = False


class _Session:
    def __init__(self, manager, entry, channel, start, end):
        self.manager = manager
        self.entry = entry
        self.channel = channel
        self.start = start
        self.end = end
        self.id = secrets.token_urlsafe(18)
        self.token = secrets.token_urlsafe(32)
        self.touched = time.monotonic()
        self.condition = asyncio.Condition()
        self.closed = False
        self.finished = False
        self.error: Exception | None = None
        self.task: asyncio.Task | None = None
        self.fragments: list[_Fragment] = []
        self.cache: OrderedDict[int, bytes] = OrderedDict()
        self.cache_size = 0
        self.inits: list[bytes] = []
        self.init_size = 0
        self.requested = -1

    def expired(self, now):
        return now - self.touched >= _IDLE_SECONDS

    def start_producer(self):
        if self.task is None:
            self.task = self.manager.hass.async_create_task(self.produce())

    async def produce(self):
        frames = None
        reserved = False
        muxer = None
        pending_time = None
        pending_bytes = 0
        last_time = None
        current_init = 0
        gap_start = None
        last_seen = None
        pending_discontinuity = False
        try:
            self.manager.reserve(self)
            reserved = True
            frames = self.entry.runtime_data.client.playback(
                self.channel, datetime.fromtimestamp(self.start, timezone.utc),
                datetime.fromtimestamp(self.end, timezone.utc),
            )
            async for frame in frames:
                async with self.condition:
                    await self.condition.wait_for(
                        lambda: self.closed or len(self.fragments) <= self.requested + _PREFETCH
                    )
                    if self.closed:
                        return
                timestamp = frame.time.timestamp()
                if timestamp < self.start:
                    continue
                if timestamp >= self.end:
                    break
                if len(frame.data) > _MAX_FRAGMENT_BYTES:
                    raise HomeAssistantError("DVR frame exceeds the HLS buffer limit")
                last_seen = timestamp
                if (muxer is not None and pending_time is not None and last_time is not None
                        and timestamp - last_time > max(2.0, 10 * muxer._last_delta / TIMESCALE)):
                    chunks = muxer.flush()
                    gap_start = min(timestamp, last_time + muxer._last_delta / TIMESCALE)
                    for chunk in chunks:
                        await self.add_fragment(chunk, pending_time, gap_start - pending_time,
                                                current_init, pending_discontinuity)
                    pending_time = None
                    pending_bytes = 0
                if gap_start is not None:
                    if not frame.keyframe:
                        continue
                    await self.add_gaps(gap_start, timestamp)
                    gap_start = None
                    pending_discontinuity = True
                if muxer is None:
                    if not frame.keyframe:
                        continue
                    muxer = FMP4Muxer(frame.width or 0, frame.height or 0)
                    muxer._base_us = int(self.start * 1_000_000)
                if frame.keyframe and frame.width and frame.height:
                    muxer.width, muxer.height = frame.width, frame.height
                old_time = pending_time
                chunks = muxer.push(frame.data, int(timestamp * 1_000_000), frame.keyframe)
                for chunk in chunks:
                    if chunk[4:8] == b"ftyp":
                        if len(self.inits) >= 256 or self.init_size + len(chunk) > _MAX_INIT_BYTES:
                            raise HomeAssistantError("DVR video initialization limit exceeded")
                        self.inits.append(chunk)
                        self.init_size += len(chunk)
                        current_init = len(self.inits)
                    else:
                        if old_time is None:
                            raise HomeAssistantError("DVR returned an invalid media fragment")
                        await self.add_fragment(chunk, old_time, timestamp - old_time,
                                                current_init, pending_discontinuity)
                        pending_discontinuity = False
                        pending_bytes = 0
                if muxer._pending:
                    pending_time = muxer._pending[0].time_us / 1_000_000
                    pending_bytes += len(frame.data)
                    if pending_bytes > _MAX_FRAGMENT_BYTES or len(muxer._pending) > 10000:
                        raise HomeAssistantError("DVR GOP exceeds the HLS buffer limit")
                else:
                    pending_time = None
                    pending_bytes = 0
                last_time = timestamp
        except TVTError as exc:
            if exc.code not in (ERR_PLAY_COMPLETE, ERR_NO_RECORD):
                self.error = exc
                _LOGGER.warning("DVR HLS playback failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
            _LOGGER.warning("DVR HLS playback failed: %s", exc)
        finally:
            try:
                if frames is not None:
                    await frames.aclose()
                if not self.closed and self.error is None and muxer and pending_time is not None:
                    # The last GOP has no next keyframe. Use the muxer's final
                    # sample duration, clipped to the requested end.
                    chunks = muxer.flush()
                    duration = min(self.end, last_time + muxer._last_delta / TIMESCALE) - pending_time
                    for chunk in chunks:
                        await self.add_fragment(chunk, pending_time, duration, current_init,
                                                pending_discontinuity)
                if not self.closed and self.error is None and gap_start is not None and last_seen is not None:
                    await self.add_gaps(gap_start, last_seen)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = exc
                _LOGGER.warning("DVR HLS finalization failed: %s", exc)
            finally:
                if reserved:
                    self.manager.release(self)
                async with self.condition:
                    self.finished = True
                    self.condition.notify_all()

    async def add_fragment(self, data, timestamp, duration, init, discontinuity=False):
        if duration > _TARGET_DURATION:
            raise _LongGOPError("DVR GOP exceeds the 10-second HLS target duration")
        if (not init or duration <= 0 or len(data) > _MAX_FRAGMENT_BYTES
                or len(self.fragments) >= _MAX_FRAGMENTS):
            raise HomeAssistantError("DVR returned an unsupported HLS fragment")
        async with self.condition:
            while self.cache and self.cache_size + len(data) > _MAX_CACHE_BYTES:
                _, old = self.cache.popitem(last=False)
                self.cache_size -= len(old)
            index = len(self.fragments)
            self.cache[index] = data
            self.cache_size += len(data)
            self.fragments.append(_Fragment(timestamp, duration, init, discontinuity=discontinuity))
            self.condition.notify_all()

    async def add_gaps(self, start, end):
        async with self.condition:
            while start < end:
                duration = min(_TARGET_DURATION, end - start)
                if len(self.fragments) >= _MAX_FRAGMENTS:
                    raise HomeAssistantError("DVR HLS fragment limit exceeded")
                self.fragments.append(_Fragment(start, duration, 0, gap=True))
                start += duration
            self.condition.notify_all()

    def playlist(self):
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{_TARGET_DURATION}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:EVENT"]
        last_init = 0
        for index, fragment in enumerate(self.fragments):
            if fragment.discontinuity or (not fragment.gap and last_init and fragment.init != last_init):
                lines.append("#EXT-X-DISCONTINUITY")
            if not fragment.gap and fragment.init != last_init:
                lines.append(f'#EXT-X-MAP:URI="init-{fragment.init}.mp4"')
                last_init = fragment.init
            stamp = datetime.fromtimestamp(fragment.timestamp, timezone.utc).isoformat()
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp}")
            lines.append(f"#EXTINF:{fragment.duration:.6f},")
            if fragment.gap:
                lines.append("#EXT-X-GAP")
            lines.append(f"segment-{index}.m4s")
        if self.finished and self.error is None:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        async with self.condition:
            self.condition.notify_all()
        self.cache.clear()
        self.cache_size = 0
        self.inits.clear()
        self.init_size = 0


class _Manager:
    def __init__(self, hass):
        self.hass = hass
        self.sessions: dict[str, _Session] = {}
        self.active: dict[str, set[str]] = {}
        self.lock = asyncio.Lock()
        self.sweeper = hass.async_create_background_task(self.sweep(), "dvrkit HLS sweeper")

    def reserve(self, session):
        active = self.active.setdefault(session.entry.entry_id, set())
        if len(active) >= session.entry.runtime_data.playback_limit:
            raise _CapacityError("DVR playback stream limit reached")
        active.add(session.id)

    def release(self, session):
        active = self.active.get(session.entry.entry_id)
        if active is not None:
            active.discard(session.id)
            if not active:
                self.active.pop(session.entry.entry_id, None)

    async def sweep(self):
        while True:
            await asyncio.sleep(30)
            async with self.lock:
                expired = [s for s in self.sessions.values() if s.expired(time.monotonic())]
                for session in expired:
                    self.sessions.pop(session.id, None)
                    await session.close()

    async def remove_entry(self, entry_id):
        async with self.lock:
            removed = [s for s in self.sessions.values() if s.entry.entry_id == entry_id]
            for session in removed:
                self.sessions.pop(session.id, None)
                await session.close()

    async def stop(self, _event):
        self.sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await self.sweeper
        async with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            await session.close()
        for entry_id, manager in list(_MANAGERS.items()):
            if manager is self:
                _MANAGERS.pop(entry_id, None)


class _HLSView(HomeAssistantView):
    url = "/api/dvrkit/hls/{session_id}/{token}/{resource}"
    name = "api:dvrkit:hls"
    # Media elements cannot send HA bearer headers. The authenticated URL
    # creator issues an unguessable, short-lived capability for all resources.
    requires_auth = False

    async def get(self, request, session_id, token, resource):
        manager: _Manager = request.app["hass"].data[_DOMAIN_DATA]
        session = manager.sessions.get(session_id)
        if (session is None or not secrets.compare_digest(session.token, token)
                or session.expired(time.monotonic()) or session.closed):
            raise web.HTTPNotFound()
        session.touched = time.monotonic()
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if resource == "index.m3u8":
            session.start_producer()
            async with session.condition:
                await session.condition.wait_for(lambda: bool(session.fragments) or session.finished or session.closed)
                if session.closed:
                    raise web.HTTPGone()
                if isinstance(session.error, _CapacityError):
                    raise web.HTTPServiceUnavailable(reason="DVR playback stream limit reached")
                if isinstance(session.error, _LongGOPError):
                    raise web.HTTPBadGateway(reason="DVR GOP exceeds the 10-second HLS target duration")
                if session.error:
                    raise web.HTTPBadGateway(reason="DVR playback failed")
                if not session.fragments:
                    raise web.HTTPNotFound(reason="No recording found")
                body = session.playlist()
                # GAP segments are intentionally not requested by HLS clients.
                # Let prefetch move through advertised gaps without blocking.
                while (session.requested + 1 < len(session.fragments)
                       and session.fragments[session.requested + 1].gap):
                    session.requested += 1
                session.condition.notify_all()
            return web.Response(text=body, content_type="application/vnd.apple.mpegurl", headers=headers)
        if resource.startswith("segment-") and resource.endswith(".m4s"):
            number = resource[8:-4]
            if not number.isascii() or not number.isdecimal() or len(number) > 5:
                raise web.HTTPNotFound()
            index = int(number)
            async with session.condition:
                if index >= len(session.fragments):
                    raise web.HTTPNotFound()
                if session.fragments[index].gap:
                    return web.Response(status=204, headers=headers)
                data = session.cache.get(index)
                if data is None:
                    raise web.HTTPGone(reason="Fragment evicted; create a new playback URL to seek")
                session.cache.move_to_end(index)
                session.requested = max(session.requested, index)
                session.condition.notify_all()
            return web.Response(body=data, content_type="video/iso.segment", headers=headers)
        if resource.startswith("init-") and resource.endswith(".mp4"):
            number = resource[5:-4]
            if not number.isascii() or not number.isdecimal() or len(number) > 3:
                raise web.HTTPNotFound()
            index = int(number)
            if not 1 <= index <= len(session.inits):
                raise web.HTTPNotFound()
            return web.Response(body=session.inits[index - 1], content_type="video/mp4", headers=headers)
        raise web.HTTPNotFound()


async def async_setup(hass):
    """Register the HLS endpoint once for this Home Assistant instance."""
    if _DOMAIN_DATA in hass.data:
        return
    manager = _Manager(hass)
    hass.data[_DOMAIN_DATA] = manager
    hass.http.register_view(_HLSView)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, manager.stop)


async def async_unload_entry(entry):
    """Close and remove all playback sessions for a config entry."""
    manager = _MANAGERS.pop(entry.entry_id, None)
    if manager is not None:
        await manager.remove_entry(entry.entry_id)


async def _release_url(manager, entry, channel, url):
    """Release a validated session while the manager lock is held."""
    try:
        parsed = urlsplit(url)
        parts = parsed.path.split("/")
        if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or len(parts) != 7
                or parts[:4] != ["", "api", "dvrkit", "hls"] or parts[6] != "index.m3u8"):
            raise ValueError
        old = manager.sessions.get(parts[4])
        if (old is not None and (old.entry.entry_id != entry.entry_id or old.channel != channel
                or not secrets.compare_digest(old.token, parts[5]))):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise HomeAssistantError("Invalid playback URL to replace") from exc
    if old is not None:
        manager.sessions.pop(old.id, None)
        await old.close()


async def async_release_url(hass, entry, channel, replace_url):
    """Release one explicitly identified playback session, if still present."""
    if isinstance(channel, bool) or not isinstance(channel, int) or not 1 <= channel <= entry.runtime_data.channels:
        raise HomeAssistantError("Invalid playback channel")
    manager: _Manager = hass.data[_DOMAIN_DATA]
    async with manager.lock:
        await _release_url(manager, entry, channel, replace_url)


async def async_create_url(hass, entry, channel, start, end, replace_url=None):
    """Create a relative playlist URL, optionally replacing one owned session."""
    try:
        start, end = float(start), float(end)
        if (not math.isfinite(start) or not math.isfinite(end) or end <= start
                or end - start > _MAX_WINDOW_SECONDS):
            raise ValueError
        datetime.fromtimestamp(start, timezone.utc)
        datetime.fromtimestamp(end, timezone.utc)
        if isinstance(channel, bool) or not isinstance(channel, int) or not 1 <= channel <= entry.runtime_data.channels:
            raise ValueError
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise HomeAssistantError("Invalid playback channel or time range") from exc
    manager: _Manager = hass.data[_DOMAIN_DATA]
    async with manager.lock:
        if replace_url is not None:
            await _release_url(manager, entry, channel, replace_url)
        expired = [s for s in manager.sessions.values() if s.expired(time.monotonic())]
        for old in expired:
            manager.sessions.pop(old.id, None)
            await old.close()
        if len(manager.active.get(entry.entry_id, ())) >= entry.runtime_data.playback_limit:
            raise HomeAssistantError("DVR playback stream limit reached")
        session = _Session(manager, entry, channel, start, end)
        manager.sessions[session.id] = session
        _MANAGERS[entry.entry_id] = manager
    return f"/api/dvrkit/hls/{session.id}/{session.token}/index.m3u8"
