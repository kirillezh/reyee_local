"""Reyee coordinator — full data model from confirmed eWeb modules (EG105G-V3).

Confirmed sources (from live eWeb capture):
  devSta.get   user_list  data={devType:all,dataType:timely}  → clients (wire+wireless)
  devConfig.get devRemark                                     → mac→custom name
  devSta.get   port_status                                    → 5 physical ports
  devSta.get   flow  data={func:interface_info}               → live WAN throughput
  devSta.get   flow  data={func:user_info}                    → per-IP live rate
  devSta.get   ipinfo                                         → per-WAN ip/gw/dns
  devSta.get   networkConnect                                 → internet up/down
  devSta.get   local_topology                                 → AP/switch tree
  devSta.get   arp                                            → fallback client coverage
  devConfig.get network / mllb                                → VLANs / WAN uplinks
"""
import asyncio
import ipaddress
import json
import logging
from datetime import timedelta

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, DEFAULT_SCAN_INTERVAL, CONF_SCAN_INTERVAL
from .api import (ReyeeAuthError, ReyeeConnError, build_master_swap_payload,
                  build_portmap_add, build_portmap_remove, build_flowctrl_toggle,
                  build_ssid_toggle, build_rate_limit, build_block_rule,
                  build_led_toggle, build_led_per_device)

_LOGGER = logging.getLogger(__name__)

_JUNK_NAMES = {"", "*", "wlan0", "unknown", "null"}


def _norm_mac(m):
    if not m:
        return ""
    m = m.lower().replace(".", "").replace(":", "").replace("-", "")
    if len(m) == 12:
        return ":".join(m[i:i + 2] for i in range(0, 12, 2))
    return m.lower()


def _clean_name(n):
    if n and n.strip().lower() not in _JUNK_NAMES:
        return n.strip()
    return None


def _list_of(r):
    """Pull `list` out of a reply, tolerating JSON-string or double-nested data."""
    for _ in range(3):
        if isinstance(r, str):
            try:
                r = json.loads(r)
            except ValueError:
                return []
        if isinstance(r, list):
            return r
        if not isinstance(r, dict):
            return []
        if isinstance(r.get("list"), list):
            return r["list"]
        r = r.get("data")
    return []


def _is_error(r):
    if r is None:
        return True
    if isinstance(r, str):
        return r.strip() == ""
    if isinstance(r, dict):
        if "_error" in r:
            return True
        if r.get("rcode") and str(r.get("rcode")) != "00000000":
            return True
        if r.get("rmsg"):
            return True
        d = r.get("data")
        if isinstance(d, str) and d.strip() == "":
            return True
        if set(r.keys()) <= {"code", "id", "error", "data"} and not d:
            return True
    return False


# Router features exposed as on/off switches. All confirmed readable on the
# live gateway via discovery; written back with the full config + one field.
#   key, method ("dev"=devConfig / "ac"=acConfig), field, on-value, off-value, name, icon
FEATURES = (
    ("hwnat", "dev", "enabled", "true", "false", "Hardware NAT", "mdi:chip"),
    ("firewall_extern", "dev", "sip_alg", "true", "false", "SIP ALG", "mdi:phone-voip"),
    ("igmpproxy", "dev", "enable", "1", "0", "IGMP Proxy", "mdi:television-play"),
    ("line_detection", "dev", "enable", "1", "0", "WAN Link Detection", "mdi:lan-check"),
    ("radioLb", "ac", "enable", "1", "0", "WiFi Load Balancing", "mdi:scale-balance"),
)
VPN_SERVERS = (("openvpn_server", "OpenVPN Server"), ("l2tp_server", "L2TP Server"),
               ("pptp_server", "PPTP Server"))
_STAMPS = ("version", "configTime", "currentTime", "configId",
           "subConfigId", "networkId")
_SLOW_EVERY = 10   # feature/VPN/neighbour reads every Nth poll


def _peel(r):
    """Strip nested {code,id,data,error} wrappers down to the payload."""
    for _ in range(4):
        if isinstance(r, dict) and "data" in r and set(r) <= {"code", "id", "data", "error"}:
            r = r["data"]
        else:
            break
    return r


def is_switch_model(model, role=None):
    m = (model or "").upper()
    return (str(role or "").upper() == "SWITCH"
            or m.startswith(("RG-ES", "ES", "NBS", "RG-NBS", "RG-S", "S2")))


class ReyeeCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, api):
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(hass, logger=_LOGGER, name=DOMAIN,
                         update_interval=timedelta(seconds=interval))
        self.api = api
        self.entry = entry
        self._poll = 0
        self._slow = {"features": {}, "vpn": {}, "net_names": {}, "neighbors": [],
                      "port_desc": {}}
        self._switches = {}

    async def _get(self, module, data=None):
        """Proven path (devSta.get, unwrapped) for classic modules."""
        try:
            r = await self.api.get_state(module, data)
            return None if _is_error(r) else r
        except Exception:  # noqa: BLE001
            return None

    async def _cfg(self, module, data=None):
        """Proven path (devConfig.get, unwrapped) for classic modules."""
        try:
            r = await self.api.get_config(module, data)
            return None if _is_error(r) else r
        except Exception:  # noqa: BLE001
            return None

    async def _ac(self, module, data=None):
        """acConfig.get path for the wireless controller modules."""
        try:
            r = await self.api.raw_cmd("acConfig.get", module, data=data)
            if _is_error(r):
                return None
            if isinstance(r, dict) and isinstance(r.get("data"), (dict, list)):
                return r["data"]
            return r
        except Exception:  # noqa: BLE001
            return None

    async def _getx(self, module, data=None, no_parse=True):
        """eWeb path (raw_cmd with noParse) for advanced modules; unwrap .data."""
        try:
            r = await self.api.raw_cmd("devSta.get", module, data=data, no_parse=no_parse)
            if _is_error(r):
                return None
            # raw_cmd returns the full body; unwrap a .data wrapper if present
            if isinstance(r, dict) and isinstance(r.get("data"), (dict, list)):
                return r["data"]
            return r
        except Exception:  # noqa: BLE001
            return None

    async def _cfgx(self, module, data=None, no_parse=False):
        try:
            r = await self.api.raw_cmd("devConfig.get", module, data=data, no_parse=no_parse)
            if _is_error(r):
                return None
            if isinstance(r, dict) and isinstance(r.get("data"), (dict, list)):
                return r["data"]
            return r
        except Exception:  # noqa: BLE001
            return None

    # ── VLAN parsing (unchanged, confirmed) ──────────────────────────────
    @staticmethod
    def _parse_networks(net_cfg):
        vlans = []
        if not isinstance(net_cfg, dict):
            return vlans
        payload = net_cfg.get("data") if isinstance(net_cfg.get("data"), dict) else net_cfg
        for lan in (payload.get("lan") or []):
            if not isinstance(lan, dict):
                continue
            ip = lan.get("ipaddr")
            mask = lan.get("netmask") or "255.255.255.0"
            if not ip:
                continue
            try:
                net = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
            except ValueError:
                continue
            vlans.append({
                "vlan_id": lan.get("vlanid") or lan.get("ovlanid"),
                "name": (lan.get("desc") or "").strip() or f"VLAN {lan.get('vlanid')}",
                "gateway": ip, "netmask": mask, "cidr": str(net), "network": net,
                "dhcp_from": lan.get("ipstart"), "lease_min": lan.get("leasetime"),
                "max_hosts": lan.get("limit"),
            })
        return vlans

    @staticmethod
    def _match_vlan(ip_str, vlans):
        if not ip_str:
            return None
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return None
        for v in vlans:
            if addr in v["network"]:
                return v
        return None

    @staticmethod
    def _flatten_topology(node, out, depth=0):
        if not isinstance(node, dict):
            return
        out.append({
            "name": node.get("name"),
            "type": node.get("deviceType"),
            "model": node.get("productClass"),
            "ip": node.get("manageIp"),
            "mac": node.get("mac"),
            "sn": node.get("deviceSn"),
            "online": node.get("onlineStatus") == "ON",
            "uplink_port": node.get("uplinkPort"),
            "depth": depth,
        })
        for child in node.get("children", []) or []:
            ReyeeCoordinator._flatten_topology(child, out, depth + 1)

    @staticmethod
    def _parse_pppoe(entries):
        if not entries:
            return {}
        ordered = sorted(entries, key=lambda e: int(e.get("order", 0) or 0))
        last = ordered[-1]
        code = str(last.get("code"))
        if code == "5":
            status = "connected"
        elif code in ("11", "9"):
            status = "disconnected"
        else:
            status = "connecting"
        disc = next((e.get("time") for e in reversed(ordered)
                     if str(e.get("code")) in ("11", "9")), None)
        conn = next((e.get("time") for e in reversed(ordered)
                     if str(e.get("code")) == "5"), None)
        drops = sum(1 for e in ordered if str(e.get("code")) == "11")
        recent = [f'{e.get("time")} \u2014 {e.get("msg")}' for e in ordered[-6:]]
        return {"status": status, "last_disconnect": disc,
                "last_connect": conn, "drop_count": drops, "recent": recent}

    async def _async_update_data(self):
        try:
            raw_arp = await self.api.get_state("arp")
        except ReyeeAuthError as err:
            raise UpdateFailed(f"Auth failed: {err}") from err
        except ReyeeConnError as err:
            raise UpdateFailed(f"Cannot reach router: {err}") from err
        arp = raw_arp.get("arpList", []) if isinstance(raw_arp, dict) else []

        # ── Real client list, custom names, live per-IP rate ──────────────
        ul = await self._getx("user_list",
                             {"devType": "all", "dataType": "timely"})
        user_list = _list_of(ul)

        remark_cfg = await self._cfgx("devRemark")
        remark = {}
        for e in _list_of(remark_cfg):
            if isinstance(e, dict):
                nm = _clean_name(e.get("name"))
                if nm:
                    remark[_norm_mac(e.get("mac"))] = nm

        # ── Slow-changing reads (features, VPN, saved names, neighbours) ──
        if self._poll % _SLOW_EVERY == 0:
            await self._read_slow()
        self._poll += 1
        net_names = self._slow["net_names"]

        flow_user = await self._getx("flow", {"func": "user_info"})
        rate_by_ip = {}
        if isinstance(flow_user, dict):
            for e in flow_user.get("list", []):
                if e.get("ip"):
                    rate_by_ip[e["ip"]] = {
                        "up": int(e.get("up", 0) or 0),
                        "down": int(e.get("down", 0) or 0),
                        "conns": int(e.get("flowNum", 0) or 0),
                    }

        # VLANs
        net_cfg = await self._cfg("network")
        vlans = self._parse_networks(net_cfg)

        # ── Merge: ARP (coverage) + user_list (rich) + remark (names) ─────
        ul_by_mac = {_norm_mac(u.get("mac")): u for u in user_list
                     if isinstance(u, dict) and u.get("mac")}
        clients = []
        vlan_counts = {}
        seen = set()

        def _build(mac, ip, intf, u):
            mac = _norm_mac(mac)
            v = self._match_vlan(ip, vlans)
            vname = v["name"] if v else (intf or "Unknown")
            u = u or {}
            # deviceAliasName is the alias of the Reyee *network device* (AP /
            # switch) the client hangs off — not the client. Never use it as
            # the client's name; expose it as access_point instead.
            name = (remark.get(mac)
                    or net_names.get(mac)
                    or _clean_name(u.get("hostName"))
                    or _clean_name(u.get("hostname"))
                    or mac.upper())
            ctype = u.get("connectType")
            rate = rate_by_ip.get(ip, {})
            return {
                "mac": mac, "ip": ip, "name": name,
                "vlan": vname, "vlan_id": v["vlan_id"] if v else None,
                "connection": ("wireless" if ctype == "wireless"
                               else "wired" if ctype == "wire" else None),
                "access_point": _clean_name(u.get("deviceAliasName")),
                "ssid": _clean_name(u.get("ssid")),
                "band": _clean_name(u.get("band")),
                "rssi": u.get("rssi") or None,
                "switch_port": u.get("port") or None,
                "online_since": u.get("onlinetime") or None,
                "rate_up": rate.get("up"), "rate_down": rate.get("down"),
                "conns": rate.get("conns"),
            }

        for e in arp:
            mac = _norm_mac(e.get("hardware"))
            if not mac or mac in seen:
                continue
            seen.add(mac)
            c = _build(mac, e.get("address"), e.get("intf"), ul_by_mac.get(mac))
            clients.append(c)
            vlan_counts[c["vlan"]] = vlan_counts.get(c["vlan"], 0) + 1

        # user_list-only devices (e.g. wireless clients not in ARP)
        for mac, u in ul_by_mac.items():
            if mac in seen:
                continue
            seen.add(mac)
            c = _build(mac, u.get("userIp"), None, u)
            clients.append(c)
            vlan_counts[c["vlan"]] = vlan_counts.get(c["vlan"], 0) + 1

        # ── Physical ports ────────────────────────────────────────────────
        ps = await self._getx("port_status", no_parse=False)
        ports = ps.get("List", []) if isinstance(ps, dict) else []

        # ── Live WAN throughput ───────────────────────────────────────────
        flow_if = await self._getx("flow", {"func": "interface_info"})
        wan_flow = flow_if if isinstance(flow_if, dict) else {}
        # flow interface_info is double-nested: {count, data:{wan,wan1}} — the
        # _getx helper strips the outer .data, so unwrap the inner one here.
        if isinstance(wan_flow.get("data"), dict):
            wan_flow = wan_flow["data"]

        # ── Per-WAN IP detail ─────────────────────────────────────────────
        ipinfo = await self._getx("ipinfo") or {}

        # ── Internet up/down ──────────────────────────────────────────────
        nc = await self._getx("networkConnect", no_parse=False)
        internet_up = bool(nc and str(nc.get("connnected")).lower() == "true")

        # ── Topology (APs / switches) ─────────────────────────────────────
        topo_raw = await self._getx("local_topology",
                                   {"fromcache": "true", "caller": "eweb"})
        topo = []
        if isinstance(topo_raw, dict) and isinstance(topo_raw.get("topo"), dict):
            self._flatten_topology(topo_raw["topo"], topo)

        # ── WAN uplinks + sysinfo ─────────────────────────────────────────
        mllb_live = await self._get("mllb", {"getype": "0"})
        mllb_cfg = await self._cfg("mllb")
        mllb = mllb_live or (mllb_cfg.get("data", mllb_cfg)
                             if isinstance(mllb_cfg, dict) else {}) or {}
        wan_lines = mllb.get("master_list", []) if isinstance(mllb, dict) else []
        active_wan = mllb.get("intf") if isinstance(mllb, dict) else None

        sysinfo = await self._get("sysinfo") or {}

        # ── PPPoE connection log per WAN line (drop tracking) ──────────────
        # Fully guarded: a failure here must never take down the whole update.
        pppoe = {}
        _pppoe_debug = {}
        try:
            for line in wan_lines:
                ifn = line.get("ifname")
                if not ifn:
                    continue
                raw = await self.api.raw_cmd(
                    "devSta.get", "pppoeLog",
                    data={"intf_name": [ifn]}, no_parse=False)
                _pppoe_debug[ifn] = repr(raw)[:250]
                body = raw.get("data") if isinstance(raw, dict) else None
                entries = None
                if isinstance(body, dict) and isinstance(body.get(ifn), list):
                    entries = body[ifn]
                elif isinstance(raw, dict) and isinstance(raw.get(ifn), list):
                    entries = raw[ifn]
                if entries:
                    try:
                        pppoe[ifn] = self._parse_pppoe(entries)
                    except Exception as err:  # noqa: BLE001
                        _pppoe_debug[ifn] = f"parse error: {err}"
        except Exception as err:  # noqa: BLE001
            _pppoe_debug["_error"] = str(err)
            _LOGGER.warning("Reyee pppoe read failed: %s", err)

        # ── WiFi SSIDs from the AC controller (read-only) ─────────────────
        # Mirror the exact call the deep probe uses (data=None), fully guarded.
        ssids = []
        _ssid_debug = ""
        try:
            wireless_raw = await self.api.raw_cmd("acConfig.get", "wireless")
            _ssid_debug = repr(wireless_raw)[:400]
            wireless = None
            if isinstance(wireless_raw, dict):
                inner = wireless_raw.get("data")
                wireless = inner if isinstance(inner, dict) else wireless_raw
            if isinstance(wireless, dict) and wireless.get("ssidList"):
                for s_ in wireless.get("ssidList", []):
                    ssids.append({
                        "name": s_.get("ssidName"),
                        "enabled": s_.get("enable") == "true",
                        "hidden": s_.get("ishidden") == "true",
                        "vlan": s_.get("vlanId"),
                        "guest": s_.get("guest") == "true",
                        "band": s_.get("relatedRadio"),
                        "wlan_id": s_.get("wlanId"),
                    })
        except Exception as err:  # noqa: BLE001
            _ssid_debug = f"read error: {err}"
            _LOGGER.warning("Reyee wireless read failed: %s", err)
        _LOGGER.info("Reyee [v1.11.4] parsed %d ssids", len(ssids))

        # ── WiFi rate limit (wqos) + access-control blocks (read, guarded) ─
        rate_limit = {"ul": "0", "dl": "0"}
        try:
            wq = await self.api.raw_cmd("acConfig.get", "wqos", data={"groupId": "0"})
            wqd = wq.get("data") if isinstance(wq, dict) else None
            if isinstance(wqd, list) and wqd:
                wqd = wqd[0]
            if isinstance(wqd, dict) and isinstance(wqd.get("ap_persta"), dict):
                ap = wqd["ap_persta"]
                rate_limit = {"ul": ap.get("ul", "0"), "dl": ap.get("dl", "0")}
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Reyee wqos read failed: %s", err)

        led_on = True
        led_off_serials = set()
        led_default_close = False
        try:
            led = await self.api.raw_cmd("acConfig.get", "devLed", data={"groupId": "0"})
            ledd = led.get("data") if isinstance(led, dict) else None
            if isinstance(ledd, dict):
                led_default_close = ledd.get("led_all") == "close"
                led_on = not led_default_close
                for item in ledd.get("list", []) or []:
                    if isinstance(item, dict) and item.get("sn"):
                        # With default 'restore', a listed 'close' means that
                        # device is OFF. With default 'close', a listed
                        # 'restore' means that device is ON (others off).
                        if item.get("led_all") == "close":
                            led_off_serials.add(item["sn"])
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Reyee devLed read failed: %s", err)

        # Authoritative controllable-device list for LED targeting —
        # APs come from enetCap (the full AP list), gateway from sysinfo.
        # All auto-detected from the router, never hardcoded.
        led_devices = []
        try:
            name_by_sn = {n.get("sn"): n.get("name") for n in topo if n.get("sn")}
            type_by_sn = {n.get("sn"): n.get("type") for n in topo if n.get("sn")}

            # Gateway (the EG box itself)
            gw_sn = sysinfo.get("serial_num")
            if gw_sn:
                led_devices.append({
                    "sn": gw_sn,
                    "name": name_by_sn.get(gw_sn) or "Gateway",
                    "type": "gateway",
                })

            # All access points from enetCap
            enet = await self.api.raw_cmd(
                "devSta.get", "enetCap",
                data={"obj": {"rg_device": {"wireless": ["ap_sta_max"]}}, "sn": []})
            enetd = enet.get("data") if isinstance(enet, dict) else None
            rows = enetd.get("data") if isinstance(enetd, dict) else None
            for item in (rows or []):
                sn = item.get("sn")
                if sn and sn != gw_sn:
                    led_devices.append({
                        "sn": sn,
                        "name": name_by_sn.get(sn),
                        "type": type_by_sn.get(sn) or "AP",
                    })
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Reyee enetCap read failed: %s", err)

        blocks = []
        try:
            ac = await self.api.raw_cmd("devConfig.get", "access_ctrl")
            acd = ac.get("data", ac) if isinstance(ac, dict) else {}
            for r in (acd.get("list", []) if isinstance(acd, dict) else []):
                if r.get("target") == "REJECT":
                    blocks.append({
                        "mac": r.get("mac"), "name": r.get("ruleName"),
                        "uuid": r.get("uuid"), "enabled": r.get("enable") == "1",
                    })
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Reyee access_ctrl read failed: %s", err)

        # port forwards + flow control (confirmed writable)
        pm = await self._cfg("port_mapping")
        pm_data = pm.get("data", pm) if isinstance(pm, dict) else {}
        port_forwards = pm_data.get("portMapping", []) if isinstance(pm_data, dict) else []
        fc = await self._cfg("flowctrl")
        flowctrl = fc.get("data", fc) if isinstance(fc, dict) else {}

        return {
            "clients": clients,
            "vlans": vlans,
            "vlan_counts": vlan_counts,
            "ports": ports,
            "wan_flow": wan_flow,
            "ipinfo": ipinfo,
            "internet_up": internet_up,
            "topology": topo,
            "wan_lines": wan_lines,
            "active_wan": active_wan,
            "mllb": mllb,
            "sysinfo": sysinfo,
            "wan_ip": sysinfo.get("wan_ip"),
            "port_forwards": port_forwards,
            "flowctrl": flowctrl,
            "pppoe": pppoe,
            "ssids": ssids,
            "ssids_debug": _ssid_debug,
            "pppoe_debug": _pppoe_debug,
            "rate_limit": rate_limit,
            "blocks": blocks,
            "led_on": led_on,
            "led_off_serials": sorted(led_off_serials),
            "led_default_close": led_default_close,
            "led_devices": led_devices,
            "switches": await self._read_switches(),
            "features": self._slow["features"],
            "vpn": self._slow["vpn"],
            "neighbors": self._slow["neighbors"],
        }

    # ── Slow reads ───────────────────────────────────────────────────────
    async def _read_slow(self):
        feats = {}
        for key, kind, field, on_v, _off, _n, _i in FEATURES:
            cfg = _peel(await self._ac(key, {"groupId": "0"}) if kind == "ac"
                        else await self._cfgx(key))
            if isinstance(cfg, dict) and field in cfg:
                feats[key] = {"on": str(cfg.get(field)) == on_v, "cfg": cfg}
        vpn = {}
        for key, _n in VPN_SERVERS:
            cfg = _peel(await self._cfgx(key))
            if isinstance(cfg, dict) and "enable" in cfg:
                vpn[key] = str(cfg.get("enable")) == "1"
        names = {}
        nt = _peel(await self._cfgx("net_tml"))
        if isinstance(nt, dict):
            for e in nt.get("nt_info", []) or []:
                nm = _clean_name(e.get("name"))
                if nm and e.get("mac"):
                    names[_norm_mac(e["mac"])] = nm
        neigh, seen = [], set()
        for mod in ("neighbor", "esw_neighbor"):
            r = _peel(await self._getx(mod, no_parse=False))
            for e in (r.get("neighbor", []) if isinstance(r, dict) else []):
                key = e.get("devSN") or e.get("ip")
                if key and key not in seen:
                    seen.add(key)
                    neigh.append({"sn": e.get("devSN"), "ip": e.get("ip"),
                                  "mac": e.get("devMac"), "model": e.get("deviceType"),
                                  "role": e.get("role"),
                                  "is_switch": is_switch_model(e.get("deviceType"),
                                                               e.get("role"))})
        # managed-switch port descriptions (config, changes rarely)
        desc = {}
        for n in neigh:
            if n.get("is_switch") and n.get("ip"):
                cfg = _peel(await self._relay("devConfig.get", "port_phy", n["ip"]))
                rows = cfg.get("data") if isinstance(cfg, dict) else None
                if isinstance(rows, list):
                    desc[n["ip"]] = {r.get("lpid"): (r.get("description") or "").strip()
                                     for r in rows if isinstance(r, dict)}
        if desc:
            self._slow["port_desc"] = desc
        # keep last good values if a read failed this round
        self._slow["features"] = feats or self._slow["features"]
        self._slow["vpn"] = vpn or self._slow["vpn"]
        self._slow["net_names"] = names or self._slow["net_names"]
        self._slow["neighbors"] = neigh or self._slow["neighbors"]

    async def _relay(self, method, module, ip, data=None, no_parse=True):
        """Call a module on a managed switch through the gateway (remoteIp)."""
        try:
            r = await asyncio.wait_for(self.api.raw_cmd(
                method, module, data=data, no_parse=no_parse,
                extra={"remoteIp": ip}), 8)
        except Exception:  # noqa: BLE001
            return None
        return None if _is_error(r) else r

    async def _read_switches(self):
        """Per-port live status of each managed (MSW) switch via the gateway."""
        out = {}
        own = {e.data.get("host") for e in self.hass.config_entries.async_entries(DOMAIN)
               if e.data.get("kind") == "switch"} if self.hass else set()
        own |= set(getattr(self, "switch_coords", None) or {})
        for n in self._slow.get("neighbors", []):
            ip, sn = n.get("ip"), n.get("sn")
            if not (n.get("is_switch") and ip and sn) or ip in own:
                continue
            ps = _peel(await self._relay("devSta.get", "port_show", ip))
            if not isinstance(ps, list):
                # ES-series switches don't answer the MSW relay — keep last value
                if sn in self._switches:
                    out[sn] = self._switches[sn]
                continue
            desc = self._slow.get("port_desc", {}).get(ip, {})
            ports = []
            for p in ps:
                if not isinstance(p, dict) or not str(p.get("nm", "")).startswith(("Gi", "Te", "Fa", "Ag")):
                    continue
                if str(p.get("nm")).startswith("Ag") and not p.get("lk"):
                    continue          # unused aggregate groups
                lp = p.get("lp")
                ports.append({
                    "lpid": lp, "name": p.get("nm"),
                    "link": p.get("lk") == 1,
                    "speed": p.get("r_spd") or None,
                    "rx_rate": p.get("iRt"), "tx_rate": p.get("oRt"),
                    "rx_bytes": p.get("iBt"), "tx_bytes": p.get("oBt"),
                    "crc_errors": p.get("crc"), "pvid": p.get("pvid"),
                    "vlans": p.get("pmtVAct") or p.get("pmtV") or "",
                    "description": desc.get(lp) or "",
                })
            out[sn] = {"ip": ip, "sn": sn, "model": n.get("model"), "kind": "msw",
                       "ports": ports}
        self._switches = out
        return out

    async def async_set_feature(self, key, on):
        spec = next(f for f in FEATURES if f[0] == key)
        _k, kind, field, on_v, off_v, _n, _i = spec
        cfg = _peel(await self._ac(key, {"groupId": "0"}) if kind == "ac"
                    else await self._cfgx(key))
        if not isinstance(cfg, dict):
            raise UpdateFailed(f"Could not read {key} before writing")
        out = {k: v for k, v in cfg.items() if k not in _STAMPS}
        out[field] = on_v if on else off_v
        if kind == "ac":
            await self.api.set_ac_config(key, out)
        else:
            await self.api.set_config(key, out)
        self._poll = 0          # re-read features on the next refresh
        await self.async_request_refresh()

    def switch_targets(self):
        """IPs of managed switches seen via topology + neighbour discovery."""
        out = {}
        for n in (self.data or {}).get("topology", []):
            if n.get("ip") and is_switch_model(n.get("model"), n.get("type")):
                out[n["ip"]] = n.get("model") or n.get("name")
        for n in self._slow.get("neighbors", []):
            if n.get("ip") and n.get("is_switch"):
                out[n["ip"]] = n.get("model")
        return out

    # ── Writes (unchanged) ───────────────────────────────────────────────
    async def async_set_primary_wan(self, ifname):
        cfg = await self._cfg("mllb")
        if not cfg:
            raise ReyeeConnError("Could not read current WAN config")
        data = cfg.get("data", cfg) if isinstance(cfg, dict) else {}
        await self.api.set_config("mllb", build_master_swap_payload(data, ifname))
        await self.async_request_refresh()

    async def async_set_forced_switch(self, enabled):
        cfg = await self._cfg("mllb")
        if not cfg:
            raise ReyeeConnError("Could not read current WAN config")
        data = cfg.get("data", cfg) if isinstance(cfg, dict) else {}
        stamps = ("version", "configTime", "currentTime", "configId")
        payload = {k: v for k, v in data.items() if k not in stamps}
        payload["backup_discon"] = "1" if enabled else "0"
        await self.api.set_config("mllb", payload)
        await self.async_request_refresh()

    async def async_add_port_forward(self, rule):
        cfg = await self._cfg("port_mapping")
        if not cfg:
            raise ReyeeConnError("Could not read current port_mapping")
        data = cfg.get("data", cfg) if isinstance(cfg, dict) else {}
        await self.api.set_config("port_mapping", build_portmap_add(data, rule))
        await self.async_request_refresh()

    async def async_remove_port_forward(self, rule_name):
        cfg = await self._cfg("port_mapping")
        if not cfg:
            raise ReyeeConnError("Could not read current port_mapping")
        data = cfg.get("data", cfg) if isinstance(cfg, dict) else {}
        await self.api.set_config("port_mapping", build_portmap_remove(data, rule_name))
        await self.async_request_refresh()

    async def async_set_flow_control(self, enabled):
        cfg = await self._cfg("flowctrl")
        if not cfg:
            raise ReyeeConnError("Could not read current flowctrl")
        data = cfg.get("data", cfg) if isinstance(cfg, dict) else {}
        await self.api.set_config("flowctrl", build_flowctrl_toggle(data, enabled))
        await self.async_request_refresh()

    async def async_set_rate_limit(self, up_kbps, down_kbps):
        """Set the global wireless per-station rate limit (0 = unlimited)."""
        await self.api._cmd_write(
            "acConfig.update", "wqos", build_rate_limit(up_kbps, down_kbps))
        _LOGGER.info("Reyee: wifi rate limit set up=%s dl=%s", up_kbps, down_kbps)
        await self.async_request_refresh()

    async def async_set_leds(self, on: bool):
        """Turn all device LEDs on (restore) or off (close)."""
        await self.api.set_ac_config("devLed", build_led_toggle(on))
        _LOGGER.info("Reyee: LEDs %s", "on" if on else "off")
        await self.async_request_refresh()

    async def async_set_device_led(self, serial: str, on: bool):
        """Turn one device's LED on/off, preserving the state of the others.

        Model: top-level led_all='restore' (all on by default) with a list of
        exceptions set to 'close' (off). We keep the full off-set and rewrite it.
        """
        off = set((self.data or {}).get("led_off_serials", []))
        if on:
            off.discard(serial)
        else:
            off.add(serial)
        await self.api.set_ac_config("devLed", build_led_per_device(sorted(off)))
        _LOGGER.info("Reyee: LED %s -> %s", serial, "on" if on else "off")
        await self.async_request_refresh()

    async def async_block_device(self, mac, name=None):
        """Block a device (MAC) from reaching the internet."""
        rule_name = (name or mac).replace(":", "")[:32]
        await self.api._cmd_write(
            "devConfig.add", "access_ctrl", build_block_rule(mac, rule_name))
        _LOGGER.info("Reyee: blocked %s", mac)
        await self.async_request_refresh()

    async def async_unblock_device(self, mac):
        """Remove the block rule(s) for a MAC by uuid."""
        ac = await self.api.raw_cmd("devConfig.get", "access_ctrl")
        acd = ac.get("data", ac) if isinstance(ac, dict) else {}
        uuids = [r.get("uuid") for r in (acd.get("list", []) if isinstance(acd, dict) else [])
                 if r.get("mac", "").lower() == mac.lower() and r.get("uuid")]
        if not uuids:
            raise ReyeeConnError(f"No block rule found for {mac}")
        await self.api._cmd_write("devConfig.del", "access_ctrl", {"uuid": uuids})
        _LOGGER.info("Reyee: unblocked %s (%d rules)", mac, len(uuids))
        await self.async_request_refresh()

    async def async_set_ssid_enabled(self, wlan_id, enabled: bool):
        """Enable/disable a WiFi SSID via the AC controller."""
        cfg = await self._ac("wireless", {"groupId": "0"})
        if not cfg:
            raise ReyeeConnError("Could not read current wireless config")
        payload = build_ssid_toggle(cfg, wlan_id, enabled)
        await self.api.set_ac_config("wireless", payload)
        _LOGGER.info("Reyee: SSID wlanId=%s enabled=%s", wlan_id, enabled)
        await self.async_request_refresh()
