"""DVRKit Home Assistant integration."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import hls, live, recordings
from .tvt import TVTClient, TVTError

DOMAIN = "dvrkit"
PLATFORMS = ["camera"]


@dataclass
class DVRRuntime:
    client: TVTClient
    host: str
    port: int
    username: str
    password: str
    timezone: ZoneInfo
    channels: int
    playback_limit: int


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    await hls.async_setup(hass)
    await live.async_setup(hass)
    await recordings.async_setup(hass)
    bundle_dir = Path(__file__).parent / "www" / "acc"
    bundle = bundle_dir / "advanced-camera-card.js"
    if await hass.async_add_executor_job(bundle.is_file):
        await hass.http.async_register_static_paths([
            StaticPathConfig("/dvrkit_static/acc", str(bundle_dir), False)
        ])
        content = await hass.async_add_executor_job(bundle.read_bytes)
        version = hashlib.sha256(content).hexdigest()[:12]
        frontend.add_extra_js_url(
            hass, f"/dvrkit_static/acc/advanced-camera-card.js?v={version}"
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data
    client = TVTClient(async_get_clientsession(hass), data["host"], data["username"], data["password"], port=data["port"])
    try:
        await client.login()
    except (TVTError, OSError, TimeoutError) as err:
        raise ConfigEntryNotReady(str(err)) from err
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.runtime_data = DVRRuntime(client, data["host"], data["port"], data["username"], data["password"], ZoneInfo(data["timezone"]), data["channels"], entry.options.get("playback_limit", 4))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)



async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await live.async_unload_entry(entry)
    await hls.async_unload_entry(entry)
    await entry.runtime_data.client.logout()
    return True
