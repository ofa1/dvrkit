"""Authenticated WebSocket API for DVR recordings and playback."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, time as day_time, timedelta, timezone
import logging
import math
from time import monotonic

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.auth.permissions.const import POLICY_READ

from .hls import async_create_url, async_release_url

_LOGGER = logging.getLogger(__name__)
_TTL = 60
_DOMAIN = "dvrkit"
_UTC = timezone.utc


class _Recordings:
    """Cache device searches independently for each loaded config entry."""

    def __init__(self, entry):
        self.entry = entry
        self.runtime_data = entry.runtime_data
        self.lock = asyncio.Lock()
        self.dates = None
        self.segments = {}

    async def get_dates(self):
        cached = self.dates
        if cached is not None and monotonic() - cached[0] < _TTL:
            return cached[1]
        async with self.lock:
            cached = self.dates
            if cached is None or monotonic() - cached[0] >= _TTL:
                channels = range(1, self.entry.runtime_data.channels + 1)
                result = await self.entry.runtime_data.client.record_dates(channels)
                cached = (monotonic(), sorted(set(result)))
                self.dates = cached
            return cached[1]

    def _valid_segments(self, cached, day):
        if cached is None:
            return False
        today = datetime.now(self.entry.runtime_data.timezone).date()
        return ((day < today and cached[2] > day)
                or (day >= today and monotonic() - cached[0] < _TTL))

    async def get_segments_for_days(self, channel, days):
        """Load requested days, combining adjacent cache misses into searches."""
        days = sorted(set(days))
        result = {}
        for day in days:
            cached = self.segments.get((channel, day))
            if self._valid_segments(cached, day):
                result[day] = cached[1]
        if len(result) == len(days):
            return result

        async with self.lock:
            missing = []
            for day in days:
                cached = self.segments.get((channel, day))
                if self._valid_segments(cached, day):
                    result[day] = cached[1]
                else:
                    missing.append(day)

            runs = []
            for day in missing:
                if runs and day - runs[-1][-1] == timedelta(days=1):
                    runs[-1].append(day)
                else:
                    runs.append([day])
            for run in runs:
                start = datetime.combine(run[0], day_time.min)
                end = datetime.combine(run[-1], day_time(23, 59, 59))
                segments = await self.entry.runtime_data.client.record_segments(channel, start, end)
                today = datetime.now(self.entry.runtime_data.timezone).date()
                timestamp = monotonic()
                # Build the entire run before publishing any of its day entries.
                split = {}
                for day in run:
                    day_start = datetime.combine(day, day_time.min)
                    day_end = datetime.combine(day, day_time(23, 59, 59))
                    split[day] = [
                        replace(segment, start=max(segment.start, day_start),
                                end=min(segment.end, day_end))
                        for segment in segments
                        if segment.start < day_end and segment.end > day_start
                    ]
                for day, day_segments in split.items():
                    self.segments[(channel, day)] = (timestamp, day_segments, today)
                    result[day] = day_segments
        return result

    async def get_segments(self, channel, day):
        return (await self.get_segments_for_days(channel, [day]))[day]


def _intervals(segments, tz, start, end):
    """Convert device-local intervals to UTC seconds, clipping to the window."""
    result = []
    for segment in segments:
        seg_start = segment.start.replace(tzinfo=tz).timestamp()
        seg_end = segment.end.replace(tzinfo=tz).timestamp()
        clipped_start, clipped_end = max(start, seg_start), min(end, seg_end)
        if clipped_start < clipped_end:
            result.append((clipped_start, clipped_end, segment.rec_type))
    return sorted(result)


def _hourly(segments, day, tz):
    """Sum the union of recording intervals in each local clock hour."""
    start = datetime.combine(day, day_time.min, tzinfo=tz).timestamp()
    end = datetime.combine(day + timedelta(days=1), day_time.min, tzinfo=tz).timestamp()
    intervals = _intervals(segments, tz, start, end)
    merged = []
    for left, right, _ in intervals:
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    durations = {}
    for left, right in merged:
        while left < right:
            local = datetime.fromtimestamp(left, tz)
            # Advancing in UTC preserves repeated and skipped local hours.
            next_hour = left + 3600 - (local.minute * 60 + local.second + local.microsecond / 1_000_000)
            boundary = min(right, next_hour)
            durations[local.hour] = durations.get(local.hour, 0) + boundary - left
            left = boundary
    return [{"hour": hour, "duration": durations[hour]} for hour in sorted(durations)]


async def _summary(cache, channel, tz):
    calendar_days = await cache.get_dates()
    segments_by_day = await cache.get_segments_for_days(channel, calendar_days)
    days = []
    for day in reversed(calendar_days):
        hours = _hourly(segments_by_day[day], day, tz)
        if hours:
            days.append({"day": day.isoformat(), "hours": hours})
    return {"timezone": tz.key, "days": days}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise vol.Invalid("Expected a finite Unix timestamp")
    return value

def get_cache(hass, entry):
    """Return the entry's shared recording-search cache."""
    caches = hass.data.setdefault("dvrkit_recordings", {})
    cache = caches.get(entry.entry_id)
    if cache is None or cache.entry is not entry or cache.runtime_data is not entry.runtime_data:
        cache = caches[entry.entry_id] = _Recordings(entry)
    return cache


async def async_setup(hass: HomeAssistant) -> None:
    """Register recording commands once for this Home Assistant instance."""
    hass.data.setdefault("dvrkit_recordings", {})

    def resolve(connection, entity_id):
        entity = er.async_get(hass).async_get(entity_id)
        if (entity is None or entity.domain != "camera" or entity.platform != _DOMAIN
                or not entity.config_entry_id):
            raise ValueError("Not a DVRKit camera")
        if not connection.user.permissions.check_entity(entity_id, POLICY_READ):
            raise PermissionError("Access to camera denied")
        entry = hass.config_entries.async_get_entry(entity.config_entry_id)
        if entry is None or entry.domain != _DOMAIN or not hasattr(entry, "runtime_data"):
            raise ValueError("DVRKit entry is not loaded")
        prefix = f"{entry.entry_id}_"
        unique_id = entity.unique_id
        if not unique_id.startswith(prefix) or not unique_id[len(prefix):].isdigit():
            raise ValueError("Invalid DVRKit camera channel")
        channel = int(unique_id[len(prefix):])
        if not 1 <= channel <= entry.runtime_data.channels:
            raise ValueError("Invalid DVRKit camera channel")
        return entity, entry, channel, get_cache(hass, entry)

    async def execute(connection, msg, operation):
        try:
            entity, entry, channel, cache = resolve(connection, msg["entity_id"])
            result = await operation(entity, entry, channel, cache, msg)
        except PermissionError as err:
            connection.send_error(msg["id"], "unauthorized", str(err))
        except (ValueError, OverflowError, OSError) as err:
            connection.send_error(msg["id"], "invalid_format", str(err))
        except HomeAssistantError as err:
            connection.send_error(msg["id"], "home_assistant_error", str(err))
        except Exception:
            _LOGGER.exception("DVRKit recording request failed")
            connection.send_error(msg["id"], "unknown_error", "DVR request failed")
        else:
            connection.send_result(msg["id"], result)

    async def info(entity, entry, channel, cache, msg):
        state = hass.states.get(entity.entity_id)
        return {"entry_id": entry.entry_id, "channel": channel,
                "name": (state.name if state else entity.name or entity.original_name or entity.entity_id),
                "timezone": entry.runtime_data.timezone.key}

    async def summary(entity, entry, channel, cache, msg):
        return await _summary(cache, channel, entry.runtime_data.timezone)

    async def get(entity, entry, channel, cache, msg):
        after, before = msg["after"], msg["before"]
        if after >= before:
            raise ValueError("after must be before before")
        tz = entry.runtime_data.timezone
        first = datetime.fromtimestamp(after, tz).date()
        last = datetime.fromtimestamp(before, tz).date()
        days = [day for day in await cache.get_dates()
                if first <= day <= last
                and datetime.combine(day, day_time.min, tzinfo=tz).timestamp() < before]
        segments_by_day = await cache.get_segments_for_days(channel, days)
        intervals = []
        for day in days:
            intervals.extend(_intervals(segments_by_day[day], tz, after, before))
        return [{"start_time": start, "end_time": end, "type": rec_type}
                for start, end, rec_type in sorted(intervals)]

    async def playback(entity, entry, channel, cache, msg):
        if msg.get("release"):
            if "replace_url" not in msg:
                raise ValueError("replace_url is required to release playback")
            await async_release_url(hass, entry, channel, msg["replace_url"])
            return {"released": True}
        if "start" not in msg or "end" not in msg:
            raise ValueError("start and end are required for playback")
        start, end = msg["start"], msg["end"]
        if start >= end or end - start > 3600:
            raise ValueError("Playback window must be positive and at most one hour")
        # Reject timestamps outside the datetime range before passing them to HLS.
        datetime.fromtimestamp(start, _UTC)
        datetime.fromtimestamp(end, _UTC)
        return {"url": await async_create_url(hass, entry, channel, start, end, msg.get("replace_url"))}

    @websocket_api.websocket_command({vol.Required("type"): "dvrkit/camera/info", vol.Required("entity_id"): str})
    @websocket_api.async_response
    async def camera_info(hass, connection, msg):
        await execute(connection, msg, info)

    @websocket_api.websocket_command({vol.Required("type"): "dvrkit/recordings/summary", vol.Required("entity_id"): str})
    @websocket_api.async_response
    async def recordings_summary(hass, connection, msg):
        await execute(connection, msg, summary)

    @websocket_api.websocket_command({vol.Required("type"): "dvrkit/recordings/get", vol.Required("entity_id"): str,
                                      vol.Required("after"): _number, vol.Required("before"): _number})
    @websocket_api.async_response
    async def recordings_get(hass, connection, msg):
        await execute(connection, msg, get)

    @websocket_api.websocket_command({vol.Required("type"): "dvrkit/playback/url", vol.Required("entity_id"): str,
                                      vol.Optional("start"): _number, vol.Optional("end"): _number,
                                      vol.Optional("replace_url"): str, vol.Optional("release"): bool})
    @websocket_api.async_response
    async def playback_url(hass, connection, msg):
        await execute(connection, msg, playback)

    for handler in (camera_info, recordings_summary, recordings_get, playback_url):
        websocket_api.async_register_command(hass, handler)
