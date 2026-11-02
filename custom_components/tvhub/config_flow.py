"""Config flow: point Home Assistant at a running TVHub server."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SSL, CONF_TOKEN
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TVHubAuthError, TVHubClient, TVHubError
from .const import DEFAULT_PORT, DOMAIN


class TVHubConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

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
