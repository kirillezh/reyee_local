"""Managed switch as its own hub entry (Add Integration → switch IP).

Talks to the switch directly when it answers; otherwise reads it through the
Reyee gateway entry (the gateway relays calls with remoteIp=<switch ip>, the
same way the gateway's own web UI manages switches).
"""
import asyncio
import logging
from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass, BinarySensorEntity)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity, DataUpdateCoordinator, UpdateFailed)

from .const import DOMAIN, CONF_HOST, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

KIND_SWITCH = "switch"
SWITCH_PLATFORMS = ["binary_sensor", "sensor", "button"]
_TIMEOUT = 8


def is_switch_entry(entry):
    return entry.data.get("kind") == KIND_SWITCH


def _peel(r):
    for _ in range(4):
        if isinstance(r, dict) and "data" in r and set(r) <= {"code", "id", "data", "error"}:
            r = r["data"]
        else:
            break
    return r


def _bad(r):
    if r is None or r == "" or r == {} or r == []:
        return True
    if isinstance(r, dict):
        if "_error" in r or r.get("err") or r.get("rmsg"):
            return True
        if r.get("rcode") and str(r["rcode"]) != "00000000":
            return True
    return False


def gateway_coordinators(hass):
    from .coordinator import ReyeeCoordinator
    return [c for c in hass.data.get(DOMAIN, {}).values()
            if isinstance(c, ReyeeCoordinator)]


def gateway_knows_switch(hass, host):
    """Neighbour record for `host` if a gateway entry sees it as a switch."""
    for gw in gateway_coordinators(hass):
        for n in gw._slow.get("neighbors", []):
            if n.get("ip") == host and n.get("is_switch"):
                return n
    return None


async def probe_direct(api):
    """sysinfo straight from the device, or None if it doesn't answer."""
    try:
        r = await asyncio.wait_for(api.raw_cmd("devSta.get", "sysinfo", no_parse=True), _TIMEOUT)
    except Exception:  # noqa: BLE001
        return None
    r = _peel(r)
    return None if _bad(r) or not isinstance(r, dict) else r


class SwitchCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, api):
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(hass, logger=_LOGGER, name=f"{DOMAIN}_switch",
                         update_interval=timedelta(seconds=interval))
        self.entry = entry
        self.api = api                  # direct API to the switch
        self.host = entry.data[CONF_HOST]
        self.mode = None                # "direct" | "relay"
        self.sysinfo = {}
        self._desc = {}
        self._poll = 0

    # ── transport ────────────────────────────────────────────────────────
    def _gateway(self):
        gws = gateway_coordinators(self.hass)
        for gw in gws:
            if any(n.get("ip") == self.host for n in gw._slow.get("neighbors", [])):
                return gw
        return gws[0] if gws else None

    async def _call(self, method, module, data=None, no_parse=True):
        try:
            if self.mode == "direct":
                r = await asyncio.wait_for(self.api.raw_cmd(
                    method, module, data=data, no_parse=no_parse), _TIMEOUT)
            else:
                gw = self._gateway()
                if gw is None:
                    return None
                r = await asyncio.wait_for(gw.api.raw_cmd(
                    method, module, data=data, no_parse=no_parse,
                    extra={"remoteIp": self.host}), _TIMEOUT)
        except Exception:  # noqa: BLE001
            return None
        r = _peel(r)
        return None if _bad(r) else r

    async def _pick_mode(self):
        info = await probe_direct(self.api)
        if info:
            self.mode, self.sysinfo = "direct", info
            return
        self.mode = "relay"
        info = await self._call("devSta.get", "sysinfo")
        if isinstance(info, dict):
            self.sysinfo = info

    # ── poll ─────────────────────────────────────────────────────────────
    async def _async_update_data(self):
        if self.mode is None:
            await self._pick_mode()
        gw_node = gateway_knows_switch(self.hass, self.host) or {}

        ps = await self._call("devSta.get", "port_show")
        if not isinstance(ps, list) and self.mode == "direct":
            # direct path stopped answering — fall back to the gateway relay
            self.mode = "relay"
            ps = await self._call("devSta.get", "port_show")

        if self._poll % 10 == 0 and isinstance(ps, list):
            cfg = await self._call("devConfig.get", "port_phy")
            rows = cfg.get("data") if isinstance(cfg, dict) else None
            if isinstance(rows, list):
                self._desc = {r.get("lpid"): (r.get("description") or "").strip()
                              for r in rows if isinstance(r, dict)}
        self._poll += 1

        uplink = await self._call("devSta.get", "linkup") if isinstance(ps, list) else None

        ports = []
        for p in (ps if isinstance(ps, list) else []):
            nm = str(p.get("nm", ""))
            if not nm.startswith(("Gi", "Te", "Fa", "Ag")):
                continue
            if nm.startswith("Ag") and not p.get("lk"):
                continue
            lp = p.get("lp")
            ports.append({
                "lpid": lp, "name": nm, "link": p.get("lk") == 1,
                "speed": p.get("r_spd") or None,
                "rx_rate": p.get("iRt"), "tx_rate": p.get("oRt"),
                "rx_bytes": p.get("iBt"), "tx_bytes": p.get("oBt"),
                "crc_errors": p.get("crc"), "pvid": p.get("pvid"),
                "vlans": p.get("pmtVAct") or p.get("pmtV") or "",
                "description": self._desc.get(lp) or "",
            })

        online = bool(ports) or bool(gw_node)
        if not online and self.mode == "relay" and self._gateway() is None:
            raise UpdateFailed("Switch not reachable directly and no Reyee gateway entry is loaded")
        return {
            "online": online,
            "mode": self.mode,
            "ports": ports,
            "uplink": uplink.get("interface") if isinstance(uplink, dict) else None,
            "model": self.sysinfo.get("model") or gw_node.get("model"),
            "serial": self.sysinfo.get("serial_num") or gw_node.get("sn"),
            "mac": self.sysinfo.get("sys_mac") or gw_node.get("mac"),
            "firmware": self.sysinfo.get("software_version"),
            "ports_supported": isinstance(ps, list),
        }


# ── entities ─────────────────────────────────────────────────────────────
def _device(coord):
    d = coord.data or {}
    return DeviceInfo(
        identifiers={(DOMAIN, coord.entry.entry_id)},
        manufacturer="Ruijie",
        name=coord.entry.title,
        model=d.get("model"),
        sw_version=d.get("firmware"),
        serial_number=d.get("serial"),
        configuration_url=f"http://{coord.host}",
    )


class _Base(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coord, key):
        super().__init__(coord)
        self._attr_unique_id = f"{coord.entry.entry_id}_{key}"
        self._attr_device_info = _device(coord)


class SwitchOnline(_Base, BinarySensorEntity):
    _attr_name = "Online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coord):
        super().__init__(coord, "online")

    @property
    def is_on(self):
        return bool((self.coordinator.data or {}).get("online"))

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        return {"connection": "via router" if d.get("mode") == "relay" else "direct",
                "ip": self.coordinator.host, "serial": d.get("serial"),
                "mac": d.get("mac"),
                "port_data": "available" if d.get("ports_supported") else
                             "not available for this model yet"}


class SwitchPortLink(_Base, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coord, lpid, name):
        super().__init__(coord, f"port_{lpid}")
        self._lpid = lpid
        self._attr_name = f"Port {name}"

    def _port(self):
        for p in (self.coordinator.data or {}).get("ports", []):
            if p.get("lpid") == self._lpid:
                return p
        return None

    @property
    def available(self):
        return super().available and self._port() is not None

    @property
    def is_on(self):
        p = self._port()
        return bool(p and p.get("link"))

    @property
    def icon(self):
        return "mdi:ethernet" if self.is_on else "mdi:ethernet-off"

    @property
    def extra_state_attributes(self):
        p = self._port() or {}
        return {k: p.get(k) for k in ("speed", "description", "pvid", "vlans", "rx_rate",
                                      "tx_rate", "rx_bytes", "tx_bytes", "crc_errors")}


class SwitchPortsUp(_Base, SensorEntity):
    _attr_name = "Ports Up"
    _attr_icon = "mdi:ethernet"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord):
        super().__init__(coord, "ports_up")

    @property
    def native_value(self):
        ports = (self.coordinator.data or {}).get("ports", [])
        return sum(1 for p in ports if p.get("link")) if ports else None

    @property
    def extra_state_attributes(self):
        ports = (self.coordinator.data or {}).get("ports", [])
        return {"total_ports": len(ports),
                "up": [p["name"] for p in ports if p.get("link")]}


class SwitchUplink(_Base, SensorEntity):
    _attr_name = "Uplink Port"
    _attr_icon = "mdi:arrow-up-network"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord):
        super().__init__(coord, "uplink")

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("uplink")


class SwitchRefresh(_Base, ButtonEntity):
    _attr_name = "Refresh"
    _attr_icon = "mdi:refresh"

    def __init__(self, coord):
        super().__init__(coord, "refresh")

    async def async_press(self):
        await self.coordinator.async_request_refresh()


async def async_setup_platform_for_switch(hass, entry, async_add_entities, platform):
    coord = hass.data[DOMAIN][entry.entry_id]
    if platform == "binary_sensor":
        async_add_entities([SwitchOnline(coord)])
        seen = set()

        def _add_ports():
            new = []
            for p in (coord.data or {}).get("ports", []):
                if p["lpid"] not in seen:
                    seen.add(p["lpid"])
                    new.append(SwitchPortLink(coord, p["lpid"], p["name"]))
            if new:
                async_add_entities(new)

        _add_ports()
        entry.async_on_unload(coord.async_add_listener(_add_ports))
    elif platform == "sensor":
        async_add_entities([SwitchPortsUp(coord), SwitchUplink(coord)])
    elif platform == "button":
        async_add_entities([SwitchRefresh(coord)])
