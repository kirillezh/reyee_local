"""Config flow + options flow for Reyee (Local)."""
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (DOMAIN, CONF_HOST, CONF_USERNAME, CONF_PASSWORD,
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
from .api import ReyeeLocalAPI, ReyeeAuthError, ReyeeConnError
from .switch_hub import KIND_SWITCH, gateway_knows_switch, is_es_model, probe_direct


class ReyeeLocalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            user_input[CONF_HOST] = user_input[CONF_HOST].strip()
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_configured()
            api = ReyeeLocalAPI(
                host=user_input[CONF_HOST],
                password=user_input[CONF_PASSWORD],
                username=user_input.get(CONF_USERNAME, "admin"),
                session=async_get_clientsession(self.hass),
            )
            host = user_input[CONF_HOST]
            known_switch = gateway_knows_switch(self.hass, host)
            if known_switch and is_es_model(known_switch.get("model")):
                # ES-series switches have no eWeb login — manage through the router.
                return self.async_create_entry(
                    title=f"{host} Reyee {known_switch.get('model') or 'Switch'}",
                    data={**user_input, "kind": KIND_SWITCH},
                )
            try:
                await api.login()
            except ReyeeAuthError:
                errors["base"] = "invalid_auth"
            except ReyeeConnError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"

            info = await probe_direct(api) if not errors else None
            is_switch = bool(known_switch) or bool(info and (
                str(info.get("productType", "")).lower() in ("msw", "esw", "sw")
                or str(info.get("forwardMode", "")).upper() == "SWITCH"))

            if is_switch and (not errors or known_switch):
                # A switch: added even if it refuses a direct login, as long as
                # the gateway entry knows it (it's then read through the router).
                model = (info or {}).get("model") or (known_switch or {}).get("model") or "Switch"
                return self.async_create_entry(
                    title=f"{host} Reyee {model}",
                    data={**user_input, "kind": KIND_SWITCH},
                )
            if not errors:
                return self.async_create_entry(title=f"Reyee {host}", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_USERNAME, default="admin"): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ReyeeOptionsFlow()


class ReyeeOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_SCAN_INTERVAL, default=current): vol.All(
                    int, vol.Range(min=10, max=300)
                ),
            }),
        )
