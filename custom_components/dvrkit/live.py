"""Loopback-only, capability-authenticated HLS bridge for TVT live preview.

The DVR's substream is remuxed, not decoded or recorded. Each channel has a
bounded in-memory rolling window; consumers must keep requesting it to keep the
preview alive. The URL is a bearer capability and must not be exposed publicly.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
import json
import logging
import secrets
import time
import uuid

import aiohttp
from aiohttp import web
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError

from .fmp4 import FMP4Muxer
from .tvt import TVTError, channel_guid, parse_media_message, parse_shfl

_LOGGER = logging.getLogger(__name__)
_DOMAIN_DATA = "dvrkit_live"
_IDLE_SECONDS = 60
_READ_TIMEOUT = 20
_START_TIMEOUT = 30
_MAX_GOP_SECONDS = 20
_MAX_GOP_BYTES = 8 * 1024 * 1024
_MAX_WINDOW_BYTES = 16 * 1024 * 1024
_MAX_SEGMENTS = 32
_MANAGERS: dict[str, "_Manager"] = {}


@dataclass(slots=True)
class _Segment:
    number: int
    duration: float
    init_number: int
    init: bytes
    data: bytes


class _Session:
    def __init__(self, manager, entry, channel):
        self.manager = manager
        self.entry = entry
        self.channel = channel
        self.token = secrets.token_urlsafe(32)
        self.touched = time.monotonic()
        self.segments = deque()
        self.window_bytes = 0
        self.next_number = 0
        self.changed = asyncio.Event()
        self.closed = False
        self.failed = False
        self.task: asyncio.Task | None = None

    def expired(self, now):
        return now - self.touched >= _IDLE_SECONDS

    def start(self):
        if self.task is None:
            self.task = self.manager.hass.async_create_background_task(
                self._run(), f"dvrkit live channel {self.channel}"
            )

    def _append(self, data, duration, init_number, init):
        if len(data) + len(init) > _MAX_WINDOW_BYTES:
            raise ValueError("live fragment exceeds memory limit")
        segment = _Segment(self.next_number, duration, init_number, init, data)
        self.next_number += 1
        self.segments.append(segment)
        # Count init per segment as an upper bound, even when it is shared.
        self.window_bytes += len(data) + len(init)
        while len(self.segments) > _MAX_SEGMENTS or self.window_bytes > _MAX_WINDOW_BYTES:
            old = self.segments.popleft()
            self.window_bytes -= len(old.data) + len(old.init)
        self.changed.set()

    def playlist(self):
        lines = ["#EXTM3U", "#EXT-X-VERSION:7",
                 f"#EXT-X-TARGETDURATION:{_MAX_GOP_SECONDS}",
                 f"#EXT-X-MEDIA-SEQUENCE:{self.segments[0].number}"]
        current_init = None
        for segment in self.segments:
            if segment.init_number != current_init:
                if current_init is not None:
                    lines.append("#EXT-X-DISCONTINUITY")
                current_init = segment.init_number
                lines.append(f'#EXT-X-MAP:URI="init-{current_init}.mp4"')
            lines.extend((f"#EXTINF:{segment.duration:.6f},", f"segment-{segment.number}.m4s"))
        return "\n".join(lines) + "\n"

    async def _run(self):
        client = self.entry.runtime_data.client
        task_id = "{%s}" % uuid.uuid4()
        muxer = None
        init = None
        init_number = 0
        gop_start = None
        gop_bytes = 0
        last_us = None
        try:
            if not client._session_id:
                raise RuntimeError("DVR session is not logged in")
            url = f"{client._ws_base}/requestWebsocketConnection?sessionID={client._session_id}"
            async with client._http.ws_connect(url, max_msg_size=_MAX_GOP_BYTES, heartbeat=None) as ws:
                try:
                    opened = False
                    while True:
                        msg = await asyncio.wait_for(ws.receive(), timeout=_READ_TIMEOUT)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            env = json.loads(msg.data)
                            command = env.get("url", "")
                            code = env.get("data", {}).get("code", env.get("basic", {}).get("code", 0))
                            if command == "/device/create_connection#response":
                                if code:
                                    raise TVTError("create_connection", code)
                                await client._send(ws, "/device/preview/open", {
                                    "task_id": task_id, "channel_id": channel_guid(self.channel),
                                    "stream_index": 2, "audio": False,
                                })
                            elif command == "/device/preview/open#response":
                                if code:
                                    raise TVTError("preview/open", code)
                                opened = True
                            elif command == "/device/preview/data" and code:
                                raise TVTError("preview/data", code)
                            continue
                        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            raise ConnectionError("DVR preview websocket closed")
                        if msg.type != aiohttp.WSMsgType.BINARY or not opened:
                            continue
                        _, payload = parse_media_message(msg.data)
                        frame = parse_shfl(payload)
                        if frame is None:
                            continue
                        if muxer is None:
                            if not frame.keyframe or not frame.width or not frame.height:
                                continue
                            muxer = FMP4Muxer(frame.width, frame.height)
                        timestamp = int(frame.time.timestamp() * 1_000_000)
                        if last_us is not None:
                            timestamp = max(timestamp, last_us + 1)
                        last_us = timestamp
                        if gop_start is not None and timestamp - gop_start > _MAX_GOP_SECONDS * 1_000_000:
                            raise ValueError("DVR preview GOP exceeds duration limit")
                        if frame.keyframe:
                            gop_bytes = 0
                        gop_bytes += len(frame.data)
                        if gop_bytes > _MAX_GOP_BYTES:
                            raise ValueError("DVR preview GOP exceeds memory limit")
                        for part in muxer.push(frame.data, timestamp, frame.keyframe):
                            if part[4:8] == b"ftyp":
                                init = part
                                init_number += 1
                            elif init is not None and gop_start is not None:
                                self._append(part, max((timestamp - gop_start) / 1_000_000, 0.001), init_number, init)
                        if frame.keyframe and init is not None:
                            gop_start = timestamp
                finally:
                    if not ws.closed:
                        with suppress(aiohttp.ClientError, ConnectionError, asyncio.TimeoutError):
                            await asyncio.wait_for(client._send(ws, "/device/preview/close", {"task_id": task_id}), 2)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("DVR live preview failed for channel %s", self.channel)
            self.failed = True
        finally:
            self.changed.set()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.changed.set()
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self.segments.clear()
        self.window_bytes = 0


class _Manager:
    def __init__(self, hass):
        self.hass = hass
        self.sessions = {}
        self.runner = None
        self.port = None
        self.sweeper = None

    async def start(self):
        app = web.Application()
        app.router.add_get("/{token}/{resource}", self.get)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.sweeper = self.hass.async_create_background_task(self.sweep(), "dvrkit live sweeper")

    async def get(self, request):
        token = request.match_info["token"]
        session = self.sessions.get(token)
        if session is None or session.closed or session.expired(time.monotonic()):
            raise web.HTTPNotFound()
        session.touched = time.monotonic()
        resource = request.match_info["resource"]
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if resource == "index.m3u8":
            session.start()
            async def wait_ready():
                while not session.segments and not session.failed and not session.closed:
                    session.changed.clear()
                    await session.changed.wait()
            try:
                await asyncio.wait_for(wait_ready(), _START_TIMEOUT)
            except asyncio.TimeoutError:
                raise web.HTTPServiceUnavailable(reason="DVR preview is not ready") from None
            if session.failed or session.closed:
                raise web.HTTPBadGateway(reason="DVR preview stopped")
            return web.Response(text=session.playlist(), content_type="application/vnd.apple.mpegurl", headers=headers)
        for segment in session.segments:
            if resource == f"segment-{segment.number}.m4s":
                return web.Response(body=segment.data, content_type="video/mp4", headers=headers)
            if resource == f"init-{segment.init_number}.mp4":
                return web.Response(body=segment.init, content_type="video/mp4", headers=headers)
        raise web.HTTPNotFound()

    async def sweep(self):
        while True:
            await asyncio.sleep(15)
            now = time.monotonic()
            expired = [s for s in self.sessions.values() if s.expired(now)]
            for session in expired:
                self.sessions.pop(session.token, None)
                await session.close()

    async def remove_entry(self, entry_id):
        sessions = [s for s in self.sessions.values() if s.entry.entry_id == entry_id]
        for session in sessions:
            self.sessions.pop(session.token, None)
            await session.close()

    async def stop(self, _event):
        if self.sweeper is not None:
            self.sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await self.sweeper
        sessions = list(self.sessions.values())
        self.sessions.clear()
        for session in sessions:
            await session.close()
        if self.runner is not None:
            await self.runner.cleanup()
        for entry_id, manager in list(_MANAGERS.items()):
            if manager is self:
                _MANAGERS.pop(entry_id, None)


async def async_setup(hass):
    """Start a loopback HTTP endpoint without waiting for any DVR media."""
    if _DOMAIN_DATA in hass.data:
        return
    manager = _Manager(hass)
    await manager.start()
    hass.data[_DOMAIN_DATA] = manager
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, manager.stop)


async def async_unload_entry(entry):
    """Close this entry's preview tasks before logging out of its client."""
    manager = _MANAGERS.pop(entry.entry_id, None)
    if manager is not None:
        await manager.remove_entry(entry.entry_id)


async def async_stream_source(hass, entry, channel):
    """Return an absolute loopback HLS URL suitable for HA's ffmpeg input."""
    if isinstance(channel, bool) or not isinstance(channel, int) or not 1 <= channel <= entry.runtime_data.channels:
        raise HomeAssistantError("Invalid live channel")
    manager = hass.data[_DOMAIN_DATA]
    now = time.monotonic()
    for session in list(manager.sessions.values()):
        if session.entry.entry_id == entry.entry_id and session.channel == channel:
            if not session.closed and not session.failed and not session.expired(now):
                session.touched = now
                return f"http://127.0.0.1:{manager.port}/{session.token}/index.m3u8"
            manager.sessions.pop(session.token, None)
            await session.close()
    session = _Session(manager, entry, channel)
    manager.sessions[session.token] = session
    _MANAGERS[entry.entry_id] = manager
    return f"http://127.0.0.1:{manager.port}/{session.token}/index.m3u8"
