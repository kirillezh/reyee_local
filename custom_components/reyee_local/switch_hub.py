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
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity, DataUpdateCoordinator, UpdateFailed)

from .const import DOMAIN, CONF_HOST, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

KIND_SWITCH = "switch"
SWITCH_PLATFORMS = ["binary_sensor", "sensor", "button", "switch", "select", "text"]

# Switch-wide on/off settings: key, module, field, on, off, name, icon
GLOBALS = (
    ("rldp", "rldp", "enable", 1, 0, "Loop Detection", "mdi:sync-alert"),
    ("stp", "stp", "enable", 1, 0, "Spanning Tree", "mdi:graph-outline"),
    ("lldp", "lldp", "enable", 1, 0, "LLDP", "mdi:lan-connect"),
    ("dhcp_snooping", "dhcp_snooping", "enable", 1, 0, "DHCP Snooping", "mdi:shield-lock-outline"),
    ("igmp_snooping", "l2_multicast_base", "igmp_snp_en", 1, 0, "IGMP Snooping", "mdi:television-play"),
    ("voice_vlan", "voice_vlan", "enable", "1", "0", "Voice VLAN", "mdi:phone-voip"),
)
_STAMPS = ("version", "configId", "configTime", "currentTime")
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


# ── switches inside the router entry ("Switches" group) ─────────────────
def switch_candidates(hass, entry):
    """Switches this router has found: {ip: {model, sn, label}}."""
    from .coordinator import is_switch_model
    gw = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    out = {}
    if gw is None or not hasattr(gw, "_slow"):
        return out
    for n in (gw.data or {}).get("topology", []):
        if n.get("ip") and is_switch_model(n.get("model"), n.get("type")):
            out[n["ip"]] = {"model": n.get("model"), "sn": n.get("sn")}
    for n in gw._slow.get("neighbors", []):
        if n.get("ip") and (n.get("is_switch") or is_switch_model(n.get("model"), n.get("role"))):
            cur = out.setdefault(n["ip"], {})
            cur["model"] = cur.get("model") or n.get("model")
            cur["sn"] = cur.get("sn") or n.get("sn")
    for ip, v in out.items():
        kind = "PoE" if is_es_model(v.get("model")) else "managed"
        v["label"] = f"{ip} · {v.get('model') or 'Switch'} ({kind})"
    return dict(sorted(out.items(), key=lambda kv: tuple(
        int(x) if x.isdigit() else 0 for x in kv[0].split("."))))


async def check_switch(hass, entry, host, username, password):
    """Can we manage `host`? → (ok, model, sn)."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from .api import ReyeeLocalAPI
    node = gateway_knows_switch(hass, host) or switch_candidates(hass, entry).get(host) or {}
    if node and is_es_model(node.get("model")):
        return True, node.get("model"), node.get("sn")      # via the router
    api = ReyeeLocalAPI(host=host, password=password, username=username or "admin",
                        session=async_get_clientsession(hass))
    info = None
    try:
        await asyncio.wait_for(api.login(), 10)
        info = await probe_direct(api)
    except Exception:  # noqa: BLE001
        info = None
    if info:
        return True, info.get("model") or node.get("model"), info.get("serial_num") or node.get("sn")
    if node:
        return True, node.get("model"), node.get("sn")      # relayed by the router
    return False, None, None


def make_record(entry, host, username, password, model, sn, key=None, title=None):
    return {
        CONF_HOST: host,
        "username": username or "admin",
        "password": password or "",
        "model": model,
        "sn": sn,
        "key": key or f"{entry.entry_id}_sw_{host}",
        "title": title or f"{host} Reyee {model or 'Switch'}",
    }


async def setup_router_switches(hass, entry, raw_add, platform):
    """Add the entities of every switch in the router's Switches group."""
    from .subentries import SWITCHES, bind
    gw = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    coords = getattr(gw, "switch_coords", None) or {}
    if not coords:
        return
    add = bind(entry, raw_add, SWITCHES)
    for coord in coords.values():
        if platform not in SWITCH_PLATFORMS:
            continue
        try:
            await async_setup_platform_for_switch(hass, entry, add, platform, coord)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Reyee: switch %s %s setup failed: %s", coord.host, platform, err)


class SwitchCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, api, record=None):
        """entry: the switch's own entry (legacy) or the router entry.
        record: the switch's row in the router's "Switches" group."""
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(hass, logger=_LOGGER, name=f"{DOMAIN}_switch",
                         update_interval=timedelta(seconds=interval))
        self.entry = entry
        self.api = api                  # direct API to the switch
        self.record = record or {}
        self.host = self.record.get(CONF_HOST) or entry.data[CONF_HOST]
        # key → unique ids + device identifier (kept stable across the move
        # from a standalone entry into the router's Switches group)
        self.key = self.record.get("key") or entry.entry_id
        self.title = self.record.get("title") or entry.title
        self.gw_entry_id = entry.entry_id if record else None
        self.mode = None                # "direct" | "relay"
        self.sysinfo = {}
        self._desc = {}
        self._poll = 0
        self._slowcache = {"protected": {}, "macs": {}, "vlans": [], "port_vlan": {},
                           "globals": {}}

    # ── transport ────────────────────────────────────────────────────────
    def _gateway(self):
        own = self.hass.data.get(DOMAIN, {}).get(self.gw_entry_id) if self.gw_entry_id else None
        if own is not None and hasattr(own, "_slow"):
            return own
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

    async def _write(self, module, data, order=("devConfig.update", "devConfig.set")):
        """Config write in the eWeb's format; tries the methods in `order`."""
        last = None
        for method in order:
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

    async def async_set_global(self, key, on):
        spec = next(g for g in GLOBALS if g[0] == key)
        _k, module, field, on_v, off_v, _n, _i = spec
        cfg = await self._call("devConfig.get", module)
        if not isinstance(cfg, dict):
            raise HomeAssistantError(f"Couldn't read {module}")
        out = {k: v for k, v in cfg.items() if k not in _STAMPS}
        out[field] = on_v if on else off_v
        await self._write(module, out, order=("devConfig.set", "devConfig.update"))
        self._poll = 0
        await self.async_request_refresh()

    async def async_set_port_pvid(self, lpid, pvid):
        """Change a port's access VLAN (access port) or native VLAN (trunk)."""
        p = next((x for x in (self.data or {}).get("ports", []) if x["lpid"] == lpid), None)
        if p and p["name"] == (self.data or {}).get("uplink"):
            raise HomeAssistantError("Refusing to change the uplink port's VLAN (would cut the switch off)")
        cfg = await self._call("devConfig.get", "port_vlan")
        row = next((r for r in (cfg.get("list", []) if isinstance(cfg, dict) else [])
                    if r.get("lpid") == lpid), None)
        if row is None:
            raise HomeAssistantError(f"Couldn't read VLAN settings for port {lpid}")
        mode = row.get("mode")
        permit = row.get("permitvlan", "")
        if mode == 2 and permit and str(pvid) not in permit.split(","):
            permit = f"{permit},{pvid}"
        entry = {"mode": mode, "lpid": lpid, "pvid": str(pvid),
                 "permitvlan": "" if mode == 1 else permit,
                 "untagvlan": row.get("untagvlan", "") if mode == 0 else ""}
        await self._write("port_vlan", {"list": [entry]})
        self._poll = 0
        await self.async_request_refresh()

    async def async_create_vlan(self, vlan_id, name=""):
        await self._write("vlan", {"v": str(int(vlan_id)), "n": name or ""},
                          order=("devConfig.add", "devConfig.set"))
        self._poll = 0
        await self.async_request_refresh()

    async def async_delete_vlan(self, vlan_id):
        if int(vlan_id) == 1:
            raise HomeAssistantError("VLAN 1 can't be deleted")
        await self._write("vlan", {"v": str(int(vlan_id))}, order=("devConfig.del",))
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

    # ── ES-series (no eWeb): gateway relays CGI calls via /api/switch ────
    # Taken from the gateway eWeb: switchApi("doSwitchApi", {ip, sn, method, url, data})
    ES_READS = {
        "port": "rest_get_port.cgi",
        "poe": "rest_get_poe_info.cgi",
        "poe_power": "rest_get_poe_power_info.cgi",
        "sys_power": "rest_get_sys_power_info.cgi",
        "info": "rest_get_info.cgi",
        "panel": "rest_get_panel.cgi",
        "vlan_list": "rest_get_vlan_list.cgi",
    }

    def _es_sn(self):
        return ((gateway_knows_switch(self.hass, self.host) or {}).get("sn")
                or self.record.get("sn"))

    async def _esw(self, url, method="get", data=None):
        """One doSwitchApi call through the gateway. Returns (ok, payload)."""
        gw = self._gateway()
        if gw is None or not hasattr(gw.api, "switch_api"):
            return False, "no gateway"
        params = {"ip": self.host, "sn": self._es_sn(), "method": method, "url": url}
        if data is not None:
            params["data"] = data
        body = await gw.api.switch_api(params)
        if not isinstance(body, dict):
            return (body is not None), body
        if "_error" in body:
            return False, body["_error"]
        code = body.get("code")
        if code not in (0, "0", None):
            return False, body
        # Replies are wrapped twice: gateway {code,data:{…}} around the switch's own
        # {result:…, dev_type:"esw", code:0}. Peel every layer down to `result`.
        r = body
        for _ in range(5):
            if not isinstance(r, dict):
                break
            if r.get("code") not in (0, "0", None):
                return False, r
            if "result" in r and set(r) <= {"result", "dev_type", "code", "msg", "id", "error"}:
                r = r["result"]
            elif "data" in r and set(r) <= {"data", "code", "id", "error", "msg"}:
                r = r["data"]
            else:
                break
        return True, r

    async def _esw_write(self, url, data=None):
        ok, res = await self._esw(url, method="post", data=data)
        if not ok:
            raise HomeAssistantError(f"Switch {self.host} rejected {url}: {str(res)[:200]}")
        return res

    async def _read_es(self):
        raw = dict(getattr(self, "_es_cache", {}))
        first = not hasattr(self, "_es_dumped")
        keys = list(self.ES_READS) if first else (
            ["port", "poe", "vlan_list"] if self._poll % 10 == 0 else ["port", "poe"])
        for key in keys:
            ok, res = await self._esw(self.ES_READS[key])
            meaningful = ok and res not in (None, "", [], {}) and not (
                isinstance(res, dict) and set(res) <= {"result", "dev_type"})
            if meaningful:
                raw[key] = res                 # keep the last good reply otherwise
            elif key not in raw:
                raw[key] = None
            if first:
                raw.setdefault("_errors", {})[key] = None if ok else str(res)[:300]
        if self._poll % 5 == 0:
            ok, res = await self._esw("mac.cgi?page=fwd_tbl_json")
            if ok:
                raw["mac"] = res
        self._es_cache = raw
        if first:
            self._es_dumped = True
            await self.hass.async_add_executor_job(
                _dump, self.hass.config.path(f"reyee_es_{self.host.replace('.', '_')}.json"),
                {"host": self.host, "sn": self._es_sn(), "via": "/api/switch doSwitchApi",
                 "replies": raw})
        return raw

    # ES parsing — tolerant: field names are matched from a few likely spellings.
    @staticmethod
    def _first(d, keys, default=None):
        for k in keys:
            if isinstance(d, dict) and k in d and d[k] not in (None, ""):
                return d[k]
        return default

    @staticmethod
    def _truthy(v):
        return str(v).strip().lower() in ("1", "true", "on", "up", "enable", "enabled", "yes", "link")

    @staticmethod
    def _list_in(obj):
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for k in ("list", "ports", "port_list", "portList", "portInfo", "poe_list",
                      "poeList", "data", "port"):
                if isinstance(obj.get(k), list):
                    return obj[k]
            for v in obj.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    return v
        return []

    _ES_SPEED = {1: "10M half", 2: "10M", 3: "100M half", 4: "100M", 5: "1000M", 6: "2.5G",
                 7: "10G"}

    @staticmethod
    def _num(v, div=1, nd=1):
        try:
            return round(float(v) / div, nd)
        except (TypeError, ValueError):
            return None

    def _parse_es(self, raw):
        """Parse the ES switch replies (field names confirmed on ES205GC-P / ES220GS-P)."""
        poe_raw = raw.get("poe") if isinstance(raw.get("poe"), dict) else {}
        poe_ports = set()
        for row in (raw.get("panel") or []) if isinstance(raw.get("panel"), list) else []:
            for cell in (row or []) if isinstance(row, list) else []:
                if isinstance(cell, dict) and cell.get("poe") == 1 and cell.get("lpid") is not None:
                    poe_ports.add(int(cell["lpid"]))
        for item in self._list_in(poe_raw):
            try:
                poe_ports.add(int(item.get("port")))
            except (TypeError, ValueError, AttributeError):
                pass

        # MAC table (ports numbered from 1) → device names; busiest port = uplink
        names = {}
        gw = self._gateway()
        gw_macs = set()
        for c in ((gw.data or {}).get("clients", []) if gw else []):
            if c.get("mac"):
                names[_norm_mac(c["mac"])] = c.get("name") or c["mac"].upper()
        gw_sys = ((gw.data or {}).get("sysinfo") or {}) if gw else {}
        if gw_sys.get("sys_mac"):
            gw_macs.add(_norm_mac(gw_sys["sys_mac"]))
        for n in ((gw.data or {}).get("topology", []) if gw else []):
            if n.get("depth") == 0 and n.get("mac"):
                gw_macs.add(_norm_mac(n["mac"]))
        by_port = {}
        for row in self._list_in(raw.get("mac")):
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get("port")) - 1
            except (TypeError, ValueError):
                continue
            mac = _norm_mac(row.get("mac"))
            if mac and mac not in by_port.setdefault(pid, []):
                by_port[pid].append(mac)
        uplink = None
        if by_port:
            uplink = next((p for p, ms in by_port.items() if gw_macs & set(ms)), None)
            if uplink is None:
                non_poe = [p for p in by_port if p not in poe_ports]
                pool = non_poe or list(by_port)
                uplink = max(pool, key=lambda p: len(by_port[p]))

        ports = []
        for i, item in enumerate(self._list_in(raw.get("port"))):
            if not isinstance(item, dict):
                continue
            try:
                pid = int(item.get("port", i))
            except (TypeError, ValueError):
                pid = i
            has_poe = pid in poe_ports
            devices = [] if pid == uplink else [names.get(m, m.upper()) for m in by_port.get(pid, [])]
            vids = item.get("vid") if isinstance(item.get("vid"), list) else []
            ports.append({
                "port": pid, "label": f"Port {pid + 1}",
                "link": item.get("status") == 1,
                "enabled": item.get("enable") == 1,
                "speed": self._ES_SPEED.get(item.get("dup_r")) if item.get("status") == 1 else None,
                "rx_rate": item.get("rxSpeed"), "tx_rate": item.get("txSpeed"),
                "rx_bytes": item.get("rxBytes"), "tx_bytes": item.get("txBytes"),
                "errors": (item.get("rxBad") or 0) + (item.get("txBad") or 0),
                "loop": bool(item.get("loop")),
                "fiber": item.get("isFiber") == 1,
                "pvid": item.get("pvid"),
                "vlans": ",".join(str(v) for v in vids),
                "vid_list": [int(v) for v in vids if str(v).isdigit()],
                "vlan_mode": "trunk" if str(item.get("vlanType")) == "1" else "access",
                "uplink": pid == uplink,
                "poe": has_poe,
                "poe_on": (item.get("pEnable") == 1) if has_poe else None,
                "poe_active": (item.get("pStatus") == 1) if has_poe else None,
                "poe_power": self._num(item.get("pPower"), 1000, 2) if has_poe else None,
                "poe_voltage": self._num(item.get("pVoltage"), 1000, 1) if has_poe else None,
                "poe_current": item.get("pCurrent") if has_poe else None,
                "poe_fault": bool(item.get("pFault")) if has_poe else None,
                "devices": devices[:20],
            })
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        # VLANs: switch's own list, else every VLAN seen on its ports; names from the router
        gw_names = {str(v.get("vlan_id")): v.get("name")
                    for v in ((gw.data or {}).get("vlans", []) if gw else []) if v.get("vlan_id")}
        vids = set()
        for v in self._list_in(raw.get("vlan_list")) or (raw.get("vlan_list") or []):
            vid = self._first(v, ("vlanid", "vid", "id", "v")) if isinstance(v, dict) else v
            if str(vid).isdigit():
                vids.add(int(vid))
        for item in self._list_in(raw.get("port")):
            for vid in (item.get("vid") or []) if isinstance(item, dict) else []:
                if str(vid).isdigit():
                    vids.add(int(vid))
        on_switch = set(vids)
        vids |= {int(v) for v in gw_names if str(v).isdigit() and 1 <= int(v) <= 4094}
        es_vlans = [{"id": str(v), "name": gw_names.get(str(v)) or ("Default" if v == 1 else f"VLAN {v}"),
                     "on_switch": v in on_switch}
                    for v in sorted(vids)]
        return {
            "es_vlans": es_vlans,
            "es_ports": sorted(ports, key=lambda p: p["port"]),
            "es_uplink": f"Port {uplink + 1}" if uplink is not None else None,
            "es_hostname": info.get("hostname"),
            "poe_max": self._num(poe_raw.get("maxp"), 1000),
            "poe_used": self._num(poe_raw.get("usedp"), 1000) if poe_raw.get("usedp") is not None
                        else (round(sum(p["poe_power"] or 0 for p in ports if p["poe"]), 1)
                              if any(p["poe"] for p in ports) else None),
            "poe_remaining": self._num(poe_raw.get("remap"), 1000),
            "loop_ports": [p["label"] for p in ports if p["loop"]],
        }

    async def async_es_set_vlan(self, port, vid=None, mode=None):
        """Change a port's VLAN and/or mode (access/trunk) — vlan_p.cgi vlan_setting_add."""
        p = next((x for x in (self.data or {}).get("es_ports", []) if x["port"] == port), None)
        if p is None:
            raise HomeAssistantError(f"No port {port + 1}")
        if p.get("uplink"):
            raise HomeAssistantError(
                f"{p['label']} is this switch's uplink to the router — changing its VLAN "
                "would cut the switch off.")
        mode = mode or p.get("vlan_mode") or "access"
        native = int(vid if vid is not None else (p.get("pvid") or 1))
        if mode == "trunk":
            permit = sorted(set(p.get("vid_list") or []) | {native})
            if p.get("vlan_mode") != "trunk":       # access → trunk: allow every VLAN
                permit = sorted({int(v["id"]) for v in (self.data or {}).get("es_vlans", [])
                                 if v.get("on_switch")} | {native})
            data = {"port": [port], "vlanType": "1", "native": native, "permit": permit}
        else:
            data = {"port": [port], "vlanType": "0", "native": native, "permit": [native]}
        known = {int(v["id"]) for v in (self.data or {}).get("es_vlans", []) if v.get("on_switch")}
        for v in data["permit"]:
            if v not in known and v != 1:
                await self._esw_write("vlan_p.cgi?cmd=vlan_entry_add", {"vlanid": v})
        await self._esw_write("vlan_p.cgi?cmd=vlan_setting_add", data)
        await asyncio.sleep(1)
        await self.async_request_refresh()

    async def async_es_poe(self, port, on):
        await self._esw_write("poe.cgi", {"portid": port, "mode": "1" if on else "0"})
        await asyncio.sleep(1.5 if on else 1)
        await self.async_request_refresh()

    async def async_es_poe_restart(self, port):
        await self._esw_write("home.cgi?cmd=poe_restart", {"portid": port})

    async def async_es_reboot(self):
        await self._esw_write("reboot.cgi")

    async def _pick_mode(self):
        node = gateway_knows_switch(self.hass, self.host) or {}
        if is_es_model(node.get("model") or self.record.get("model")):
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
            raw = await self._read_es()
            self._poll += 1
            parsed = self._parse_es(raw)
            info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
            return {
                "online": bool(gw_node) or bool(parsed["es_ports"]),
                "mode": "es", "ports": [], "uplink": None,
                "model": info.get("devtype") or gw_node.get("model"),
                "serial": info.get("sn") or gw_node.get("sn"),
                "mac": info.get("mac") or gw_node.get("mac"),
                "firmware": info.get("sver"),
                "hw_version": info.get("hver"),
                "ports_supported": bool(parsed["es_ports"]),
                "crc_total": None, **parsed,
                "es_errors": raw.get("_errors"),
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
            if self._poll % 5 == 1:
                pv = await self._call("devConfig.get", "port_vlan")
                if isinstance(pv, dict) and isinstance(pv.get("list"), list):
                    self._slowcache["port_vlan"] = {r.get("lpid"): r for r in pv["list"]}
            if self._poll % 10 == 1:
                vl = await self._call("devConfig.get", "vlan")
                if isinstance(vl, dict) and isinstance(vl.get("list"), list):
                    self._slowcache["vlans"] = [
                        {"id": str(v.get("v")), "name": v.get("n") or f"VLAN{v.get('v')}"}
                        for v in vl["list"]]
                g = {}
                for key, module, field, on_v, _off, _n, _i in GLOBALS:
                    cfg = await self._call("devConfig.get", module)
                    if isinstance(cfg, dict) and field in cfg:
                        g[key] = str(cfg[field]) == str(on_v)
                if g:
                    self._slowcache["globals"] = g
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
                "vlan_mode": {0: "hybrid", 1: "access", 2: "trunk"}.get(
                    (self._slowcache["port_vlan"].get(lp) or {}).get("mode")),
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
            "vlans": self._slowcache["vlans"],
            "globals": self._slowcache["globals"],
            "crc_total": sum(int(p.get("crc_errors") or 0) for p in ports),
        }


# ── entities ─────────────────────────────────────────────────────────────
def _device(coord):
    d = coord.data or {}
    return DeviceInfo(
        identifiers={(DOMAIN, coord.key)},
        manufacturer="Ruijie",
        name=coord.title,
        model=d.get("model"),
        sw_version=d.get("firmware"),
        serial_number=d.get("serial"),
        configuration_url=f"http://{coord.host}",
    )


class _Base(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coord, key):
        super().__init__(coord)
        self._attr_unique_id = f"{coord.key}_{key}"
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
            return {"connection": "via router (ES series)", "ip": self.coordinator.host,
                    "serial": d.get("serial"), "mac": d.get("mac"),
                    "ports_read": len(d.get("es_ports") or []),
                    "errors": {k: v for k, v in (d.get("es_errors") or {}).items() if v} or None}
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


class _PortBase(_Base):
    def __init__(self, coord, key, lpid, label):
        super().__init__(coord, f"{key}_{lpid}")
        self._lpid = lpid
        self._attr_name = label

    def _port(self):
        for p in (self.coordinator.data or {}).get("ports", []):
            if p.get("lpid") == self._lpid:
                return p
        return None

    @property
    def available(self):
        return super().available and self._port() is not None


class SwitchPortVlan(_PortBase, SelectEntity):
    """Access VLAN (access port) / native VLAN (trunk port)."""
    _attr_icon = "mdi:lan"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord, lpid, name):
        super().__init__(coord, "port_vlan", lpid, f"Port {name} VLAN")

    def _label(self, vid):
        for v in (self.coordinator.data or {}).get("vlans", []):
            if v["id"] == str(vid):
                return f"{v['id']} – {v['name']}"
        return str(vid)

    @property
    def options(self):
        opts = [self._label(v["id"]) for v in (self.coordinator.data or {}).get("vlans", [])]
        cur = self.current_option
        if cur and cur not in opts:
            opts.append(cur)
        return opts or ["1"]

    @property
    def current_option(self):
        p = self._port()
        return self._label(p.get("pvid")) if p and p.get("pvid") else None

    @property
    def extra_state_attributes(self):
        p = self._port() or {}
        return {"mode": p.get("vlan_mode"), "allowed_vlans": p.get("vlans") or None}

    async def async_select_option(self, option):
        await self.coordinator.async_set_port_pvid(self._lpid, option.split(" ")[0])


class SwitchPortDescription(_PortBase, TextEntity):
    _attr_icon = "mdi:tag-text-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_max = 32

    def __init__(self, coord, lpid, name):
        super().__init__(coord, "port_desc", lpid, f"Port {name} Description")

    @property
    def native_value(self):
        p = self._port()
        return (p or {}).get("description") or ""

    async def async_set_value(self, value):
        p = self._port() or {}
        await self.coordinator.async_set_port_description(p.get("name", self._lpid), value)


class SwitchPortIsolated(_PortBase, SwitchEntity):
    _attr_icon = "mdi:shield-half-full"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord, lpid, name):
        super().__init__(coord, "port_iso", lpid, f"Port {name} Isolated")

    @property
    def is_on(self):
        return bool((self._port() or {}).get("isolated"))

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_set_port_isolation((self._port() or {})["name"], True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_set_port_isolation((self._port() or {})["name"], False)


class SwitchPortValue(_PortBase, SensorEntity):
    """rx/tx rate, speed or CRC count for one port."""

    def __init__(self, coord, lpid, name, field, label, unit=None, icon=None,
                 enabled=True, state_class=SensorStateClass.MEASUREMENT, diag=False):
        super().__init__(coord, f"port_{field}", lpid, f"Port {name} {label}")
        self._field = field
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_entity_registry_enabled_default = enabled
        self._attr_state_class = state_class
        if diag:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return (self._port() or {}).get(self._field)


class SwitchGlobal(_Base, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord, key, name, icon):
        super().__init__(coord, f"global_{key}")
        self._key = key
        self._attr_name = name
        self._attr_icon = icon

    @property
    def available(self):
        return super().available and self._key in (self.coordinator.data or {}).get("globals", {})

    @property
    def is_on(self):
        return bool((self.coordinator.data or {}).get("globals", {}).get(self._key))

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_set_global(self._key, True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_set_global(self._key, False)


class SwitchVlans(_Base, SensorEntity):
    _attr_name = "VLANs"
    _attr_icon = "mdi:lan"

    def __init__(self, coord):
        super().__init__(coord, "vlans")

    @property
    def native_value(self):
        v = (self.coordinator.data or {}).get("vlans")
        return len(v) if v else None

    @property
    def extra_state_attributes(self):
        return {v["id"]: v["name"] for v in (self.coordinator.data or {}).get("vlans", [])}


def _per_port(coord, entry, async_add_entities, factory, skip_ag=True):
    seen = set()

    def _add():
        new = []
        for p in (coord.data or {}).get("ports", []):
            if p["lpid"] in seen or (skip_ag and p["name"].startswith("Ag")):
                continue
            seen.add(p["lpid"])
            new.extend(factory(p))
        if new:
            async_add_entities(new)

    _add()
    entry.async_on_unload(coord.async_add_listener(_add))


class _EsPort(_Base):
    def __init__(self, coord, key, port, label):
        super().__init__(coord, f"es_{key}_{port}")
        self._port_id = port
        self._attr_name = label

    def _p(self):
        for p in (self.coordinator.data or {}).get("es_ports", []):
            if p["port"] == self._port_id:
                return p
        return None

    @property
    def available(self):
        return super().available and self._p() is not None


class EsPortLink(_EsPort, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coord, port):
        super().__init__(coord, "link", port, f"Port {port + 1}")

    @property
    def is_on(self):
        return bool((self._p() or {}).get("link"))

    @property
    def icon(self):
        p = self._p() or {}
        if p.get("uplink"):
            return "mdi:arrow-up-network"
        return "mdi:ethernet" if p.get("link") else "mdi:ethernet-off"

    @property
    def extra_state_attributes(self):
        p = self._p() or {}
        attrs = {k: p.get(k) for k in ("speed", "vlan_mode", "pvid", "vlans", "rx_rate",
                                       "tx_rate", "errors", "loop", "uplink", "fiber")}
        if p.get("poe"):
            attrs.update({"poe_on": p.get("poe_on"), "poe_active": p.get("poe_active"),
                          "poe_power_w": p.get("poe_power"), "poe_voltage_v": p.get("poe_voltage"),
                          "poe_current_ma": p.get("poe_current"), "poe_fault": p.get("poe_fault")})
        attrs["devices"] = p.get("devices") or []
        attrs["connected"] = ", ".join(p.get("devices") or []) or None
        return attrs


class EsPortPower(_EsPort, SensorEntity):
    _attr_native_unit_of_measurement = "W"
    _attr_device_class = "power"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord, port):
        super().__init__(coord, "poe_power", port, f"Port {port + 1} PoE Power")

    @property
    def native_value(self):
        return (self._p() or {}).get("poe_power")


class EsPortRate(_EsPort, SensorEntity):
    _attr_native_unit_of_measurement = "kbit/s"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord, port, field, label, icon):
        super().__init__(coord, field, port, f"Port {port + 1} {label}")
        self._field = field
        self._attr_icon = icon

    @property
    def native_value(self):
        return (self._p() or {}).get(self._field)


class EsText(_Base, SensorEntity):
    def __init__(self, coord, key, label, icon):
        super().__init__(coord, f"es_{key}")
        self._key = key
        self._attr_name = label
        self._attr_icon = icon

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get(self._key)


class EsLoop(_Base, BinarySensorEntity):
    _attr_name = "Loop Detected"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coord):
        super().__init__(coord, "es_loop")

    @property
    def is_on(self):
        return bool((self.coordinator.data or {}).get("loop_ports"))

    @property
    def extra_state_attributes(self):
        return {"ports": (self.coordinator.data or {}).get("loop_ports", [])}


class EsPortPoe(_EsPort, SwitchEntity):
    _attr_icon = "mdi:flash"

    def __init__(self, coord, port):
        super().__init__(coord, "poe", port, f"Port {port + 1} PoE")

    @property
    def is_on(self):
        return bool((self._p() or {}).get("poe_on"))

    @property
    def extra_state_attributes(self):
        p = self._p() or {}
        return {"powering_device": p.get("poe_active"), "power_w": p.get("poe_power"),
                "connected": ", ".join(p.get("devices") or []) or None}

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_es_poe(self._port_id, True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_es_poe(self._port_id, False)


class EsPortPoeRestart(_EsPort, ButtonEntity):
    _attr_icon = "mdi:power-cycle"

    def __init__(self, coord, port):
        super().__init__(coord, "poe_restart", port, f"Port {port + 1} PoE Power-Cycle")

    async def async_press(self):
        await self.coordinator.async_es_poe_restart(self._port_id)


class EsPoePower(_Base, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "W"
    _attr_icon = "mdi:flash"

    def __init__(self, coord, key, label):
        super().__init__(coord, f"es_{key}")
        self._key = key
        self._attr_name = label

    @property
    def native_value(self):
        v = (self.coordinator.data or {}).get(self._key)
        try:
            return round(float(v), 1)
        except (TypeError, ValueError):
            return None


class EsPortVlan(_EsPort, SelectEntity):
    """Access VLAN (access port) / native VLAN (trunk port) on an ES switch."""
    _attr_icon = "mdi:lan"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord, port):
        super().__init__(coord, "vlan", port, f"Port {port + 1} VLAN")

    def _label(self, vid):
        for v in (self.coordinator.data or {}).get("es_vlans", []):
            if v["id"] == str(vid):
                return f"{v['id']} – {v['name']}"
        return str(vid)

    @property
    def options(self):
        opts = [self._label(v["id"]) for v in (self.coordinator.data or {}).get("es_vlans", [])]
        cur = self.current_option
        if cur and cur not in opts:
            opts.append(cur)
        return opts or ["1"]

    @property
    def current_option(self):
        p = self._p()
        return self._label(p.get("pvid")) if p and p.get("pvid") is not None else None

    @property
    def extra_state_attributes(self):
        p = self._p() or {}
        return {"mode": p.get("vlan_mode"), "allowed_vlans": p.get("vlans") or None,
                "uplink": p.get("uplink")}

    async def async_select_option(self, option):
        await self.coordinator.async_es_set_vlan(self._port_id, vid=option.split(" ")[0])


class EsPortVlanMode(_EsPort, SelectEntity):
    _attr_icon = "mdi:lan-pending"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["access", "trunk"]

    def __init__(self, coord, port):
        super().__init__(coord, "vlan_mode", port, f"Port {port + 1} VLAN Mode")

    @property
    def current_option(self):
        return (self._p() or {}).get("vlan_mode")

    async def async_select_option(self, option):
        await self.coordinator.async_es_set_vlan(self._port_id, mode=option)


class EsVlans(_Base, SensorEntity):
    _attr_name = "VLANs"
    _attr_icon = "mdi:lan"

    def __init__(self, coord):
        super().__init__(coord, "es_vlans")

    @property
    def native_value(self):
        v = (self.coordinator.data or {}).get("es_vlans")
        return len(v) if v else None

    @property
    def extra_state_attributes(self):
        return {v["id"]: v["name"] for v in (self.coordinator.data or {}).get("es_vlans", [])}


class EsReboot(_Base, ButtonEntity):
    _attr_name = "Reboot"
    _attr_icon = "mdi:restart"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord):
        super().__init__(coord, "es_reboot")

    async def async_press(self):
        await self.coordinator.async_es_reboot()


def _es_per_port(coord, entry, async_add_entities, factory, poe_only=False):
    seen = set()

    def _add():
        new = []
        for p in (coord.data or {}).get("es_ports", []):
            if p["port"] in seen or (poe_only and not p.get("poe")):
                continue
            seen.add(p["port"])
            new.extend(factory(p["port"]))
        if new:
            async_add_entities(new)

    _add()
    entry.async_on_unload(coord.async_add_listener(_add))


async def async_setup_platform_for_switch(hass, entry, async_add_entities, platform, coord=None):
    coord = coord or hass.data[DOMAIN][entry.entry_id]
    if coord.mode == "es":
        # ES-series PoE switches: their own feature set (ports + PoE), read and
        # controlled through the router's /api/switch relay.
        if platform == "binary_sensor":
            async_add_entities([SwitchOnline(coord), EsLoop(coord)])
            _es_per_port(coord, entry, async_add_entities, lambda n: [EsPortLink(coord, n)])
        elif platform == "switch":
            _es_per_port(coord, entry, async_add_entities, lambda n: [EsPortPoe(coord, n)],
                         poe_only=True)
        elif platform == "select":
            _es_per_port(coord, entry, async_add_entities,
                         lambda n: [EsPortVlan(coord, n), EsPortVlanMode(coord, n)])
        elif platform == "button":
            async_add_entities([SwitchRefresh(coord), EsReboot(coord)])
            _es_per_port(coord, entry, async_add_entities,
                         lambda n: [EsPortPoeRestart(coord, n)], poe_only=True)
        elif platform == "sensor":
            async_add_entities([EsPoePower(coord, "poe_used", "PoE Power Used"),
                                EsPoePower(coord, "poe_max", "PoE Power Budget"),
                                EsPoePower(coord, "poe_remaining", "PoE Power Remaining"),
                                EsText(coord, "es_uplink", "Uplink Port", "mdi:arrow-up-network"),
                                EsText(coord, "es_hostname", "Switch Name", "mdi:tag"),
                                EsVlans(coord)])
            _es_per_port(coord, entry, async_add_entities,
                         lambda n: [EsPortPower(coord, n)], poe_only=True)
            _es_per_port(coord, entry, async_add_entities, lambda n: [
                EsPortRate(coord, n, "rx_rate", "RX Rate", "mdi:download-network"),
                EsPortRate(coord, n, "tx_rate", "TX Rate", "mdi:upload-network")])
        return
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
        async_add_entities([SwitchPortsUp(coord), SwitchUplink(coord), SwitchCrc(coord),
                            SwitchVlans(coord)])
        _per_port(coord, entry, async_add_entities, lambda p: [
            SwitchPortValue(coord, p["lpid"], p["name"], "rx_rate", "RX Rate", "kbit/s",
                            "mdi:download-network"),
            SwitchPortValue(coord, p["lpid"], p["name"], "tx_rate", "TX Rate", "kbit/s",
                            "mdi:upload-network"),
            SwitchPortValue(coord, p["lpid"], p["name"], "speed", "Link Speed", None,
                            "mdi:speedometer", enabled=False, state_class=None, diag=True),
            SwitchPortValue(coord, p["lpid"], p["name"], "crc_errors", "CRC Errors", None,
                            "mdi:alert-circle-outline", enabled=False,
                            state_class=SensorStateClass.TOTAL_INCREASING, diag=True),
        ])
    elif platform == "button":
        async_add_entities([SwitchRefresh(coord), SwitchReboot(coord)])
    elif platform == "select":
        _per_port(coord, entry, async_add_entities,
                  lambda p: [SwitchPortVlan(coord, p["lpid"], p["name"])])
    elif platform == "text":
        _per_port(coord, entry, async_add_entities,
                  lambda p: [SwitchPortDescription(coord, p["lpid"], p["name"])])
    elif platform == "switch":
        async_add_entities([SwitchGlobal(coord, k, n, i) for k, _m, _f, _on, _off, n, i in GLOBALS])
        _per_port(coord, entry, async_add_entities,
                  lambda p: [SwitchPortIsolated(coord, p["lpid"], p["name"])])
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
