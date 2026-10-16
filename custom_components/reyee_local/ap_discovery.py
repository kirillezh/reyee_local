"""Access-point scan — button "Reyee Scan Access Points" / service
reyee_local.scan_access_points.

READ-ONLY. For every AP the router knows it:
  1. reads AP modules through the router with remoteIp=<AP ip> (the same relay
     the router's own web UI uses for APs and switches),
  2. reads the router's controller-side modules for that AP (data {"sn": [sn]}),
  3. reads the router's enetCap capability tree for that AP.
Module names come from a list of known Reyee AP modules plus every module the
router's own eWeb JavaScript uses. Only getters are called; anything that looks
like an action (reboot, reset, scan, set …) is skipped.

Writes /config/reyee_aps.json and shows a short notification.
"""
import asyncio
import json
import logging
import re

import aiohttp
from homeassistant.components.persistent_notification import async_create

from .discovery import _DENY_RE, _classify, _collect_js, _extract, _write

_LOGGER = logging.getLogger(__name__)

# Known / likely Reyee AP (EAP/RAP) module names — wrong guesses just return nothing.
AP_MODULES = (
    "sysinfo", "devSta", "summaryInfo", "local_topology", "network", "networkConnect",
    "wireless", "wirelessInfo", "wireless_info", "wlan", "wlanInfo", "wifi", "wifiInfo",
    "wifiBase", "wifi_base", "ssid", "ssidInfo", "radio", "radioInfo", "radio_info",
    "rf", "rfInfo", "rf_info", "rfConfig", "rfParam", "channel", "channelInfo",
    "chan_util", "chanUtil", "power", "txpower", "txPower", "country",
    "band_steering", "bandSteering", "bandsteer", "roam", "roaming", "wqos",
    "sta_list", "staList", "staInfo", "sta_info", "user_list", "userList",
    "client_list", "clientList", "wifi_client", "assoc_sta",
    "neighbor_ap", "neighborAp", "nbr_ap", "interference", "rogue_ap",
    "mesh", "meshInfo", "easymesh", "wds", "wdsInfo", "ap_mode", "apMode", "workmode",
    "workMode", "uplink", "port_status", "ethernet", "eth_info", "lan", "vlan",
    "devLed", "devStaLed", "led", "timeReboot", "syslog", "flow", "wifi_flow",
    "apInfo", "ap_info", "apList", "ap_list", "apGroup", "wifiSchedule", "wifi_timer",
    "guest", "guestWifi", "portal", "access_ctrl", "acl", "blacklist",
    "airtime", "wmm", "ofdma", "mumimo", "beamforming", "ft", "kvr", "11k", "11v",
)

# enetCap obj trees worth asking the router for, per AP
ENETCAP_OBJS = (
    {"rg_device": {"wireless": ["ap_sta_max"]}},
    {"rg_device": {"wireless": []}},
    {"rg_device": {}},
    {"rg_device": {"wireless": ["radio", "channel", "power", "band", "ssid_max"]}},
)

_WIFI_RE = re.compile(r"ap|wifi|wlan|wireless|radio|ssid|rf|sta|roam|mesh|wds|chan|band|"
                      r"led|power|client|guest|portal|airtime", re.I)

CALL_TIMEOUT = 6
AP_BUDGET = 180           # seconds per AP
MAX_STRIKES = 6


def _access_points(coordinator):
    """[(ip, sn, name, model)] for every AP in the router's topology."""
    out, seen = [], set()
    for n in (coordinator.data or {}).get("topology", []):
        t = str(n.get("type") or "").upper()
        if n.get("depth", 0) == 0 or t not in ("EAP", "AP", "RAP"):
            continue
        key = n.get("sn") or n.get("ip")
        if key and key not in seen:
            seen.add(key)
            out.append((n.get("ip"), n.get("sn"), n.get("name"), n.get("model")))
    return out


async def _call(api, method, module, data=None, no_parse=True, extra=None):
    try:
        return await asyncio.wait_for(api.raw_cmd(
            method, module, data=data, no_parse=no_parse, extra=extra), CALL_TIMEOUT)
    except asyncio.TimeoutError:
        return {"_error": "timeout"}
    except Exception as err:  # noqa: BLE001
        return {"_error": str(err)}


def _is_timeout(body):
    return isinstance(body, dict) and "timeout" in str(body.get("_error", "")).lower()


async def _probe_ap(api, ip, sn, modules):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + AP_BUDGET
    relay, controller, skipped = {}, {}, []
    strikes = gw_strikes = 0

    for mod in modules:
        if _DENY_RE.search(mod):
            skipped.append(mod)
            continue
        if loop.time() > deadline:
            relay["_note"] = "time budget reached"
            break
        # 1) on the AP itself, through the router
        if ip and strikes < MAX_STRIKES:
            res = {}
            for method in ("devSta.get", "devConfig.get"):
                for no_parse in (True, False):
                    body = await _call(api, method, mod, no_parse=no_parse,
                                       extra={"remoteIp": ip})
                    strikes = strikes + 1 if _is_timeout(body) else 0
                    kind, payload = _classify(body)
                    if kind == "data":
                        res[method] = {"noParse": no_parse, "data": payload}
                        break
                if method in res:
                    break
            if res:
                relay[mod] = res
        # 2) on the router, scoped to this AP's serial (controller view)
        if sn and _WIFI_RE.search(mod) and gw_strikes < MAX_STRIKES:
            for method, data in (("devSta.get", {"sn": [sn]}), ("devConfig.get", {"sn": [sn]}),
                                 ("acConfig.get", {"groupId": "0", "sn": sn})):
                body = await _call(api, method, mod, data=data)
                gw_strikes = gw_strikes + 1 if _is_timeout(body) else 0
                kind, payload = _classify(body)
                if kind == "data":
                    controller.setdefault(mod, {})[method] = {"request_data": data,
                                                              "data": payload}
    caps = {}
    if sn:
        for obj in ENETCAP_OBJS:
            body = await _call(api, "devSta.get", "enetCap", data={"obj": obj, "sn": [sn]})
            kind, payload = _classify(body)
            if kind == "data":
                caps[json.dumps(obj)] = payload
    return {"via_ap": relay, "via_router_for_this_ap": controller,
            "enetCap": caps, "skipped_action_modules": skipped,
            "relay_stopped": strikes >= MAX_STRIKES}


async def run_ap_scan(hass, coordinator):
    api = coordinator.api
    nid = "reyee_ap_scan"
    aps = _access_points(coordinator)
    if not aps:
        async_create(hass, "The router didn't list any access points.",
                     title="Reyee AP Scan", notification_id=nid)
        return
    async_create(hass, f"Found {len(aps)} access point(s). Reading the router's module "
                       "list first… this takes a few minutes, read-only.",
                 title="Reyee AP Scan", notification_id=nid)

    # every module name the router's own web UI uses
    js_modules = {}
    try:
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=jar) as sess:
            _base, files, _pages, _login = await _collect_js(api, sess)
        js_modules, _m, _p = _extract(files)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Reyee AP scan: eWeb JS read failed: %s", err)
    api.sid = None                     # our extra login may have replaced the session
    try:
        await api.login()
    except Exception as err:  # noqa: BLE001
        async_create(hass, f"Couldn't log in to the router: {err}",
                     title="Reyee AP Scan", notification_id=nid)
        return

    wifi_js = sorted(m for m in js_modules if _WIFI_RE.search(m))
    modules = list(dict.fromkeys([*AP_MODULES, *wifi_js]))

    report = {"router": api.host, "modules_tried": len(modules),
              "modules_from_router_js": sorted(js_modules), "access_points": {}}
    lines = []
    for i, (ip, sn, name, model) in enumerate(aps, 1):
        label = f"{name or sn or ip} ({model or 'AP'}, {ip or 'no IP'})"
        async_create(hass, f"Reading AP {i} of {len(aps)}: {label}…",
                     title="Reyee AP Scan", notification_id=nid)
        res = await _probe_ap(api, ip, sn, modules)
        ok_ap = sorted(k for k in res["via_ap"] if not k.startswith("_"))
        ok_gw = sorted(res["via_router_for_this_ap"])
        report["access_points"][ip or sn] = {"name": name, "model": model, "sn": sn,
                                             "ip": ip, **res}
        lines.append(f"- **{label}**: {len(ok_ap)} on the AP, {len(ok_gw)} via the router"
                     + (f" — {', '.join(ok_ap[:15])}{'…' if len(ok_ap) > 15 else ''}"
                        if ok_ap else ""))
    path = hass.config.path("reyee_aps.json")
    await hass.async_add_executor_job(
        _write, path, json.dumps(report, ensure_ascii=False, indent=2, default=str))
    async_create(hass, "Done.\n\n" + "\n".join(lines) +
                 "\n\nSend back **one file**: `/config/reyee_aps.json`",
                 title="Reyee AP Scan", notification_id=nid)
