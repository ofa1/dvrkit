"""Configuration flow for TVT DVRs."""

from __future__ import annotations

import asyncio
import logging
from xml.etree.ElementTree import ParseError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .tvt import TVTClient, TVTError

_LOGGER = logging.getLogger(__name__)

DOMAIN = "dvrkit"
CONF_TIMEZONE = "timezone"
CONF_CHANNELS = "channels"
CONF_PLAYBACK_LIMIT = "playback_limit"


def _schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): vol.All(
                str, vol.Strip, vol.Length(min=1)
            ),
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, 80)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): vol.All(
                str, vol.Strip, vol.Length(min=1)
            ),
            vol.Required(CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(CONF_TIMEZONE, default=defaults.get(CONF_TIMEZONE, "UTC")): vol.All(
                str, vol.Strip, vol.Length(min=1)
            ),
        }
    )


class DVRKitConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up one configuration entry per DVR."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        """Allow tuning concurrent playback independently of connection settings."""
        return DVRKitOptionsFlow()

    async def async_step_user(self, user_input: dict | None = None):
        errors = {}
        if user_input is not None:
            try:
                ZoneInfo(user_input[CONF_TIMEZONE])
            except (ZoneInfoNotFoundError, ValueError):
                errors[CONF_TIMEZONE] = "invalid_timezone"
            else:
                await self.async_set_unique_id(
                    f"{user_input[CONF_HOST].lower()}:{user_input[CONF_PORT]}"
                )
                self._abort_if_unique_id_configured()
                client = TVTClient(
                    async_get_clientsession(self.hass),
                    user_input[CONF_HOST],
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    port=user_input[CONF_PORT],
                )
                try:
                    async with asyncio.timeout(15):
                        await client.login()
                except TVTError as err:
                    _LOGGER.debug("DVR rejected the configuration: %s", err)
                    errors["base"] = "login_failed"
                except (aiohttp.ClientError, TimeoutError, OSError):
                    errors["base"] = "cannot_connect"
                except (ParseError, ValueError):
                    _LOGGER.debug("DVR returned an invalid response", exc_info=True)
                    errors["base"] = "invalid_response"
                else:
                    self._channels = 8
                    try:
                        async with asyncio.timeout(5):
                            caps = await client._request("querySystemCaps")
                        channels = int(caps.findtext("content/analogChlCount") or "")
                        if channels > 0:
                            self._channels = channels
                    except (TVTError, aiohttp.ClientError, TimeoutError, OSError, ParseError, ValueError):
                        _LOGGER.debug("Could not discover DVR channel count", exc_info=True)
                    finally:
                        try:
                            async with asyncio.timeout(15):
                                await client.logout()
                        except (TVTError, aiohttp.ClientError, TimeoutError, OSError, ParseError, ValueError):
                            _LOGGER.debug("Could not log out after DVR discovery", exc_info=True)
                    self._connection = user_input
                    return await self.async_step_channels()

        defaults = {CONF_TIMEZONE: self.hass.config.time_zone or "UTC"}
        if user_input:
            defaults.update(user_input)
        return self.async_show_form(
            step_id="user", data_schema=_schema(defaults), errors=errors
        )

    async def async_step_channels(self, user_input: dict | None = None):
        """Confirm the discovered channel count."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._connection[CONF_HOST],
                data={**self._connection, **user_input},
            )
        return self.async_show_form(
            step_id="channels",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CHANNELS, default=self._channels): vol.All(
                        vol.Coerce(int), vol.Range(min=1)
                    )
                }
            ),
        )


class DVRKitOptionsFlow(config_entries.OptionsFlow):
    """Configure playback concurrency."""

    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PLAYBACK_LIMIT,
                        default=self.config_entry.options.get(CONF_PLAYBACK_LIMIT, 4),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1))
                }
            ),
        )
