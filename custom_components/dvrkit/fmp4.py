"""Minimal fragmented-MP4 muxer for H.264/H.265 Annex-B access units.

Repackages bytes only (no decoding), so it is cheap enough to run on a
Raspberry Pi. Output is playable by <video> via MSE or as a plain file.
Assumes no B-frames (decode order == presentation order), which holds for
the DVR streams observed so far.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass, field

TIMESCALE = 90_000
_TRACK_ID = 1

# H.265 NAL unit types
_HEVC_VPS, _HEVC_SPS, _HEVC_PPS, _HEVC_AUD = 32, 33, 34, 35
# H.264 NAL unit types
_AVC_SPS, _AVC_PPS, _AVC_AUD = 7, 8, 9


def split_annexb(data: bytes) -> list[bytes]:
    """Split an Annex-B byte stream into NAL units (start codes removed)."""
    nals = []
    pos = data.find(b"\0\0\1")
    while pos >= 0:
        start = pos + 3
        nxt = data.find(b"\0\0\1", start)
        if nxt < 0:
            nal = data[start:]
        else:
            # A 4-byte start code leaves one extra zero before 00 00 01.
            nal = data[start : nxt - 1 if data[nxt - 1] == 0 else nxt]
        if nal:
            nals.append(nal)
        pos = nxt
    return nals


def _rbsp(nal: bytes, header_len: int) -> bytes:
    """Strip the NAL header and emulation-prevention bytes."""
    out = bytearray()
    zeros = 0
    for b in nal[header_len:]:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


class _Bits:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
        return (1 << zeros) - 1 + self.u(zeros)


def _box(kind: bytes, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I", 8 + len(body)) + kind + body


def _full_box(kind: bytes, version: int, flags: int, *parts: bytes) -> bytes:
    return _box(kind, struct.pack(">I", (version << 24) | flags), *parts)


def _hvcc(vps: bytes, sps: bytes, pps: bytes) -> tuple[bytes, str]:
    """Build an hvcC box body and the RFC 6381 codec string."""
    r = _rbsp(sps, 2)
    b = _Bits(r)
    b.u(4)  # sps_video_parameter_set_id
    max_sub_layers_minus1 = b.u(3)
    temporal_id_nested = b.u(1)
    ptl = r[1:13]  # general profile_tier_level, 12 bytes
    b.pos = 8 + 96
    # Skip sub-layer profile/level info.
    sub_profile = []
    for _ in range(max_sub_layers_minus1):
        sub_profile.append((b.u(1), b.u(1)))
    if max_sub_layers_minus1 > 0:
        for _ in range(max_sub_layers_minus1, 8):
            b.u(2)
    for profile_present, level_present in sub_profile:
        if profile_present:
            b.u(88)
        if level_present:
            b.u(8)
    b.ue()  # sps_seq_parameter_set_id
    chroma = b.ue()
    if chroma == 3:
        b.u(1)
    b.ue()  # pic_width_in_luma_samples
    b.ue()  # pic_height_in_luma_samples
    if b.u(1):  # conformance_window_flag
        for _ in range(4):
            b.ue()
    depth_luma = b.ue()
    depth_chroma = b.ue()

    profile_space = ptl[0] >> 6
    tier = (ptl[0] >> 5) & 1
    profile_idc = ptl[0] & 0x1F
    compat = struct.unpack(">I", ptl[1:5])[0]
    level = ptl[11]

    body = bytes([1]) + ptl[0:12]
    body += struct.pack(
        ">HBBBBHB",
        0xF000,  # reserved + min_spatial_segmentation_idc
        0xFC,  # reserved + parallelismType
        0xFC | chroma,
        0xF8 | depth_luma,
        0xF8 | depth_chroma,
        0,  # avgFrameRate
        ((max_sub_layers_minus1 + 1) << 3) | (temporal_id_nested << 2) | 3,
    )
    arrays = [(_HEVC_VPS, vps), (_HEVC_SPS, sps), (_HEVC_PPS, pps)]
    body += bytes([len(arrays)])
    for nal_type, nal in arrays:
        body += struct.pack(">BHH", 0x80 | nal_type, 1, len(nal)) + nal

    # RFC 6381 / ISO 14496-15 Annex E codec string, e.g. hvc1.1.6.L120.90
    rev = int(f"{compat:032b}"[::-1], 2)
    space = "" if profile_space == 0 else "ABC"[profile_space - 1]
    constraints = ptl[5:11].rstrip(b"\0")
    codec = f"hvc1.{space}{profile_idc}.{rev:X}.{'H' if tier else 'L'}{level}"
    if constraints:
        codec += "." + ".".join(f"{c:X}" for c in constraints)
    return body, codec


def _avcc(sps: bytes, pps: bytes) -> tuple[bytes, str]:
    body = bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1])
    body += struct.pack(">H", len(sps)) + sps + bytes([1]) + struct.pack(">H", len(pps)) + pps
    return body, f"avc1.{sps[1]:02X}{sps[2]:02X}{sps[3]:02X}"


@dataclass(slots=True)
class Sample:
    time_us: int  # absolute presentation time, microseconds
    keyframe: bool
    data: bytes  # length-prefixed NAL units


@dataclass
class FMP4Muxer:
    """Turn Annex-B access units into an init segment plus media fragments."""

    width: int
    height: int
    codec: str = ""  # RFC 6381 string, set once parameter sets are seen
    _params: dict[int, bytes] = field(default_factory=dict)
    _hevc: bool | None = None
    _seq: int = 0
    _base_us: int | None = None
    _pending: list[Sample] = field(default_factory=list)
    _last_delta: int = TIMESCALE // 25

    def push(self, annexb: bytes, time_us: int, keyframe: bool) -> list[bytes]:
        """Add one access unit. Returns segments ready to emit (init and/or moof+mdat)."""
        out: list[bytes] = []
        nals = split_annexb(annexb)
        if self._hevc is None:
            self._hevc = any(((n[0] >> 1) & 0x3F) == _HEVC_VPS for n in nals)
        payload = bytearray()
        new_params: dict[int, bytes] = {}
        for nal in nals:
            t = ((nal[0] >> 1) & 0x3F) if self._hevc else (nal[0] & 0x1F)
            if self._hevc and t in (_HEVC_VPS, _HEVC_SPS, _HEVC_PPS):
                new_params[t] = nal
            elif not self._hevc and t in (_AVC_SPS, _AVC_PPS):
                new_params[t] = nal
            elif t in ((_HEVC_AUD,) if self._hevc else (_AVC_AUD,)):
                continue
            else:
                payload += struct.pack(">I", len(nal)) + nal
        if keyframe and new_params:
            params = self._params | new_params
            required = ((_HEVC_VPS, _HEVC_SPS, _HEVC_PPS) if self._hevc
                        else (_AVC_SPS, _AVC_PPS))
            if all(t in params for t in required) and params != self._params:
                # New or changed parameter sets: close the running fragment, re-init.
                out += self.flush()
                self._params = params
                out.append(self._init_segment())
        if not self._params:
            return out  # wait for the first key frame
        if keyframe and self._pending:
            out += self.flush()
        if self._base_us is None:
            self._base_us = time_us
        self._pending.append(Sample(time_us, keyframe, bytes(payload)))
        return out

    def flush(self) -> list[bytes]:
        """Emit buffered samples as one moof+mdat fragment."""
        if not self._pending:
            return []
        samples, self._pending = self._pending, []
        return [self._fragment(samples)]

    def _ticks(self, us: int) -> int:
        return (us - (self._base_us or 0)) * TIMESCALE // 1_000_000

    def _init_segment(self) -> bytes:
        if self._hevc:
            cfg, self.codec = _hvcc(
                self._params[_HEVC_VPS], self._params[_HEVC_SPS], self._params[_HEVC_PPS]
            )
            entry_kind, cfg_box = b"hvc1", _box(b"hvcC", cfg)
        else:
            cfg, self.codec = _avcc(self._params[_AVC_SPS], self._params[_AVC_PPS])
            entry_kind, cfg_box = b"avc1", _box(b"avcC", cfg)
        w, h = self.width, self.height
        matrix = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
        sample_entry = _box(
            entry_kind,
            b"\0" * 6 + struct.pack(">H", 1),  # reserved, data_reference_index
            b"\0" * 16,  # pre_defined/reserved
            struct.pack(">HHIIIH", w, h, 0x480000, 0x480000, 0, 1),
            b"\0" * 32,  # compressorname
            struct.pack(">Hh", 0x18, -1),
            cfg_box,
        )
        stbl = _box(
            b"stbl",
            _full_box(b"stsd", 0, 0, struct.pack(">I", 1), sample_entry),
            _full_box(b"stts", 0, 0, struct.pack(">I", 0)),
            _full_box(b"stsc", 0, 0, struct.pack(">I", 0)),
            _full_box(b"stsz", 0, 0, struct.pack(">II", 0, 0)),
            _full_box(b"stco", 0, 0, struct.pack(">I", 0)),
        )
        minf = _box(
            b"minf",
            _full_box(b"vmhd", 0, 1, b"\0" * 8),
            _box(b"dinf", _full_box(b"dref", 0, 0, struct.pack(">I", 1),
                                    _full_box(b"url ", 0, 1))),
            stbl,
        )
        mdia = _box(
            b"mdia",
            _full_box(b"mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, TIMESCALE, 0, 0x55C4, 0)),
            _full_box(b"hdlr", 0, 0, struct.pack(">I4s12x", 0, b"vide"), b"video\0"),
            minf,
        )
        tkhd = _full_box(
            b"tkhd", 0, 3,
            struct.pack(">IIIII", 0, 0, _TRACK_ID, 0, 0),
            b"\0" * 8, struct.pack(">hhHH", 0, 0, 0, 0), matrix,
            struct.pack(">II", w << 16, h << 16),
        )
        mvhd = _full_box(
            b"mvhd", 0, 0,
            struct.pack(">IIII", 0, 0, 1000, 0),
            struct.pack(">IH10x", 0x10000, 0x0100), matrix, b"\0" * 24,
            struct.pack(">I", _TRACK_ID + 1),
        )
        mvex = _box(b"mvex", _full_box(b"trex", 0, 0, struct.pack(">IIIII", _TRACK_ID, 1, 0, 0, 0)))
        ftyp = _box(b"ftyp", b"iso5", struct.pack(">I", 512), b"iso5iso6mp41")
        return ftyp + _box(b"moov", mvhd, _box(b"trak", tkhd, mdia), mvex)

    def _fragment(self, samples: list[Sample]) -> bytes:
        self._seq += 1
        durations = []
        for cur, nxt in zip(samples, samples[1:]):
            d = self._ticks(nxt.time_us) - self._ticks(cur.time_us)
            durations.append(d if d > 0 else self._last_delta)
        if durations:
            self._last_delta = durations[-1]
        durations.append(self._last_delta)

        trun_entries = b"".join(
            struct.pack(">III", d, len(s.data), 0x02000000 if s.keyframe else 0x01010000)
            for s, d in zip(samples, durations)
        )
        # trun flags: data-offset, sample-duration, sample-size, sample-flags
        def build(data_offset: int) -> bytes:
            trun = _full_box(b"trun", 0, 0x000701,
                             struct.pack(">Ii", len(samples), data_offset), trun_entries)
            traf = _box(
                b"traf",
                _full_box(b"tfhd", 0, 0x020000, struct.pack(">I", _TRACK_ID)),
                _full_box(b"tfdt", 1, 0, struct.pack(">Q", self._ticks(samples[0].time_us))),
                trun,
            )
            return _box(b"moof", _full_box(b"mfhd", 0, 0, struct.pack(">I", self._seq)), traf)

        moof = build(0)
        moof = build(len(moof) + 8)
        mdat = _box(b"mdat", *(s.data for s in samples))
        return moof + mdat


def mux_all(frames: Iterable[tuple[bytes, int, bool]], width: int, height: int) -> tuple[bytes, str]:
    """Convenience: mux (annexb, time_us, keyframe) tuples into one fMP4 file."""
    m = FMP4Muxer(width, height)
    out = bytearray()
    for data, t, key in frames:
        for seg in m.push(data, t, key):
            out += seg
    for seg in m.flush():
        out += seg
    return bytes(out), m.codec
