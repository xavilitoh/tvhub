"""One media_player per TV known to TVHub."""
from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TVHubConfigEntry, TVHubCoordinator
from .api import TVHubError
from .const import DOMAIN

MAKER = {"android": "Android TV / Fire TV", "vidaa": "Hisense", "webos": "LG"}
BASE = (MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE | MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.PREVIOUS_TRACK | MediaPlayerEntityFeature.NEXT_TRACK)


async def async_setup_entry(hass: HomeAssistant, entry: TVHubConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [tv_id for tv_id in coordinator.data if tv_id not in known]
        if new:
            known.update(new)
            async_add_entities(TVHubPlayer(coordinator, tv_id) for tv_id in new)

    entry.async_on_unload(coordinator.async_add_listener(_add_new))
    _add_new()


class TVHubPlayer(CoordinatorEntity[TVHubCoordinator], MediaPlayerEntity):
    _attr_has_entity_name = True
    _attr_name = None                      # the entity is the device itself
    _attr_device_class = MediaPlayerDeviceClass.TV

    def __init__(self, coordinator: TVHubCoordinator, tv_id: str) -> None:
        super().__init__(coordinator)
        self._tv_id = tv_id
        self._attr_unique_id = f"tvhub_{tv_id}_media_player"
        tv = self._tv or {}
        # The second identifier is what the MQTT bridge uses, so both integrations share ONE device.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id), ("mqtt", f"tvhub_{tv_id}")},
            name=tv.get("name"), manufacturer=MAKER.get(tv.get("type", ""), "TV"), model=tv.get("model") or None)

    # ---- helpers
    @property
    def _tv(self) -> dict[str, Any] | None:
        return self.coordinator.data.get(self._tv_id) if self.coordinator.data else None

    @property
    def _vol_tv(self) -> dict[str, Any] | None:
        """The TV whose volume we really control (a Fire Stick can hand it to the TV it is plugged into)."""
        tv = self._tv
        if not tv:
            return None
        return self.coordinator.data.get(tv.get("volume_via") or "") or tv

    async def _do(self, coro) -> None:
        try:
            await coro
        except TVHubError as err:
            raise HomeAssistantError(f"TVHub: {err}") from err
        await self.coordinator.async_request_refresh()

    # ---- state
    @property
    def available(self) -> bool:
        tv = self._tv
        return super().available and tv is not None and tv["state"] in ("device", "disconnected")

    @property
    def state(self) -> MediaPlayerState | None:
        tv = self._tv
        if not tv:
            return None
        if tv["state"] != "device" or tv.get("on") is False:
            return MediaPlayerState.OFF
        return MediaPlayerState.ON

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        tv, vt = self._tv, self._vol_tv
        feats = BASE
        if tv and tv["caps"].get("apps"):
            feats |= MediaPlayerEntityFeature.SELECT_SOURCE
        if vt and vt["caps"].get("volume_set"):
            feats |= MediaPlayerEntityFeature.VOLUME_SET
        return feats

    @property
    def volume_level(self) -> float | None:
        vt = self._vol_tv
        v = vt.get("volume") if vt else None
        return None if v is None else max(0.0, min(1.0, v / 100))

    @property
    def is_volume_muted(self) -> bool | None:
        vt = self._vol_tv
        return bool(vt.get("muted")) if vt and vt.get("volume") is not None else None

    @property
    def app_name(self) -> str | None:
        tv = self._tv
        return (tv.get("app") or None) if tv and self.state == MediaPlayerState.ON else None

    @property
    def app_id(self) -> str | None:
        tv = self._tv
        return (tv.get("focus") or None) if tv and self.state == MediaPlayerState.ON else None

    @property
    def source_list(self) -> list[str] | None:
        apps = self.coordinator.apps.get(self._tv_id)
        return [a["name"] for a in apps] if apps else None

    @property
    def source(self) -> str | None:
        name = self.app_name
        return name if name and self.source_list and name in self.source_list else None

    # ---- commands
    async def async_turn_on(self) -> None:
        await self._do(self.coordinator.client.power(self._tv_id, True))

    async def async_turn_off(self) -> None:
        await self._do(self.coordinator.client.power(self._tv_id, False))

    async def async_volume_up(self) -> None:
        await self._do(self.coordinator.client.key(self._tv_id, "VOLUME_UP"))

    async def async_volume_down(self) -> None:
        await self._do(self.coordinator.client.key(self._tv_id, "VOLUME_DOWN"))

    async def async_set_volume_level(self, volume: float) -> None:
        await self._do(self.coordinator.client.volume(self._tv_id, round(volume * 100)))

    async def async_mute_volume(self, mute: bool) -> None:
        if self.is_volume_muted is not None and self.is_volume_muted == mute:
            return
        await self._do(self.coordinator.client.key(self._tv_id, "VOLUME_MUTE"))      # TVs only offer a toggle

    async def async_media_play(self) -> None:
        await self._do(self.coordinator.client.key(self._tv_id, "MEDIA_PLAY"))

    async def async_media_pause(self) -> None:
        await self._do(self.coordinator.client.key(self._tv_id, "MEDIA_PAUSE"))

    async def async_media_stop(self) -> None:
        await self._do(self.coordinator.client.key(self._tv_id, "MEDIA_STOP"))

    async def async_media_previous_track(self) -> None:
        await self._do(self.coordinator.client.key(self._tv_id, "MEDIA_REWIND"))

    async def async_media_next_track(self) -> None:
        await self._do(self.coordinator.client.key(self._tv_id, "MEDIA_FAST_FORWARD"))

    async def async_select_source(self, source: str) -> None:
        for a in self.coordinator.apps.get(self._tv_id, []):
            if a["name"] == source:
                await self._do(self.coordinator.client.launch(self._tv_id, a["pkg"]))
                return
        raise HomeAssistantError(f"Unknown app: {source}")
