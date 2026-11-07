"""Live source and options regressions (requires Home Assistant)."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

from custom_components.dvrkit.camera import DVRKitCamera
from custom_components.dvrkit.config_flow import DVRKitOptionsFlow

import voluptuous as vol


def entry(host="dvr.example", username="user", password="password", options=None):
    return SimpleNamespace(
        entry_id="entry", title="DVR", options=options or {},
        data={"host": host, "username": username, "password": password},
    )


class LiveTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_rtsp_encodes_credentials_and_brackets_ipv6(self):
        camera = DVRKitCamera(entry(
            host="2001:db8::1", username="name@:/", password="p@ss: /%雪",
            options={"live_transport": "rtsp", "rtsp_port": 8554},
        ), 3)
        with patch("custom_components.dvrkit.camera.live.async_stream_source", new_callable=AsyncMock) as bridge:
            source = await camera.stream_source()
        self.assertEqual(
            source,
            "rtsp://name%40%3A%2F:p%40ss%3A%20%2F%25%E9%9B%AA@[2001:db8::1]:8554/"
            "?chID=3&streamType=sub&linkType=tcp",
        )
        bridge.assert_not_awaited()

    async def test_rtsp_default_port_and_prebracketed_host(self):
        camera = DVRKitCamera(entry(host="[::1]", options={"live_transport": "rtsp"}), 1)
        self.assertEqual(
            await camera.stream_source(),
            "rtsp://user:password@[::1]:554/?chID=1&streamType=sub&linkType=tcp",
        )

    async def test_options_reject_invalid_transport_and_port(self):
        flow = DVRKitOptionsFlow()
        with patch.object(DVRKitOptionsFlow, "config_entry", new_callable=PropertyMock, return_value=entry()):
            form = await flow.async_step_init()
            schema = form["data_schema"]
            for port in (0, 65536):
                with self.subTest(port=port), self.assertRaises(vol.Invalid):
                    schema({"rtsp_port": port})
            with self.assertRaises(vol.Invalid):
                schema({"live_transport": "other"})
