"""Config flow: point Home Assistant at a running TVHub server."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SSL, CONF_TOKEN
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import TVHubAuthError, TVHubClient, TVHubError
from .const import DEFAULT_PORT, DOMAIN


class TVHubConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    _hassio: dict[str, Any]

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The TVHub add-on announced itself through the Supervisor (host, port and an API token)."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        cfg = discovery_info.config
        self._hassio = {CONF_HOST: str(cfg[CONF_HOST]), CONF_PORT: int(cfg.get(CONF_PORT, DEFAULT_PORT)),
                        CONF_TOKEN: str(cfg.get(CONF_TOKEN, "")), CONF_SSL: False}
        await self.async_set_unique_id(f"{self._hassio[CONF_HOST]}:{self._hassio[CONF_PORT]}")
        self._abort_if_unique_id_configured(updates=self._hassio)
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        d = self._hassio
        if user_input is not None:
            client = TVHubClient(async_get_clientsession(self.hass), d[CONF_HOST], d[CONF_PORT], d[CONF_TOKEN], d[CONF_SSL])
            try:
                await client.tvs()
            except TVHubAuthError:
                errors["base"] = "invalid_auth"
            except TVHubError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title=f"TVHub ({d[CONF_HOST]})", data=d)
        return self.async_show_form(step_id="hassio_confirm", description_placeholders={"addon": d[CONF_HOST]}, errors=errors)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            client = TVHubClient(async_get_clientsession(self.hass), host, user_input[CONF_PORT],
                                  user_input.get(CONF_TOKEN, ""), user_input.get(CONF_SSL, False))
            try:
                await client.tvs()
            except TVHubAuthError:
                errors["base"] = "invalid_auth"
            except TVHubError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"{host}:{user_input[CONF_PORT]}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"TVHub ({host})", data={**user_input, CONF_HOST: host})
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Optional(CONF_TOKEN, default=(user_input or {}).get(CONF_TOKEN, "")): str,
            vol.Optional(CONF_SSL, default=(user_input or {}).get(CONF_SSL, False)): bool,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
