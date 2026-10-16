"""Config flow + options flow for Reyee (Local)."""
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectOptionDict, SelectSelector, SelectSelectorConfig, SelectSelectorMode,
    TextSelector, TextSelectorConfig, TextSelectorType)

from .const import (DOMAIN, CONF_HOST, CONF_USERNAME, CONF_PASSWORD,
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
from .api import ReyeeLocalAPI, ReyeeAuthError, ReyeeConnError
from .switch_hub import (KIND_SWITCH, gateway_knows_switch, is_es_model, probe_direct,
                         is_switch_entry, switch_candidates, check_switch, make_record)
from .subentries import (SWITCHES, APS, subentry_id, switch_records, set_switch_records,
                         ap_records, set_ap_records)
from .ap_hub import ap_candidates, make_ap_record

try:
    from homeassistant.config_entries import ConfigSubentryFlow
except ImportError:  # HA < 2025.3
    ConfigSubentryFlow = None

_OTHER = "__other__"


def _router_with_switch_group(hass):
    for e in hass.config_entries.async_entries(DOMAIN):
        if not is_switch_entry(e) and subentry_id(e, SWITCHES):
            return e
    return None


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
            router = _router_with_switch_group(self.hass)
            if known_switch and router:
                return self._add_to_router(router, host, user_input, known_switch.get("model"),
                                           known_switch.get("sn"))
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

            if is_switch and router and not errors:
                return self._add_to_router(router, host, user_input, (info or {}).get("model"),
                                           (info or {}).get("serial_num"))
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

    def _add_to_router(self, router, host, user_input, model, sn):
        """Switches live inside the router entry's "Switches" group now."""
        recs = [r for r in switch_records(router) if r.get(CONF_HOST) != host]
        recs.append(make_record(router, host, user_input.get(CONF_USERNAME, "admin"),
                                user_input.get(CONF_PASSWORD, ""), model, sn))
        set_switch_records(self.hass, router, recs)
        return self.async_abort(reason="added_to_router")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ReyeeOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry):
        if ConfigSubentryFlow is None or is_switch_entry(config_entry):
            return {}
        return {SWITCHES: ReyeeSwitchSubentryFlow, APS: ReyeeApSubentryFlow}


class ReyeeSwitchSubentryFlow(ConfigSubentryFlow or object):
    """ "+ Add switch" on the router entry: pick one the router has found."""

    async def async_step_user(self, user_input=None):
        entry = self._get_entry()
        sid = subentry_id(entry, SWITCHES)
        if not sid:
            return self.async_abort(reason="no_group")
        have = {r.get(CONF_HOST) for r in switch_records(entry)}
        found = {ip: v for ip, v in switch_candidates(self.hass, entry).items()
                 if ip not in have}
        errors = {}
        if user_input is not None:
            host = str(user_input.get("switch") or "").strip()
            if host == _OTHER:
                host = ""
            host = host.split(" ", 1)[0]          # "192.168.88.14 · RG-ES…" → IP
            if not host:
                errors["base"] = "need_host"
            elif host in have:
                return self.async_abort(reason="already_added")
            else:
                user = user_input.get(CONF_USERNAME) or "admin"
                pw = user_input.get(CONF_PASSWORD) or entry.data.get(CONF_PASSWORD, "")
                ok, model, sn = await check_switch(self.hass, entry, host, user, pw)
                if not ok:
                    errors["base"] = "cannot_connect"
                else:
                    recs = switch_records(entry) + [
                        make_record(entry, host, user, user_input.get(CONF_PASSWORD) or "",
                                    model, sn)]
                    # updating the group reloads the router entry → switch appears
                    set_switch_records(self.hass, entry, recs)
                    return self.async_abort(reason="switch_added",
                                            description_placeholders={"host": host})

        options = [SelectOptionDict(value=ip, label=v["label"]) for ip, v in found.items()]
        first = options[0]["value"] if options else vol.UNDEFINED
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                # dropdown of found switches; you can also type any IP into it
                vol.Required("switch", default=first): SelectSelector(SelectSelectorConfig(
                    options=options, mode=SelectSelectorMode.DROPDOWN, custom_value=True)),
                vol.Optional(CONF_USERNAME, default="admin"): TextSelector(),
                vol.Optional(CONF_PASSWORD, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }),
            errors=errors,
            description_placeholders={"router": entry.data.get(CONF_HOST, ""),
                                      "count": str(len(found))},
        )

    async def async_step_reconfigure(self, user_input=None):
        """Remove switches from the group."""
        entry = self._get_entry()
        recs = switch_records(entry)
        if not recs:
            return self.async_abort(reason="no_switches")
        labels = {r[CONF_HOST]: r.get("title") or r[CONF_HOST] for r in recs}
        if user_input is not None:
            drop = set(user_input.get("remove") or [])
            if drop:
                set_switch_records(self.hass, entry,
                                   [r for r in recs if r[CONF_HOST] not in drop])
            return self.async_abort(reason="reconfigure_successful")
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Optional("remove", default=[]): cv.multi_select(labels)}),
        )


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


class ReyeeApSubentryFlow(ConfigSubentryFlow or object):
    """ "+ Add AP" on the router entry: pick one of the router's access points.
    APs are read through the router, so no password is asked."""

    async def async_step_user(self, user_input=None):
        entry = self._get_entry()
        if not subentry_id(entry, APS):
            return self.async_abort(reason="no_group")
        have = {r.get(CONF_HOST) for r in ap_records(entry)}
        found = {ip: v for ip, v in ap_candidates(self.hass, entry).items() if ip not in have}
        if not found:
            return self.async_abort(reason="no_new_aps")
        if user_input is not None:
            picked = [ip for ip in (user_input.get("aps") or []) if ip in found]
            if picked:
                recs = ap_records(entry) + [make_ap_record(entry, ip, found[ip]) for ip in picked]
                set_ap_records(self.hass, entry, recs)      # reloads the router entry
                return self.async_abort(reason="ap_added",
                                        description_placeholders={"count": str(len(picked))})
        options = [SelectOptionDict(value=ip, label=v["label"]) for ip, v in found.items()]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("aps", default=list(found)): SelectSelector(SelectSelectorConfig(
                    options=options, multiple=True, mode=SelectSelectorMode.LIST)),
            }),
            description_placeholders={"router": entry.data.get(CONF_HOST, ""),
                                      "count": str(len(found))},
        )

    async def async_step_reconfigure(self, user_input=None):
        """Remove APs from the group."""
        entry = self._get_entry()
        recs = ap_records(entry)
        if not recs:
            return self.async_abort(reason="no_aps")
        labels = {r[CONF_HOST]: f"{r.get('name') or r.get('sn')} ({r[CONF_HOST]})" for r in recs}
        if user_input is not None:
            drop = set(user_input.get("remove") or [])
            if drop:
                set_ap_records(self.hass, entry, [r for r in recs if r[CONF_HOST] not in drop])
            return self.async_abort(reason="reconfigure_successful")
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Optional("remove", default=[]): cv.multi_select(labels)}),
        )
