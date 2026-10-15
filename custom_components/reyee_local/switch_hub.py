"""Managed switch as its own hub entry (Add Integration → switch IP).

Talks to the switch directly when it answers; otherwise reads it through the
Reyee gateway entry (the gateway relays calls with remoteIp=<switch ip>, the
same way the gateway's own web UI manages switches).
"""
import asyncio
import logging
import re
from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass, BinarySensorEntity)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity, DataUpdateCoordinator, UpdateFailed)

from .const import DOMAIN, CONF_HOST, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

KIND_SWITCH = "switch"
SWITCH_PLATFORMS = ["binary_sensor", "sensor", "button", "switch"]
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


_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$|^[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}$")


def _norm_mac(m):
    m = str(m or "").lower().replace(".", "").replace(":", "").replace("-", "")
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)) if len(m) == 12 else m


def _write_ok(body):
    if not isinstance(body, dict):
        return False
    if "_error" in body:
        return "timeout" in str(body["_error"]).lower()     # applied async
    inner = body.get("data") if isinstance(body.get("data"), dict) else {}
    for d in (body, inner):
        rc = d.get("rcode")
        if rc and str(rc) != "00000000":
            return False
        if d.get("err") or (d.get("code") not in (None, 0, "0")):
            return False
    return True


def _dump(path, obj):
    import json
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, default=str)


def gateway_coordinators(hass):
    from .coordinator import ReyeeCoordinator
    return [c for c in hass.data.get(DOMAIN, {}).values()
            if isinstance(c, ReyeeCoordinator)]


def gateway_knows_switch(hass, host):
    """Record for `host` if any gateway entry sees it as a switch
    (neighbour list, ES-switch list or topology)."""
    from .coordinator import is_switch_model
    host = (host or "").strip()
    for gw in gateway_coordinators(hass):
        for n in getattr(gw, "_slow", {}).get("neighbors", []):
            if n.get("ip") == host and (n.get("is_switch") or
                                        is_switch_model(n.get("model"), n.get("role"))):
                return n
        for n in (gw.data or {}).get("topology", []):
            if n.get("ip") == host and is_switch_model(n.get("model"), n.get("type")):
                return {"ip": host, "sn": n.get("sn"), "mac": n.get("mac"),
                        "model": n.get("model"), "is_switch": True}
    return None


def is_es_model(model):
    m = (model or "").upper()
    return m.startswith(("RG-ES", "ES"))


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
        self._slowcache = {"protected": {}, "macs": {}}

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

    async def _raw(self, method, module, data=None, no_parse=False, via_master=False):
        """Unfiltered call (for writes). via_master: send to the gateway itself."""
        try:
            if self.mode == "direct":
                return await asyncio.wait_for(self.api.raw_cmd(
                    method, module, data=data, no_parse=no_parse), 15)
            gw = self._gateway()
            if gw is None:
                return {"_error": "no gateway entry loaded"}
            extra = None if via_master else {"remoteIp": self.host}
            return await asyncio.wait_for(gw.api.raw_cmd(
                method, module, data=data, no_parse=no_parse, extra=extra), 15)
        except asyncio.TimeoutError:
            return {"_error": "timeout"}
        except Exception as err:  # noqa: BLE001
            return {"_error": str(err)}

    async def _write(self, module, data):
        """Config write: devConfig.update, falling back to devConfig.set."""
        last = None
        for method in ("devConfig.update", "devConfig.set"):
            last = await self._raw(method, module, data=data)
            if _write_ok(last):
                return method
        raise HomeAssistantError(f"Switch {self.host} rejected {module}: {str(last)[:200]}")

    def _port_by_name(self, port):
        port = str(port).strip()
        for p in (self.data or {}).get("ports", []):
            if p["name"].lower() == port.lower() or str(p["lpid"]) == port:
                return p
        raise HomeAssistantError(f"No port {port} on {self.host}")

    async def async_set_port_enabled(self, lpid, enabled):
        uplink = (self.data or {}).get("uplink")
        p = next((x for x in (self.data or {}).get("ports", []) if x["lpid"] == lpid), None)
        if not enabled and p and uplink and p["name"] == uplink:
            raise HomeAssistantError(
                f"{uplink} is this switch's uplink to the router — disabling it would cut the switch off.")
        base = await self._call("devSta.get", "port_base")
        row = next((r for r in (base or []) if isinstance(r, dict) and r.get("lpid") == lpid), None)
        if row is None:
            raise HomeAssistantError(f"Couldn't read port {lpid} settings")
        entry = {k: row[k] for k in ("lpid", "c_speed", "c_duplex", "c_flowcontrol") if k in row}
        entry["enable"] = 1 if enabled else 0
        await self._write("port_base", {"data": [entry]})
        await asyncio.sleep(2)
        await self.async_request_refresh()
        now = next((x for x in (self.data or {}).get("ports", []) if x["lpid"] == lpid), {})
        if now.get("enabled") is not None and now.get("enabled") != enabled:
            raise HomeAssistantError(f"Switch accepted the change but port {lpid} still reads "
                                     f"{'on' if now.get('enabled') else 'off'}")

    async def async_set_port_description(self, port, description):
        p = self._port_by_name(port)
        cfg = await self._call("devConfig.get", "port_phy")
        rows = cfg.get("data") if isinstance(cfg, dict) else None
        row = next((r for r in (rows or []) if r.get("lpid") == p["lpid"]), None)
        if row is None:
            raise HomeAssistantError("Couldn't read port settings")
        await self._write("port_phy", {"data": [{**row, "description": description[:32]}]})
        self._poll = 0
        await self.async_request_refresh()

    async def async_set_port_vlan(self, port, mode, pvid, permit="", untag=""):
        p = self._port_by_name(port)
        modes = {"access": 1, "trunk": 2, "hybrid": 0}
        m = modes.get(str(mode).lower())
        if m is None:
            raise HomeAssistantError("mode must be access, trunk or hybrid")
        if p["name"] == (self.data or {}).get("uplink"):
            raise HomeAssistantError("Refusing to change the uplink port's VLAN (would cut the switch off)")
        entry = {"mode": m, "lpid": p["lpid"], "pvid": str(pvid),
                 "permitvlan": "" if m == 1 else str(permit),
                 "untagvlan": str(untag) if m == 0 else ""}
        await self._write("port_vlan", {"list": [entry]})
        await self.async_request_refresh()

    async def async_set_port_isolation(self, port, enabled):
        p = self._port_by_name(port)
        await self._write("protected", {"data": [{"lpid": p["lpid"], "enable": 1 if enabled else 0}]})
        self._poll = 0
        await self.async_request_refresh()

    async def async_set_port_rate_limit(self, port, in_kbps, out_kbps):
        p = self._port_by_name(port)
        entry = {"lpid": p["lpid"]}
        if in_kbps:
            entry["irate"] = str(int(in_kbps))
        if out_kbps:
            entry["orate"] = str(int(out_kbps))
        if len(entry) == 1:          # 0/0 → remove the limit
            body = await self._raw("devConfig.del", "rate_limit",
                                   data={"data": [{"lpid": p["lpid"]}]})
            if not _write_ok(body):
                raise HomeAssistantError(f"Switch rejected removing the limit: {str(body)[:200]}")
        else:
            await self._write("rate_limit", {"data": [entry]})
        await self.async_request_refresh()

    async def async_reboot(self):
        sn = (self.data or {}).get("serial")
        if not sn:
            raise HomeAssistantError("Switch serial unknown — can't reboot")
        data = {"sn": [sn], "reboot": "true"}
        for method in ("devSta.set", "devConfig.set"):
            body = await self._raw(method, "devReboot", data=data, via_master=True)
            if _write_ok(body):
                return
        raise HomeAssistantError(f"Reboot not accepted: {str(body)[:200]}")

    # ── ES-series (no eWeb): gateway relays CGI calls via module "esw" ────
    ES_URLS = ("rest_get_panel.cgi", "rest_get_port.cgi", "rest_get_vlan_list.cgi",
               "rest_get_vlan_mode.cgi", "rest_get_vlan_multiple.cgi")

    async def _esw(self, url, variant=None):
        gw = self._gateway()
        if gw is None:
            return None, None
        sn = (gateway_knows_switch(self.hass, self.host) or {}).get("sn")
        hdr = {"ip": self.host, "sn": sn, "url": url, "method": "get"}
        shapes = {
            "params": ("devSta.get", None, hdr),
            "data": ("devSta.get", hdr, None),
            "params_cfg": ("devConfig.get", None, hdr),
            "data_nested": ("devSta.get", {"data": hdr}, None),
        }
        order = [variant] if variant else list(shapes)
        for v in order:
            method, data, extra = shapes[v]
            try:
                r = await asyncio.wait_for(gw.api.raw_cmd(
                    method, "esw", data=data, no_parse=True, extra=extra), _TIMEOUT)
            except Exception:  # noqa: BLE001
                continue
            r = _peel(r)
            if not _bad(r):
                return r, v
        return None, None

    async def _read_es(self):
        if not hasattr(self, "_es_variant"):
            self._es_variant, report = None, {}
            for url in self.ES_URLS:
                r, v = await self._esw(url)
                report[url] = r if r is not None else "no answer"
                if v and not self._es_variant:
                    self._es_variant = v
            self._es_report = report
            await self.hass.async_add_executor_job(
                _dump, self.hass.config.path(
                    f"reyee_es_{self.host.replace('.', '_')}.json"),
                {"host": self.host, "variant": str(self._es_variant), "replies": report})
        if not self._es_variant:
            return {}
        out = {}
        for url in ("rest_get_panel.cgi", "rest_get_port.cgi"):
            r, _v = await self._esw(url, self._es_variant)
            if r is not None:
                out[url] = r
        return out

    async def _pick_mode(self):
        node = gateway_knows_switch(self.hass, self.host) or {}
        if is_es_model(node.get("model")):
            self.mode = "es"
            return
        info = await probe_direct(self.api)
        if info:
            self.mode, self.sysinfo = "direct", info
            return
        self.mode = "relay"
        info = await self._call("devSta.get", "sysinfo")
        if isinstance(info, dict):
            self.sysinfo = info

    async def _read_macs(self):
        """Switch MAC table → per-port list of device names (names from the gateway)."""
        names = {}
        gw = self._gateway()
        for c in ((gw.data or {}).get("clients", []) if gw else []):
            if c.get("mac"):
                names[_norm_mac(c["mac"])] = c.get("name") or c["mac"].upper()
        rows, index = [], 1
        while index <= 20:
            r = await self._call("devSta.get", "show_mac", data={"index": index, "size": 100})
            lst = r.get("list") if isinstance(r, dict) else None
            if not lst:
                break
            rows += lst
            total = int(r.get("total") or r.get("count") or 0) if isinstance(r, dict) else 0
            if len(rows) >= total or len(lst) < 100:
                break
            index += 1
        by_port = {}
        uplink = (self.data or {}).get("uplink")
        name_to_lpid = {p["name"]: p["lpid"] for p in (self.data or {}).get("ports", [])}
        for row in rows:
            if not isinstance(row, dict):
                continue
            mac = next((v for v in row.values() if isinstance(v, str) and _MAC_RE.match(v)), None)
            lp = row.get("l", row.get("lpid"))
            if lp is None:
                pn = row.get("p") or row.get("port") or row.get("interface") or row.get("i")
                lp = name_to_lpid.get(pn)
            if mac is None or lp is None:
                continue
            if uplink and name_to_lpid.get(uplink) == lp:
                continue          # everything upstream shows on the uplink
            nm = names.get(_norm_mac(mac), _norm_mac(mac).upper())
            by_port.setdefault(lp, [])
            if nm not in by_port[lp]:
                by_port[lp].append(nm)
        if rows:
            self._slowcache["macs"] = by_port
            self._slowcache["mac_sample"] = rows[:2]

    # ── poll ─────────────────────────────────────────────────────────────
    async def _async_update_data(self):
        if self.mode is None:
            await self._pick_mode()
        gw_node = gateway_knows_switch(self.hass, self.host) or {}

        if self.mode == "es":
            es = await self._read_es()
            return {
                "online": bool(gw_node) or bool(es),
                "mode": "es", "ports": [], "uplink": None,
                "model": gw_node.get("model"), "serial": gw_node.get("sn"),
                "mac": gw_node.get("mac"), "firmware": None,
                "ports_supported": False, "loop_ports": [], "crc_total": None,
                "es_raw": es,
                "es_working": bool(getattr(self, "_es_variant", None)),
            }

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
        enabled, loops = {}, {}
        if isinstance(ps, list):
            base = await self._call("devSta.get", "port_base")
            for r in (base if isinstance(base, list) else []):
                if isinstance(r, dict):
                    enabled[r.get("lpid")] = r.get("enable") == 1
            rl = await self._call("devSta.get", "rldp_port_status")
            for r in (rl.get("list", []) if isinstance(rl, dict) else []):
                loops[r.get("lpid")] = r.get("detect_state") not in (0, "0", None)
            if self._poll % 10 == 1:
                pr = await self._call("devConfig.get", "protected")
                rows = pr.get("data") if isinstance(pr, dict) else None
                if isinstance(rows, list):
                    self._slowcache["protected"] = {r.get("lpid"): r.get("enable") == 1 for r in rows}
            if self._poll % 3 == 1:
                await self._read_macs()

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
                "enabled": enabled.get(lp),
                "loop": loops.get(lp, False),
                "isolated": self._slowcache["protected"].get(lp),
                "devices": self._slowcache["macs"].get(lp, []),
            })

        online = bool(ports) or bool(gw_node)
        loop_ports = [p["name"] for p in ports if p.get("loop")]
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
            "loop_ports": loop_ports,
            "crc_total": sum(int(p.get("crc_errors") or 0) for p in ports),
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
        if d.get("mode") == "es":
            raw = d.get("es_raw") or {}
            return {"connection": "via router (ES series)", "ip": self.coordinator.host,
                    "serial": d.get("serial"), "mac": d.get("mac"),
                    "es_reply": "working" if d.get("es_working") else "no answer yet",
                    "panel": str(raw.get("rest_get_panel.cgi"))[:1000] if raw else None,
                    "ports": str(raw.get("rest_get_port.cgi"))[:1000] if raw else None}
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
        attrs = {k: p.get(k) for k in ("speed", "description", "pvid", "vlans", "rx_rate",
                                       "tx_rate", "rx_bytes", "tx_bytes", "crc_errors",
                                       "enabled", "isolated", "loop")}
        attrs["devices"] = p.get("devices") or []
        attrs["connected"] = ", ".join(p.get("devices") or []) or None
        return attrs


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


class SwitchLoop(_Base, BinarySensorEntity):
    _attr_name = "Loop Detected"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coord):
        super().__init__(coord, "loop")

    @property
    def is_on(self):
        return bool((self.coordinator.data or {}).get("loop_ports"))

    @property
    def extra_state_attributes(self):
        return {"ports": (self.coordinator.data or {}).get("loop_ports", [])}


class SwitchCrc(_Base, SensorEntity):
    _attr_name = "CRC Errors"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord):
        super().__init__(coord, "crc_total")

    @property
    def native_value(self):
        d = self.coordinator.data or {}
        return d.get("crc_total") if d.get("ports") else None

    @property
    def extra_state_attributes(self):
        ports = (self.coordinator.data or {}).get("ports", [])
        return {p["name"]: p["crc_errors"] for p in ports if p.get("crc_errors")}


class SwitchReboot(_Base, ButtonEntity):
    _attr_name = "Reboot"
    _attr_icon = "mdi:restart"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord):
        super().__init__(coord, "reboot")

    async def async_press(self):
        await self.coordinator.async_reboot()


class SwitchPortEnable(_Base, SwitchEntity):
    """Enable/disable a port (admin up/down). The uplink port is protected."""
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord, lpid, name):
        super().__init__(coord, f"port_enable_{lpid}")
        self._lpid = lpid
        self._attr_name = f"Port {name} Enabled"

    def _port(self):
        for p in (self.coordinator.data or {}).get("ports", []):
            if p.get("lpid") == self._lpid:
                return p
        return None

    @property
    def available(self):
        p = self._port()
        return super().available and p is not None and p.get("enabled") is not None

    @property
    def is_on(self):
        p = self._port()
        return bool(p and p.get("enabled"))

    @property
    def icon(self):
        return "mdi:ethernet" if self.is_on else "mdi:ethernet-off"

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_set_port_enabled(self._lpid, True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_set_port_enabled(self._lpid, False)


async def async_setup_platform_for_switch(hass, entry, async_add_entities, platform):
    coord = hass.data[DOMAIN][entry.entry_id]
    if platform == "binary_sensor":
        async_add_entities([SwitchOnline(coord), SwitchLoop(coord)])
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
        async_add_entities([SwitchPortsUp(coord), SwitchUplink(coord), SwitchCrc(coord)])
    elif platform == "button":
        async_add_entities([SwitchRefresh(coord), SwitchReboot(coord)])
    elif platform == "switch":
        seen_sw = set()

        def _add_enables():
            new = []
            for p in (coord.data or {}).get("ports", []):
                if p["lpid"] not in seen_sw and not p["name"].startswith("Ag"):
                    seen_sw.add(p["lpid"])
                    new.append(SwitchPortEnable(coord, p["lpid"], p["name"]))
            if new:
                async_add_entities(new)

        _add_enables()
        entry.async_on_unload(coord.async_add_listener(_add_enables))
