"""Client for TVT NVMS-9000 based DVRs/NVRs (and rebadges such as Hawk Vision).

Clean-room implementation from observed traffic; see docs/tvt-protocol.md.
Only uses the device's HTTP XML API and its media WebSocket. No decoding.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import struct
import time
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import aiohttp

_LOGGER = logging.getLogger(__name__)

_XML_HEAD = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<request version="1.0" systemType="NVMS-9000" clientType="WEB">'
)
_XML_TAIL = "</request>"

# Event names used by queryChlRecLog conditions and by playback type_mask.
REC_TYPES_SEARCH = ("MANUAL", "SCHEDULE", "MOTION", "SENSOR")
REC_TYPES_MASK = ("manual", "schedule", "motion", "sensor")

ERR_PLAY_COMPLETE = 536870961
ERR_NO_RECORD = 536870942
ERR_USER_LOCKED = 536870951
ERR_PARAM = 536870943
ERR_NO_PERMISSION = 536870953
ERR_SESSION_INVALID = 536871011

# Playback flow control: acknowledge every N frames or the device pauses
# after roughly 15 unacknowledged frames.
_ACK_EVERY = 8

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


class TVTError(Exception):
    """Device returned an error status."""

    def __init__(self, cmd: str, code: int | None, text: str = "") -> None:
        super().__init__(f"{cmd} failed: code={code} {text}".strip())
        self.code = code


def channel_guid(channel: int) -> str:
    return f"{{{channel:08X}-0000-0000-0000-000000000000}}"


def filetime_to_datetime(ft: int) -> datetime:
    return _FILETIME_EPOCH + timedelta(microseconds=ft // 10)


@dataclass(slots=True)
class RecordSegment:
    rec_type: str
    start: datetime  # device-local wall clock, naive
    end: datetime
    size: int


@dataclass(slots=True)
class Frame:
    """One video access unit as delivered by the device."""

    time: datetime  # UTC
    index: int  # playback: running frame index; live: ms tick
    keyframe: bool
    codec: str | None  # "HEVC"/"H264" on key frames, else None
    width: int | None
    height: int | None
    data: bytes  # Annex-B elementary stream


def parse_media_message(buf: bytes) -> tuple[dict, bytes]:
    """Split a binary WebSocket message into its JSON envelope and payload."""
    hdr_len = 8 if struct.unpack_from("<I", buf, 0)[0] == 0 else 16
    json_len = struct.unpack_from("<I", buf, 4)[0]
    envelope = json.loads(buf[hdr_len : hdr_len + json_len])
    data = envelope.get("data", {})
    span = data.get("playback_stream_data") or data.get("live_stream_data")
    if not span:
        return envelope, b""
    offset, length = (int(x) for x in span.split(","))
    start = hdr_len + json_len + offset
    return envelope, buf[start : start + length]


def parse_shfl(payload: bytes) -> Frame | None:
    """Parse an SHFL media packet. Returns None for non-video packets."""
    if len(payload) < 68 or payload[:4] != b"SHFL":
        return None
    kind = payload[6]
    if kind not in (2, 7):  # 2 live video, 7 playback video
        return None
    ft, index = struct.unpack_from("<QI", payload, 28)
    es_len = struct.unpack_from("<I", payload, 48)[0]
    keyframe = bool(payload[7])
    codec = width = height = None
    if keyframe and len(payload) - es_len >= 80:
        codec = payload[72:76].decode("ascii", "replace").strip("\0")
        width, height = struct.unpack_from("<HH", payload, 76)
    return Frame(
        time=filetime_to_datetime(ft),
        index=index,
        keyframe=keyframe,
        codec=codec,
        width=width,
        height=height,
        data=payload[len(payload) - es_len :],
    )


class TVTClient:
    """Async client. One instance holds one device session."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 80,
        ssl: bool = False,
    ) -> None:
        self._http = session
        scheme = "https" if ssl else "http"
        self._base = f"{scheme}://{host}:{port}"
        self._ws_base = f"{'wss' if ssl else 'ws'}://{host}:{port}"
        self._username = username
        self._password = password
        self._session_id: str | None = None  # without braces
        self._req_id = 0
        self._xml_lock = asyncio.Lock()
        self._last_xml = 0.0

    # ---- HTTP XML API -------------------------------------------------

    async def _request(self, cmd: str, body: str = "") -> ET.Element:
        async with self._xml_lock:
            try:
                return await self._request_locked(cmd, body)
            except TVTError as err:
                if err.code != ERR_SESSION_INVALID or cmd in ("doLogin", "doLogout"):
                    raise
                self._session_id = None
                await self._login_locked()
                return await self._request_locked(cmd, body)

    async def _request_locked(self, cmd: str, body: str = "") -> ET.Element:
        await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - self._last_xml)))
        self._last_xml = time.monotonic()
        headers = {"Content-Type": "application/xml"}
        if self._session_id:
            headers["Cookie"] = f"sessionId={self._session_id}"
        async with self._http.post(
            f"{self._base}/{cmd}",
            data=(_XML_HEAD + body + _XML_TAIL).encode(),
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            text = await resp.text()
        root = ET.fromstring(text)
        if root.findtext("status") != "success":
            code = root.findtext("errorCode")
            raise TVTError(
                cmd, int(code) if code else None, root.findtext("errorDescription") or ""
            )
        return root

    async def login(self) -> None:
        async with self._xml_lock:
            await self._login_locked()

    async def _login_locked(self) -> None:
        pw = base64.b64encode(self._password.encode()).decode()
        root = await self._request_locked(
            "doLogin",
            "<content><userName><![CDATA[%s]]></userName>"
            "<password><![CDATA[%s]]></password></content>" % (self._username, pw),
        )
        sid = root.findtext("content/sessionId") or ""
        self._session_id = sid.strip("{}")

    async def logout(self) -> None:
        if not self._session_id:
            return
        try:
            await self._request(
                "doLogout",
                "<content><sessionId><![CDATA[{%s}]]></sessionId></content>"
                % self._session_id,
            )
        finally:
            self._session_id = None

    async def record_dates(self, channels: Iterable[int]) -> list[date]:
        channel_ids = "".join(f"<chlId>{channel_guid(channel)}</chlId>" for channel in channels)
        root = await self._request(
            "queryDatesExistRec",
            f"<condition>{channel_ids}</condition>",
        )
        return sorted(
            date.fromisoformat(item.text.strip())
            for item in root.iterfind("content/item")
            if item.text
        )

    async def record_segments(
        self,
        channel: int,
        start: datetime,
        end: datetime,
        rec_types: tuple[str, ...] = REC_TYPES_SEARCH,
    ) -> list[RecordSegment]:
        """Recording segments in device-local time (naive datetimes)."""
        fmt = "%Y-%m-%d %H:%M:%S"
        types = "".join(f"<enum>{t}</enum>" for t in rec_types)
        items = "".join(f"<item>{escape(t)}</item>" for t in rec_types)
        root = await self._request(
            "queryChlRecLog",
            f"<types><recType>{types}</recType></types>"
            "<requireField><chl/><recList><item><recType/><startTime/><endTime/>"
            "<size/></item></recList></requireField>"
            f"<condition><startTime>{start.strftime(fmt)}</startTime>"
            f"<endTime>{end.strftime(fmt)}</endTime>"
            f"<recType type='list'><itemType type='recType'/>{items}</recType>"
            f"<chl id='{channel_guid(channel)}'></chl></condition>",
        )
        segments = []
        for item in root.iter("item"):
            st, et = item.findtext("startTime"), item.findtext("endTime")
            if not (st and et):
                continue
            segments.append(
                RecordSegment(
                    rec_type=(item.findtext("recType") or "").strip(),
                    start=datetime.strptime(st.strip(), fmt),
                    end=datetime.strptime(et.strip(), fmt),
                    size=int(item.findtext("size") or 0),
                )
            )
        return segments

    # ---- Media WebSocket ------------------------------------------------

    def _basic(self) -> dict:
        self._req_id += 1
        return {
            "ver": "1.0",
            "time": int(time.time() * 1000),
            "id": self._req_id,
            "nonce": random.randint(1, 2**31 - 2),
        }

    async def _send(self, ws: aiohttp.ClientWebSocketResponse, url: str, data: dict) -> None:
        await ws.send_str(json.dumps({"url": url, "basic": self._basic(), "data": data}))

    async def playback(
        self,
        channel: int,
        start: datetime,
        end: datetime,
        *,
        stream_index: int = 1,
        type_mask: tuple[str, ...] = REC_TYPES_MASK,
    ) -> AsyncIterator[Frame]:
        """Yield recorded video frames between two aware datetimes.

        The device paces delivery: it waits for acknowledgements, which this
        generator sends as frames are consumed, so a slow consumer slows the
        device instead of buffering unboundedly.
        """
        if not self._session_id:
            raise RuntimeError("not logged in")
        task_id = "{%s}" % uuid.uuid4()
        url = f"{self._ws_base}/requestWebsocketConnection?sessionID={self._session_id}"
        async with self._http.ws_connect(url, max_msg_size=0, heartbeat=None) as ws:
            try:
                async for frame in self._playback_loop(
                    ws, task_id, channel, start, end, stream_index, type_mask
                ):
                    yield frame
            finally:
                if not ws.closed:
                    try:
                        await self._send(ws, "/device/playback/close", {"task_id": task_id})
                        _LOGGER.info("playback close sent")
                    except (aiohttp.ClientError, ConnectionError):
                        pass

    async def _playback_loop(self, ws, task_id, channel, start, end, stream_index, type_mask):
        opened = False
        since_ack = 0
        end_ts = end.timestamp()
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                env = json.loads(msg.data)
                url = env.get("url", "")
                code = env.get("data", {}).get("code", env.get("basic", {}).get("code", 0))
                if url == "/device/create_connection#response":
                    if code:
                        raise TVTError("create_connection", code)
                    await self._send(
                        ws,
                        "/device/playback/open",
                        {
                            "task_id": task_id,
                            "channel_id": channel_guid(channel),
                            "start_time": int(start.timestamp()),
                            "end_time": int(end_ts),
                            "stream_index": stream_index,
                            "type_mask": list(type_mask),
                        },
                    )
                elif url == "/device/playback/open#response":
                    if code:
                        raise TVTError("playback/open", code)
                    opened = True
                elif url == "/device/playback/data" and code:
                    if code == ERR_PLAY_COMPLETE:
                        return
                    raise TVTError("playback/data", code)
                elif code and not url.endswith("refresh_play_index#response"):
                    _LOGGER.debug("device message %s", msg.data)
                continue
            if msg.type != aiohttp.WSMsgType.BINARY:
                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR):
                    return
                continue
            if not opened:
                continue
            _, payload = parse_media_message(msg.data)
            frame = parse_shfl(payload)
            if frame is None:
                continue
            if frame.time.timestamp() >= end_ts:
                return
            yield frame
            since_ack += 1
            if since_ack >= _ACK_EVERY:
                since_ack = 0
                await self._send(
                    ws,
                    "/device/playback/refresh_play_index",
                    {"task_id": task_id, "play_frame_index": frame.index},
                )

    async def __aenter__(self) -> TVTClient:
        await self.login()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.logout()
