"""Recording calendar and cache regressions (requires Home Assistant)."""
import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
import unittest
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from custom_components.dvrkit.recordings import _Recordings, _summary
from custom_components.dvrkit.tvt import RecordSegment, TVTClient, channel_guid


class CalendarClient:
    def __init__(self):
        self.date_calls = []
        self.segment_calls = []
        self.block_channel = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def record_dates(self, channels):
        self.date_calls.append(tuple(channels))
        return [date(2020, 1, 2), date(2020, 1, 1)]

    async def record_segments(self, channel, start, end):
        self.segment_calls.append((channel, start, end))
        if channel == self.block_channel:
            self.started.set()
            await self.release.wait()
        if channel == 1 and start.date() == date(2020, 1, 1):
            return [RecordSegment("MOTION", datetime(2020, 1, 1, 12),
                                  datetime(2020, 1, 1, 12, 30), 1)]
        return []


class RecordingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = CalendarClient()
        self.tz = ZoneInfo("UTC")
        runtime = SimpleNamespace(client=self.client, channels=4, timezone=self.tz)
        self.cache = _Recordings(SimpleNamespace(runtime_data=runtime))

    async def test_concurrent_summaries_share_calendar_and_use_camera_segments(self):
        summaries = await asyncio.gather(*(
            _summary(self.cache, channel, self.tz) for channel in range(1, 5)
        ))
        self.assertEqual(self.client.date_calls, [(1, 2, 3, 4)])
        self.assertEqual(summaries[0]["days"], [
            {"day": "2020-01-01", "hours": [{"hour": 12, "duration": 1800}]}
        ])
        self.assertTrue(all(summary["days"] == [] for summary in summaries[1:]))
        self.assertEqual(self.client.segment_calls, [
            (channel, datetime(2020, 1, 1), datetime(2020, 1, 2, 23, 59, 59))
            for channel in range(1, 5)
        ])

    async def test_cached_dates_and_segments_bypass_unrelated_miss(self):
        day = date(2020, 1, 1)
        dates = await self.cache.get_dates()
        segments = await self.cache.get_segments(1, day)
        self.client.block_channel = 2
        miss = asyncio.create_task(self.cache.get_segments(2, day))
        await self.client.started.wait()
        try:
            self.assertIs(await asyncio.wait_for(self.cache.get_dates(), 1), dates)
            self.assertIs(await asyncio.wait_for(self.cache.get_segments(1, day), 1), segments)
        finally:
            self.client.release.set()
            await miss

    async def test_range_splits_cross_midnight_and_isolates_cameras(self):
        class Client(CalendarClient):
            async def record_segments(self, channel, start, end):
                self.segment_calls.append((channel, start, end))
                return [RecordSegment("MOTION" if channel == 1 else "MANUAL",
                                      datetime(2020, 1, 1, 23, 30),
                                      datetime(2020, 1, 2, 0, 30), channel)]

        self.cache.entry.runtime_data.client = client = Client()
        first, second = date(2020, 1, 1), date(2020, 1, 2)
        one = await self.cache.get_segments_for_days(1, [second, first, first])
        two = await self.cache.get_segments_for_days(2, [first, second])
        self.assertEqual(len(client.segment_calls), 2)
        self.assertEqual(one[first][0], RecordSegment(
            "MOTION", datetime(2020, 1, 1, 23, 30), datetime(2020, 1, 1, 23, 59, 59), 1))
        self.assertEqual(one[second][0], RecordSegment(
            "MOTION", datetime(2020, 1, 2), datetime(2020, 1, 2, 0, 30), 1))
        self.assertEqual(two[first][0].rec_type, "MANUAL")
        self.assertEqual((await _summary(self.cache, 1, self.tz))["days"], [
            {"day": "2020-01-02", "hours": [{"hour": 0, "duration": 1800}]},
            {"day": "2020-01-01", "hours": [{"hour": 23, "duration": 1799}]},
        ])

    async def test_stale_today_keeps_historic_cache(self):
        today = datetime.now(self.tz).date()
        yesterday = today - timedelta(days=1)
        await self.cache.get_segments_for_days(1, [yesterday, today])
        key = (1, today)
        cached = self.cache.segments[key]
        self.cache.segments[key] = (cached[0] - 61, cached[1], cached[2])
        await self.cache.get_segments_for_days(1, [yesterday, today])
        self.assertEqual(self.client.segment_calls[-1], (
            1, datetime.combine(today, datetime.min.time()),
            datetime.combine(today, datetime.max.time()).replace(hour=23, minute=59, second=59, microsecond=0)))
        self.assertEqual(len(self.client.segment_calls), 2)

    async def test_gaps_and_cached_days_are_not_queried(self):
        days = [date(2020, 1, day) for day in (1, 2, 3, 5, 6)]
        await self.cache.get_segments_for_days(1, [days[1]])
        self.client.segment_calls.clear()
        await self.cache.get_segments_for_days(1, days)
        self.assertEqual(self.client.segment_calls, [
            (1, datetime(2020, 1, 1), datetime(2020, 1, 1, 23, 59, 59)),
            (1, datetime(2020, 1, 3), datetime(2020, 1, 3, 23, 59, 59)),
            (1, datetime(2020, 1, 5), datetime(2020, 1, 6, 23, 59, 59)),
        ])

    async def test_failed_range_does_not_cache_days(self):
        async def fail(channel, start, end):
            raise OSError("search failed")

        self.client.record_segments = fail
        days = [date(2020, 1, 1), date(2020, 1, 2)]
        with self.assertRaises(OSError):
            await self.cache.get_segments_for_days(1, days)
        self.assertFalse(self.cache.segments)

    async def test_calendar_request_repeats_channel_ids(self):
        class Client(TVTClient):
            async def _request(self, command, body):
                self.command, self.body = command, body
                return ET.fromstring("<response><content><item>2020-01-01</item></content></response>")

        client = Client(None, "example", "user", "password")
        self.assertEqual(await client.record_dates(range(1, 5)), [date(2020, 1, 1)])
        self.assertEqual(client.command, "queryDatesExistRec")
        self.assertEqual([item.text for item in ET.fromstring(client.body)],
                         [channel_guid(channel) for channel in range(1, 5)])
