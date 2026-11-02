"""TVHub: control Android TV / Fire TV, LG webOS and Hisense VIDAA TVs through a TVHub server."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SSL, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TVHubClient, TVHubError
from .const import APPS_TTL, DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.MEDIA_PLAYER]

type TVHubConfigEntry = ConfigEntry["TVHubCoordinator"]


class TVHubCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls TVHub for the state of every TV and keeps each TV's app list."""

    config_entry: TVHubConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TVHubConfigEntry, client: TVHubClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN,
                         update_interval=timedelta(seconds=SCAN_INTERVAL))
        self.client = client
        self.apps: dict[str, list[dict[str, Any]]] = {}
        self._apps_at: dict[str, float] = {}
        self._apps_busy: set[str] = set()

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            tvs = await self.client.tvs()
        except TVHubError as err:
            raise UpdateFailed(f"Cannot reach TVHub: {err}") from err
        data = {tv["id"]: tv for tv in tvs}
        for tv_id, tv in data.items():
            if (tv["caps"].get("apps") and tv["state"] == "device" and tv_id not in self._apps_busy
                    and time.monotonic() - self._apps_at.get(tv_id, -APPS_TTL) >= APPS_TTL):
                self._apps_busy.add(tv_id)
                self.hass.async_create_background_task(self._refresh_apps(tv_id), f"tvhub apps {tv_id}")
        return data

    async def _refresh_apps(self, tv_id: str) -> None:
        try:
            self.apps[tv_id] = await self.client.apps(tv_id)
            self._apps_at[tv_id] = time.monotonic()
            self.async_update_listeners()
        except TVHubError as err:
            _LOGGER.debug("Cannot list apps of %s: %s", tv_id, err)
            self._apps_at[tv_id] = time.monotonic() - APPS_TTL + 30      # retry in 30 s
        finally:
            self._apps_busy.discard(tv_id)


async def async_setup_entry(hass: HomeAssistant, entry: TVHubConfigEntry) -> bool:
    client = TVHubClient(async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT],
                          entry.data.get(CONF_TOKEN, ""), entry.data.get(CONF_SSL, False))
    coordinator = TVHubCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TVHubConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
