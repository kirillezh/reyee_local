"""Access points added with "+ Add AP" (the router entry's Access Points group).

Each AP is read through the router with remoteIp=<AP ip> — the same relay the
router's own web UI uses — so no AP password is needed. Read-only.

Entities land on the AP device the router already shows (same identifier), so
adding an AP enriches that device instead of creating a second one.
"""
import asyncio
import logging
from datetime import timedelta

from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorStateClass)
from homeassistant.const import (
    EntityCategory, PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, UnitOfDataRate,
    UnitOfInformation)
from homeassistant.helpers.device_registry import DeviceInfo, CONNECTION_NETWORK_MAC
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity, DataUpdateCoordinator, UpdateFailed)

from .const import DOMAIN, CONF_HOST, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

AP_PLATFORMS = ("sensor", "button")
_AP_TYPES = ("EAP", "AP", "RAP")
_TIMEOUT = 8
_SLOW_EVERY = 10          # sysinfo / port / config every N polls


def _peel(r):
    for _ in range(4):
        if isinstance(r, dict) and "data" in r and set(r) <= {"code", "id", "data", "error"}:
            r = r["data"]
        else:
            break
    return r


def _bad(r):
    return (r in (None, "", {}, []) or not isinstance(r, dict)
            or "_error" in r or bool(r.get("err")))


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else f


def ap_candidates(hass, entry):
    """APs this router has found: {ip: {sn, name, model, mac, label}}."""
    gw = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    out = {}
    for n in ((gw.data or {}).get("topology", []) if gw is not None else []):
        if n.get("depth", 0) == 0 or str(n.get("type") or "").upper() not in _AP_TYPES:
            continue
        if n.get("ip") and n.get("sn"):
            out[n["ip"]] = {"sn": n["sn"], "name": n.get("name"), "model": n.get("model"),
                            "mac": n.get("mac"),
                            "label": f"{n.get('name') or n['sn']} · {n.get('model') or 'AP'} "
                                     f"({n['ip']})"}
    return out


def make_ap_record(entry, host, info):
    sn = info.get("sn")
    return {CONF_HOST: host, "sn": sn, "name": info.get("name"),
            "model": info.get("model"), "mac": info.get("mac"),
            # same identifier as the router's own AP device → one device per AP
            "key": f"{entry.entry_id}_infra_{sn}",
            "uid": f"{entry.entry_id}_ap_{sn}"}


class APCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, record):
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(hass, logger=_LOGGER, name=f"{DOMAIN}_ap",
                         update_interval=timedelta(seconds=max(30, interval)))
        self.entry = entry
        self.record = record
        self.host = record[CONF_HOST]
        self.key = record["key"]
        self.uid = record["uid"]
        self._poll = 0
        self._slow = {"sysinfo": {}, "ports": [], "radio_cfg": {}, "led": None}

    async def _get(self, method, module):
        gw = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if gw is None:
            return None
        try:
            r = await asyncio.wait_for(gw.api.raw_cmd(
                method, module, no_parse=True, extra={"remoteIp": self.host}), _TIMEOUT)
        except Exception:  # noqa: BLE001
            return None
        r = _peel(r)
        return None if _bad(r) else r

    async def _async_update_data(self):
        slow = self._poll % _SLOW_EVERY == 0
        self._poll += 1
        calls = [self._get("devSta.get", "radioInfo"), self._get("devSta.get", "sta_list")]
        if slow:
            calls += [self._get("devSta.get", "sysinfo"), self._get("devSta.get", "port_status"),
                      self._get("devSta.get", "radio"), self._get("devSta.get", "devLed")]
        res = await asyncio.gather(*calls)
        info, stas = res[0], res[1]
        if slow:
            if res[2]:
                self._slow["sysinfo"] = res[2]
            if res[3]:
                self._slow["ports"] = res[3].get("List") or res[3].get("list") or []
            if res[4]:
                self._slow["radio_cfg"] = {r.get("type"): r for r in res[4].get("radioList", [])}
            if res[5]:
                self._slow["led"] = res[5].get("led_all")
        if info is None and stas is None:
            if self.data:
                raise UpdateFailed(f"AP {self.host} not answering through the router")
            info = {}

        radios = {}
        for r in (info or {}).values():
            if not isinstance(r, dict) or not r.get("band"):
                continue
            radios[r["band"]] = {
                "channel": _num(r.get("channel")), "util": _num(r.get("chutil")),
                "obss_util": _num(r.get("obss_util")), "tx_util": _num(r.get("tx_util")),
                "clients": _num(r.get("sta_num")), "txpower": _num(r.get("txpower")),
                "noise": _num(r.get("channf")), "bw": _num(r.get("bw")),
                "frequency": _num(r.get("frequency")),
                "tx_bytes": _num(r.get("tx_bytes")), "rx_bytes": _num(r.get("rx_bytes")),
            }
        clients = []
        for s in ((stas or {}).get("list") or []):
            name = s.get("hostName")
            clients.append({
                "name": name if name and name != "NULL" else s.get("mac"),
                "mac": s.get("mac"), "ip": s.get("userIp"), "ssid": s.get("ssid"),
                "band": s.get("band"), "rssi": _num(s.get("rssi")),
                "rate": s.get("rxrate"), "since": s.get("onlineTime")})
        clients.sort(key=lambda c: c["rssi"] if c["rssi"] is not None else 0)
        uplink = next((p for p in self._slow["ports"]
                       if str(p.get("poe_direct")) == "in" or p.get("name") == "WAN"), None)
        tx = sum(r["tx_bytes"] or 0 for r in radios.values())
        rx = sum(r["rx_bytes"] or 0 for r in radios.values())
        return {
            "radios": radios, "clients": clients,
            "client_count": len(clients) if stas is not None else
            sum(r["clients"] or 0 for r in radios.values()),
            "tx_bytes": tx or None, "rx_bytes": rx or None,
            "uplink_speed": _num((uplink or {}).get("speed")),
            "firmware": self._slow["sysinfo"].get("software_version"),
            "model": self._slow["sysinfo"].get("model") or self.record.get("model"),
            "weakest": clients[0] if clients else None,
        }


# ── entities ─────────────────────────────────────────────────────────────
class _ApBase(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coord, key, name):
        super().__init__(coord)
        rec = coord.record
        self._attr_unique_id = f"{coord.uid}_{key}"
        self._attr_name = name
        info = {"identifiers": {(DOMAIN, coord.key)}, "manufacturer": "Ruijie",
                "model": rec.get("model"), "configuration_url": f"http://{coord.host}",
                "via_device": (DOMAIN, coord.entry.entry_id)}
        if rec.get("name"):
            info["name"] = rec["name"]
        if rec.get("mac"):
            info["connections"] = {(CONNECTION_NETWORK_MAC, rec["mac"])}
        self._attr_device_info = DeviceInfo(**info)

    @property
    def _d(self):
        return self.coordinator.data or {}


class ApClients(_ApBase, SensorEntity):
    _attr_icon = "mdi:account-multiple"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "clients"

    def __init__(self, coord):
        super().__init__(coord, "clients", "Clients")

    @property
    def native_value(self):
        return self._d.get("client_count")

    @property
    def extra_state_attributes(self):
        return {"clients": [
            {k: c[k] for k in ("name", "ip", "ssid", "band", "rssi", "rate")}
            for c in self._d.get("clients", [])]}


class ApRadioValue(_ApBase, SensorEntity):
    def __init__(self, coord, band, field, label, unit=None, icon=None, dc=None,
                 diag=False, enabled=True):
        super().__init__(coord, f"{band}_{field}".replace(".", ""), f"{band} {label}")
        self._band, self._field = band, field
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_device_class = dc
        if field not in ("channel",):
            self._attr_state_class = SensorStateClass.MEASUREMENT
        if diag:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_entity_registry_enabled_default = enabled

    @property
    def native_value(self):
        return (self._d.get("radios", {}).get(self._band) or {}).get(self._field)

    @property
    def extra_state_attributes(self):
        if self._field != "channel":
            return None
        r = self._d.get("radios", {}).get(self._band) or {}
        return {"frequency_mhz": r.get("frequency"), "bandwidth_mhz": r.get("bw")}


class ApValue(_ApBase, SensorEntity):
    def __init__(self, coord, key, label, unit=None, icon=None, dc=None, sc=None, diag=False,
                 scale=None):
        super().__init__(coord, key, label)
        self._key, self._scale = key, scale
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_device_class = dc
        self._attr_state_class = sc
        if diag:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        v = self._d.get(self._key)
        if v is not None and self._scale:
            v = round(v / self._scale, 2)
        return v


class ApWeakest(_ApBase, SensorEntity):
    _attr_icon = "mdi:wifi-strength-1-alert"
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord):
        super().__init__(coord, "weakest", "Weakest Client Signal")

    @property
    def native_value(self):
        return (self._d.get("weakest") or {}).get("rssi")

    @property
    def extra_state_attributes(self):
        w = self._d.get("weakest") or {}
        return {"client": w.get("name"), "ip": w.get("ip"), "band": w.get("band")}


class ApRefresh(_ApBase, ButtonEntity):
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord):
        super().__init__(coord, "refresh", "Refresh")

    async def async_press(self):
        await self.coordinator.async_request_refresh()


def _ap_entities(coord, platform):
    if platform == "button":
        return [ApRefresh(coord)]
    ents = [
        ApClients(coord), ApWeakest(coord),
        ApValue(coord, "uplink_speed", "Uplink Speed", UnitOfDataRate.MEGABITS_PER_SECOND,
                "mdi:ethernet", SensorDeviceClass.DATA_RATE, diag=True),
        ApValue(coord, "tx_bytes", "Data Sent", UnitOfInformation.GIGABYTES, "mdi:upload",
                SensorDeviceClass.DATA_SIZE, SensorStateClass.TOTAL_INCREASING, scale=1e9),
        ApValue(coord, "rx_bytes", "Data Received", UnitOfInformation.GIGABYTES, "mdi:download",
                SensorDeviceClass.DATA_SIZE, SensorStateClass.TOTAL_INCREASING, scale=1e9),
        ApValue(coord, "firmware", "Firmware", icon="mdi:chip", diag=True),
    ]
    for band in ("2.4G", "5G"):
        ents += [
            ApRadioValue(coord, band, "clients", "Clients", "clients", "mdi:account-multiple"),
            ApRadioValue(coord, band, "channel", "Channel", None, "mdi:wifi-cog"),
            ApRadioValue(coord, band, "util", "Channel Busy", PERCENTAGE, "mdi:chart-donut"),
            ApRadioValue(coord, band, "txpower", "TX Power", PERCENTAGE, "mdi:signal",
                         diag=True),
            ApRadioValue(coord, band, "noise", "Noise Floor", SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
                         "mdi:waveform", SensorDeviceClass.SIGNAL_STRENGTH, diag=True),
            ApRadioValue(coord, band, "obss_util", "Neighbour Interference", PERCENTAGE,
                         "mdi:wifi-alert", diag=True, enabled=False),
        ]
    return ents


async def setup_router_aps(hass, entry, raw_add, platform):
    """Add the entities of every AP in the router's Access Points group."""
    from .subentries import APS, bind
    if platform not in AP_PLATFORMS:
        return
    gw = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    coords = getattr(gw, "ap_coords", None) or {}
    if not coords:
        return
    add = bind(entry, raw_add, APS)
    for coord in coords.values():
        try:
            add(_ap_entities(coord, platform))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Reyee: AP %s %s setup failed: %s", coord.host, platform, err)
