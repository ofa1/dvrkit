"""Proof of concept: search a TVT DVR and save a recording as fragmented MP4.

    DVR_PASSWORD=... uv run poc.py --host 192.168.1.24 --user homeassistant \
        --channel 2 --start "2026-11-06 12:00:00" --seconds 30 -o out.mp4
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

sys.path.insert(0, str(Path(__file__).parent / "custom_components" / "dvrkit"))
from fmp4 import FMP4Muxer
from tvt import TVTClient

GAP = 1.0  # seconds between device requests; the remote link is fragile


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--channel", type=int, required=True)
    p.add_argument("--start", required=True, help="device-local 'YYYY-MM-DD HH:MM:SS'")
    p.add_argument("--seconds", type=int, default=30)
    p.add_argument("--tz", default="Asia/Kolkata", help="device time zone")
    p.add_argument("-o", "--output", default="out.mp4")
    a = p.parse_args()

    tz = ZoneInfo(a.tz)
    local_start = datetime.strptime(a.start, "%Y-%m-%d %H:%M:%S")
    start = local_start.replace(tzinfo=tz)
    end = start + timedelta(seconds=a.seconds)

    async with aiohttp.ClientSession() as http:
        async with TVTClient(http, a.host, a.user, os.environ["DVR_PASSWORD"]) as dvr:
            await asyncio.sleep(GAP)
            dates = await dvr.record_dates([a.channel])
            print(f"dates with recordings: {[d.isoformat() for d in dates]}")
            await asyncio.sleep(GAP)
            day = local_start.replace(hour=0, minute=0, second=0)
            segs = await dvr.record_segments(a.channel, day, day.replace(hour=23, minute=59, second=59))
            for s in segs[:10]:
                print(f"  {s.rec_type:9} {s.start} -> {s.end}")
            print(f"  ({len(segs)} segments)")
            await asyncio.sleep(GAP)

            muxer: FMP4Muxer | None = None
            frames = keyframes = 0
            first = last = None
            mux_cpu = 0.0
            t0 = time.monotonic()
            with open(a.output, "wb") as f:
                async for fr in dvr.playback(a.channel, start, end):
                    if muxer is None:
                        if not fr.keyframe:
                            continue
                        muxer = FMP4Muxer(fr.width or 0, fr.height or 0)
                        print(f"first key frame: {fr.codec} {fr.width}x{fr.height} at {fr.time.astimezone(tz)}")
                    c0 = time.process_time()
                    for seg in muxer.push(fr.data, int(fr.time.timestamp() * 1_000_000), fr.keyframe):
                        f.write(seg)
                    mux_cpu += time.process_time() - c0
                    frames += 1
                    keyframes += fr.keyframe
                    first = first or fr.time
                    last = fr.time
                if muxer:
                    for seg in muxer.flush():
                        f.write(seg)
            wall = time.monotonic() - t0

    if not frames:
        print("no frames received")
        return
    span = (last - first).total_seconds()
    print(f"frames={frames} keyframes={keyframes} video_span={span:.1f}s wall={wall:.1f}s")
    print(f"codec={muxer.codec} mux_cpu={mux_cpu * 1000:.0f}ms ({mux_cpu / frames * 1e6:.0f}us/frame)")
    print(f"wrote {a.output} ({os.path.getsize(a.output)} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
