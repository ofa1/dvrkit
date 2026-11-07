"""Camera entities for configured DVR channels."""

from __future__ import annotations

from urllib.parse import quote

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import live
from .const import (
    CONF_LIVE_TRANSPORT,
    CONF_RTSP_PORT,
    DEFAULT_RTSP_PORT,
    LIVE_TRANSPORT_RTSP,
    LIVE_TRANSPORT_WEBSOCKET,
)

DOMAIN = "dvrkit"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add a camera for each configured channel."""
    runtime = entry.runtime_data
    async_add_entities(
        DVRKitCamera(entry, channel)
        for channel in range(1, runtime.channels + 1)
    )


class DVRKitCamera(Camera):
    """A DVR channel exposed through the configured live transport."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, entry: ConfigEntry, channel: int) -> None:
        super().__init__()
        self._entry = entry
        self._channel = channel
        self._attr_unique_id = f"{entry.entry_id}_{channel}"
        self._attr_name = f"Channel {channel}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="TVT",
        )

    async def stream_source(self) -> str | None:
        """Return the configured live source for Home Assistant."""
        options = self._entry.options
        if options.get(CONF_LIVE_TRANSPORT, LIVE_TRANSPORT_WEBSOCKET) == LIVE_TRANSPORT_RTSP:
            data = self._entry.data
            host = data["host"]
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            username = quote(data["username"], safe="")
            password = quote(data["password"], safe="")
            port = options.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)
            return (
                f"rtsp://{username}:{password}@{host}:{port}/"
                f"?chID={self._channel}&streamType=sub&linkType=tcp"
            )
        return await live.async_stream_source(self.hass, self._entry, self._channel)
