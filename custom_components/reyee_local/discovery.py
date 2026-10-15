"""
Module discovery — service reyee_local.discover_modules / button.

1. Downloads the router's own eWeb JavaScript (over the same HTTPS session the
   integration already uses, cert checks off) and extracts every API module
   name, the methods used with it, and a code snippet showing the request.
2. Probes every discovered module with READ-ONLY getters only
   (devSta.get / devConfig.get / acConfig.get) — never a setter.
3. Writes the full results to /config/reyee_discovery.json and a readable
   summary to /config/reyee_discovery.md, plus a short HA notification.
"""
import asyncio
import json
import logging
import re
import time
from urllib.parse import urljoin, urlparse

import aiohttp
import async_timeout
from homeassistant.components.persistent_notification import async_create

from .api import _encrypt_password

_LOGGER = logging.getLogger(__name__)

# Modules the integration already reads or writes.
KNOWN = {
    "arp", "user_list", "devRemark", "flow", "network", "port_status",
    "ipinfo", "networkConnect", "local_topology", "mllb", "sysinfo",
    "pppoeLog", "enetCap", "wireless", "wqos", "devLed", "access_ctrl",
    "port_mapping", "flowctrl",
}

GETTERS = ("devSta.get", "devConfig.get", "acConfig.get")
_METHOD_RE = re.compile(r"\b(?:dev|ac|uf)(?:Sta|Config|Cmd|Cap)\.[a-zA-Z]+\b")
_MODULE_RES = (
    re.compile(r"""module["']?\s*:\s*["'`]([A-Za-z][A-Za-z0-9_\-]{1,40}(?:\.[A-Za-z]{2,12})?)["'`]"""),
    re.compile(r"""["']module["']\s*,\s*["']([A-Za-z][A-Za-z0-9_\-]{1,40}(?:\.[A-Za-z]{2,12})?)["']"""),
)

# Never call these, even with a getter — they name actions, not state.
_DENY_RE = re.compile(
    r"reboot|reset|restore|factory|delete|_del|del_|Del$|kick|flush|move|upgrade|update|"
    r"_add|add_|Add$|set|start|stop|clear|import|export|check|scan|speedtest|batch|"
    r"(?:^|_)ping|trace|kill|(?:^|_)bind|login|logout|passw|save|apply|commit|load|opera|alarm|"
    r"(?:^|_)(?:run|exec|cmd|shell)(?:$|_)|format|disk|sql|xlog|test",
    re.I)
_SAFE_SUFFIX = ("", "get")
_DATA_LIT_RE = re.compile(r"""data\s*:\s*\{([^{}]{1,300})\}""")
_KV_RE = re.compile(r"""([A-Za-z_][\w]*)\s*:\s*["']([^"']{0,60})["']""")
_SAFE_VAL_RE = re.compile(r"(?:^|_)(get|list|query|show|read|status|info)(?:_|$)", re.I)
# Quoted or unquoted src/href on <script> and <link> (minified Vue HTML drops quotes)
_SCRIPT_SRC_RE = re.compile(
    r"""<(?:script|link)\b[^>]*?\b(?:src|href)\s*=\s*["']?([^"'\s>]+\.js)(?:\?[^"'\s>]*)?["']?""",
    re.I)
_JS_REF_RE = re.compile(r"""["'`]((?:\.{0,2}/)?[A-Za-z0-9_\-./]*?[A-Za-z0-9_\-]+\.js)["'`]""")
# webpack chunk maps: {12:"a1b2c3d4", "chunk-x":"deadbeef"}
_CHUNK_PAIR_RE = re.compile(r"""["']?([A-Za-z0-9_\-]+)["']?\s*:\s*["']([0-9a-f]{6,20})["']""")
_CHUNK_PREFIX_RE = re.compile(r"""["']((?:static/)?js/[A-Za-z0-9_\-/]*)["']\s*\+""")

MAX_JS_FILES = 400
MAX_FETCHES = 4000
SNIPPET = 260


async def _fetch_text(session, url, timeout=8, headers=None):
    try:
        async with async_timeout.timeout(timeout):
            async with session.get(url, ssl=False, headers=headers) as resp:
                if resp.status != 200:
                    return None
                return await resp.text(errors="ignore")
    except Exception:  # noqa: BLE001
        return None


async def _eweb_login(api, session, base):
    """Log in like the browser does; return (token, sid, raw_reply)."""
    attempts = [
        {"username": api.username, "password": _encrypt_password(api.password),
         "encry": True, "time": int(time.time()), "limit": False},
        {"username": api.username, "password": api.password},
    ]
    last = None
    for params in attempts:
        try:
            async with async_timeout.timeout(10):
                async with session.post(
                    f"{base}/cgi-bin/luci/api/auth",
                    json={"method": "login", "params": params}, ssl=False,
                ) as resp:
                    last = await resp.json(content_type=None)
        except Exception as err:  # noqa: BLE001
            last = {"_error": str(err)}
            continue
        data = (last or {}).get("data") or {}
        if isinstance(data, dict) and (data.get("token") or data.get("sid")):
            return data.get("token") or data.get("sid"), data.get("sid"), last
    return None, None, last


async def _collect_js(api, session):
    """Log in to the eWeb, load the real app page and crawl every JS file."""
    base = api._base or await api._resolve_base()
    host = urlparse(base).netloc
    token, sid, login_reply = await _eweb_login(api, session, base)
    token = token or ""
    hdrs = None

    pages = [
        f"/cgi-bin/luci/;stok={token}",
        f"/cgi-bin/luci/;stok={token}/",
        f"/cgi-bin/luci/;stok={token}/eweb/",
        f"/cgi-bin/luci/;stok={token}/home",
        "/", "/cgi-bin/luci/",
    ] if token else ["/", "/cgi-bin/luci/"]
    # Common Vue/webpack entry bundles under the eWeb folder
    guess_js = [
        f"/luci-static/eweb-egw/{d}{f}"
        for d in ("static/js/", "js/", "static/", "")
        for f in ("app.js", "main.js", "index.js", "manifest.js", "vendor.js",
                  "chunk-vendors.js", "runtime.js")
    ]
    queue, seen, files = [], set(), {}

    def _add(ref, rel_to):
        url = urljoin(rel_to, ref)
        p = urlparse(url)
        if p.netloc != host or not p.path.endswith(".js"):
            return
        url = f"{p.scheme}://{p.netloc}{p.path}"
        if url not in seen:
            seen.add(url)
            queue.append(url)

    page_dump, seen_html = {}, set()
    for page in pages:
        html = await _fetch_text(session, base + page, headers=hdrs)
        if not html:
            continue
        key = page.replace(token, "TOKEN") if token else page
        h = hash(html)
        if h not in seen_html:
            seen_html.add(h)
            page_dump[key] = html[:60000]
        for src in _SCRIPT_SRC_RE.findall(html):
            _add(src, base + page)
        for ref in _JS_REF_RE.findall(html):          # inline script references
            _add(ref, base + page)
        # Follow in-page navigation targets (post-login redirect, eWeb folders)
        for tgt in set(re.findall(r"""["'](/(?:cgi-bin/luci|luci-static)/[^"'\s?#]*)""", html)):
            if not tgt.endswith((".js", ".css", ".ico", ".png", ".jpg", ".svg", ".gif")) \
                    and tgt not in pages and len(pages) < 40:
                pages.append(tgt)
    for g in guess_js:
        _add(g, base + "/")

    prefixes = set()
    runtime_dump = {}
    attempts = 0
    crawl_end = asyncio.get_running_loop().time() + 240   # 4 min max crawling
    while queue and len(files) < MAX_JS_FILES and attempts < MAX_FETCHES \
            and asyncio.get_running_loop().time() < crawl_end:
        attempts += 1
        url = queue.pop(0)
        text = await _fetch_text(session, url, headers=hdrs)
        if not text:
            continue
        files[url] = text
        for ref in _JS_REF_RE.findall(text):
            _add(ref, url)
            if not ref.startswith(("/", "http")):
                _add("/" + ref.lstrip("./"), base + "/")
        # Guess webpack lazy chunks from the runtime/manifest hash map
        fname = url.rsplit("/", 1)[-1]
        local_prefixes = set(_CHUNK_PREFIX_RE.findall(text))
        prefixes.update(local_prefixes)
        is_runtime = fname.startswith(("manifest", "runtime")) or "webpackJsonp" in text[:3000]
        if local_prefixes or fname.startswith(("manifest", "runtime")):
            runtime_dump[fname] = text[:20000]
            dirs = {url.rsplit("/", 1)[0] + "/"} | {
                "/" + pre.lstrip("/") for pre in prefixes}
            dirs |= {"/luci-static/eweb-egw/static/js/", "/luci-static/eweb-egw/js/"}
            # Webpack 3: ({1:"advanced",2:"wifi"}[e]||e) + {0:"<hash>",1:"<hash>"}[e] + ".js"
            names = {}
            for obj in re.findall(r"\{((?:\s*\d+\s*:\s*\"[^\"]*\"\s*,?)+)\}", text):
                pairs = re.findall(r"(\d+)\s*:\s*\"([^\"]*)\"", obj)
                if pairs and not all(re.fullmatch(r"[0-9a-f]{6,32}", v) for _, v in pairs):
                    names.update(pairs)
            for cid, h in _CHUNK_PAIR_RE.findall(text):
                if cid in names:
                    for d in dirs:
                        for cand in (f"{names[cid]}{h}.js", f"{names[cid]}.{h}.js"):
                            _add(d + cand, base + "/")
                for d in dirs:
                    for cand in (f"{cid}{h}.js", f"{cid}.{h}.js", f"{h}.js",
                                 f"chunk-{cid}.{h}.js"):
                        _add(d + cand, base + "/")

    login_info = {"got_token": bool(token),
                  "reply_keys": sorted(((login_reply or {}).get("data") or {}).keys())
                  if isinstance((login_reply or {}).get("data"), dict) else str(login_reply)[:200]}
    login_info["runtime_files"] = runtime_dump
    return base, files, page_dump, login_info


def _extract(files):
    modules = {}
    methods = set()
    api_paths = set()
    for url, text in files.items():
        fname = url.rsplit("/", 1)[-1]
        methods.update(_METHOD_RE.findall(text))
        api_paths.update(re.findall(r"/cgi-bin/luci/[A-Za-z0-9_/\-]+", text))
        for rx in _MODULE_RES:
            for m in rx.finditer(text):
                full = m.group(1)
                name, _, suffix = full.partition(".")
                i = m.start()
                ctx = text[max(0, i - SNIPPET): i + SNIPPET]
                rec = modules.setdefault(name, {"files": set(), "methods": set(),
                                                "suffixes": set(), "snippets": [],
                                                "data_variants": []})
                rec["files"].add(fname)
                rec["suffixes"].add(suffix)
                rec["methods"].update(_METHOD_RE.findall(ctx))
                if len(rec["snippets"]) < 4:
                    rec["snippets"].append(re.sub(r"\s+", " ", ctx))
                # read-style data literal right after this module name
                after = text[m.end(): m.end() + 320]
                dm = _DATA_LIT_RE.match(after.lstrip(", "))
                if dm and suffix in _SAFE_SUFFIX:
                    kv = dict(_KV_RE.findall(dm.group(1)))
                    vals = list(kv.values())
                    if kv and any(_SAFE_VAL_RE.search(v) for v in vals) \
                            and not any(_DENY_RE.search(v) and not _SAFE_VAL_RE.search(v) for v in vals) \
                            and kv not in rec["data_variants"] and len(rec["data_variants"]) < 4:
                        rec["data_variants"].append(kv)
    return modules, methods, api_paths


def _unwrap(body):
    """Peel nested {code, id, data, error} JSON-RPC wrappers."""
    for _ in range(4):
        if isinstance(body, dict) and "data" in body and \
                set(body) <= {"code", "id", "data", "error", "rcode", "rmsg", "message"}:
            if body.get("rcode") and str(body["rcode"]) != "00000000":
                return body
            body = body["data"]
        else:
            break
    return body


def _classify(body):
    if isinstance(body, dict) and "_error" in body:
        return "error", body["_error"]
    inner = _unwrap(body)
    if inner in (None, "", {}, []) or (isinstance(inner, str) and not inner.strip()):
        return "empty", ""
    if isinstance(inner, dict) and inner.get("rcode") and str(inner["rcode"]) != "00000000":
        return "error", inner
    if isinstance(inner, dict) and set(inner) <= {"code", "msg", "error_code", "error_msg", "message"} \
            and str(inner.get("code", inner.get("error_code", "0"))) not in ("0", ""):
        return "error", inner
    if not isinstance(inner, dict) or inner is not body:
        return "data", inner
    if not isinstance(body, dict):
        return "other", body
    if "_error" in body:
        return "error", body["_error"]
    rcode = body.get("rcode")
    data = body.get("data")
    if isinstance(data, dict) and data.get("rcode") and str(data["rcode"]) != "00000000" \
            and len(data) <= 3:
        return "error", data
    if rcode and str(rcode) != "00000000":
        return "error", body
    if data in (None, {}, [], ""):
        real = {k: v for k, v in body.items()
                if k not in ("code", "id", "error", "rcode", "message", "rmsg", "data")}
        return ("data", real) if real else ("empty", body)
    return "data", data


CALL_TIMEOUT = 6          # seconds per probe call
DEVICE_BUDGET = 360       # max seconds probing one device (6 min)
MAX_STRIKES = 4           # timeouts before a getter is dropped for this device


async def _probe(api, modules):
    sem = asyncio.Semaphore(4)
    results = {}
    strikes = {g: 0 for g in GETTERS}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DEVICE_BUDGET

    async def call(method, name, data, no_parse):
        if strikes[method] >= MAX_STRIKES:
            return {"_error": "getter not answered by this device — skipped"}
        try:
            body = await asyncio.wait_for(
                api.raw_cmd(method, name, data=data, no_parse=no_parse), CALL_TIMEOUT)
        except asyncio.TimeoutError:
            body = {"_error": "timeout"}
        if isinstance(body, dict) and "_error" in body and \
                "timeout" in str(body["_error"]).lower():
            strikes[method] += 1
        elif strikes[method] < MAX_STRIKES:
            strikes[method] = 0
        return body

    async def one(name, rec):
        if _DENY_RE.search(name):
            results[name] = {"skipped": "action-type module — not called"}
            return
        if not (rec["suffixes"] & set(_SAFE_SUFFIX)):
            # Only ever written by the eWeb (x.set / x.add): read its saved
            # config only — never the runtime-state getter.
            order = ["devConfig.get", "acConfig.get"]
        else:
            seen_getters = [m for m in rec["methods"] if m.endswith(".get")]
            order = [g for g in GETTERS if g in seen_getters] + \
                    [g for g in GETTERS if g not in seen_getters]
        out = {}
        async with sem:
            for method in order:
                variants = [(None, False), (None, True)]
                if method.startswith("acConfig"):
                    variants.append(({"groupId": "0"}, False))
                variants += [(dv, True) for dv in rec.get("data_variants", [])]
                for data, no_parse in variants:
                    if loop.time() > deadline:
                        out["_note"] = "time budget reached — not probed"
                        results[name] = out
                        return
                    body = await call(method, name, data, no_parse)
                    kind, payload = _classify(body)
                    if kind == "data":
                        out[method] = {"noParse": no_parse, "request_data": data,
                                       "data": payload}
                        break
                    out.setdefault(method, {"result": kind,
                                            "detail": str(payload)[:300]})
                    if kind == "error" and "skipped" in str(payload):
                        break
        results[name] = out

    await asyncio.gather(*(one(n, r) for n, r in modules.items()))
    return results


def _write(path, content):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


async def run_discovery(hass, coordinator, api=None):
    """Scan the gateway (default) or another Reyee device when `api` is given."""
    target = api is not None
    api = api or coordinator.api
    tag = "_" + api.host.replace(".", "_").replace(":", "_") if target else ""
    nid = f"reyee_discovery{tag}"
    async_create(hass, f"Discovering Reyee modules on {api.host}… this can take a few minutes.",
                 title="Reyee Discovery", notification_id=nid)
    try:
        if not api.sid:
            await api.login()
    except Exception as err:  # noqa: BLE001
        async_create(hass, f"Couldn't log in to **{api.host}**: {err}\n\n"
                           "Check the IP, and pass `password` if this device uses a "
                           "different management password.",
                     title="Reyee Discovery", notification_id=nid)
        return

    jar = aiohttp.CookieJar(unsafe=True)   # keep cookies set by an IP host
    async with aiohttp.ClientSession(cookie_jar=jar) as sess:
        base, files, page_dump, login_info = await _collect_js(api, sess)
    # Our extra login may have replaced the integration's session — force re-auth
    api.sid = None
    modules, methods, api_paths = _extract(files)
    probe = await _probe(api, modules) if modules else {}

    with_data, writable_new, rows = [], [], []
    for name in sorted(modules, key=str.lower):
        rec = modules[name]
        pr = probe.get(name, {})
        got = [m for m, v in pr.items() if isinstance(v, dict) and "data" in v]
        setters = sorted({m for m in rec["methods"] if not m.endswith(".get")} |
                         {f".{x}" for x in rec["suffixes"] if x and x != "get"})
        known = name in KNOWN
        if got:
            with_data.append(name)
        if setters and not known:
            writable_new.append(name)
        rows.append(f"| {'✅' if known else '🆕'} `{name}` | {', '.join(got) or '—'} | "
                    f"{', '.join(setters) or '—'} |")

    report = {
        "router": base,
        "js_files_scanned": sorted(files),
        "eweb_login": login_info,
        "pages_fetched": page_dump,
        "methods_seen": sorted(methods),
        "api_paths_seen": sorted(api_paths),
        "modules": {
            n: {
                "already_in_integration": n in KNOWN,
                "methods_in_js": sorted(r["methods"]),
                "call_suffixes": sorted(x for x in r["suffixes"] if x),
                "read_data_variants": r["data_variants"],
                "files": sorted(r["files"]),
                "js_snippets": r["snippets"],
                "probe": probe.get(n, {}),
            } for n, r in sorted(modules.items())
        },
    }

    md = [
        "# Reyee module discovery", "",
        f"Router: {base}  ",
        f"JS files scanned: {len(files)}  ",
        f"Modules found in eWeb: {len(modules)}  ",
        f"Returned data on read: {len(with_data)}  ",
        f"New modules with a setter (candidates to add): {len(writable_new)}", "",
        "✅ = already used by reyee_local · 🆕 = not used yet", "",
        "| Module | Read OK via | Write methods seen in eWeb |",
        "|---|---|---|",
        *rows, "",
        "Methods seen: " + ", ".join(sorted(methods)),
    ]

    json_path = hass.config.path(f"reyee_discovery{tag}.json")
    md_path = hass.config.path(f"reyee_discovery{tag}.md")
    await hass.async_add_executor_job(
        _write, json_path, json.dumps(report, ensure_ascii=False, indent=2, default=str))
    await hass.async_add_executor_job(_write, md_path, "\n".join(md))

    if not files:
        msg = ("Couldn't download the eWeb JavaScript from the router, so nothing "
               "was discovered. Check the HA log for details.")
    else:
        new_data = [n for n in with_data if n not in KNOWN]
        msg = (
            f"Scanned **{len(files)}** JS files, found **{len(modules)}** modules.\n\n"
            f"- Returned data: **{len(with_data)}** ({len(new_data)} not yet used)\n"
            f"- New writable candidates: **{len(writable_new)}**\n\n"
            f"New modules with data: {', '.join(f'`{n}`' for n in new_data[:60]) or '—'}\n\n"
            f"Full results saved to `/config/reyee_discovery{tag}.json` and "
            f"`/config/reyee_discovery{tag}.md` — send the .json back for the next build."
        )
    async_create(hass, msg, title=f"Reyee Discovery — {api.host}", notification_id=nid)
    _LOGGER.info("Reyee discovery: %d js files, %d modules, %d with data",
                 len(files), len(modules), len(with_data))


# Switch modules seen in the switch eWeb (NBS3100 scan) that the gateway's
# own UI reads through itself with remoteIp=<switch ip>. Read-only getters.
SWITCH_MODULES = (
    "sysinfo", "rg_switch", "summaryInfo", "esw", "linkup",
    "port_base", "port_info", "port_phy", "port_show", "port_vlan", "port_status",
    "poe", "poe_global", "poe_port", "power_monitor",
    "vlan", "vlan_port", "l3_interface_info", "subNetVlan",
    "show_mac", "dynamic_mac", "static_mac", "mac_agetime", "filter_mac",
    "lldp", "lldp_port", "stp", "stpPort", "mstp", "mstpPortStatus", "mstpInfo",
    "rldp", "rldp_port", "rldp_port_status",
    "rate_limit", "storm", "mirror", "isolation", "protected",
    "aggregate_port", "lacp", "lacp_info", "lacp_port",
    "igmpCtl", "igmp_snp_vlan", "show_igmp_snp_vlan", "l2_multicast_base",
    "dhcp_snooping", "dhcp_snp_bind_tbl", "swQos", "swQosPort",
    "devStaLed", "timeReboot", "syslog", "fanout", "voice_vlan", "ipmac_bind_sta",
)


async def _relay_probe(api, ip):
    """Read each switch module through the gateway (remoteIp relay)."""
    out = {}
    strikes = 0
    for mod in SWITCH_MODULES:
        res = {}
        for method in ("devSta.get", "devConfig.get"):
            for no_parse in (True, False):
                if strikes >= 6:
                    res[method] = {"result": "error", "detail": "relay not answering — stopped"}
                    break
                try:
                    body = await asyncio.wait_for(api.raw_cmd(
                        method, mod, no_parse=no_parse, extra={"remoteIp": ip}), CALL_TIMEOUT)
                except asyncio.TimeoutError:
                    body = {"_error": "timeout"}
                if isinstance(body, dict) and "timeout" in str(body.get("_error", "")).lower():
                    strikes += 1
                else:
                    strikes = 0
                kind, payload = _classify(body)
                if kind == "data":
                    res[method] = {"noParse": no_parse, "data": payload}
                    break
                res.setdefault(method, {"result": kind, "detail": str(payload)[:300]})
            if method in res and "data" in res[method]:
                break   # one good read per module is enough
        out[mod] = res
    return out


# ES-series (ISES) switches: the gateway relays HTTP CGI calls via module "esw".
# GET-only paths — the first five are taken from the gateway eWeb, the rest
# are likely names for PoE/system pages (a wrong guess just returns nothing).
ESW_GET_URLS = (
    "rest_get_panel.cgi", "rest_get_port.cgi", "rest_get_vlan_list.cgi",
    "rest_get_vlan_mode.cgi", "rest_get_vlan_multiple.cgi",
    "rest_get_poe.cgi", "rest_get_poe_port.cgi", "rest_get_poe_info.cgi",
    "rest_get_poe_status.cgi", "rest_get_poe_global.cgi",
    "rest_get_sysinfo.cgi", "rest_get_system.cgi", "rest_get_device.cgi",
    "rest_get_loop.cgi", "rest_get_dhcpsnoop.cgi", "rest_get_storm.cgi",
    "rest_get_mac.cgi", "rest_get_port_statistic.cgi", "rest_get_port_stat.cgi",
    "rest_get_led.cgi", "rest_get_hostname.cgi",
    "poe.cgi", "poe_p.cgi", "loop.cgi", "dhcpsnoop.cgi",
)


async def _esw_probe(api, ip, sn):
    out, strikes = {}, 0
    for url in ESW_GET_URLS:
        hdr = {"ip": ip, "sn": sn, "url": url, "method": "get"}
        res = {}
        for label, method, data, extra in (
                ("params", "devSta.get", None, hdr),
                ("data", "devSta.get", hdr, None),
                ("params_cfg", "devConfig.get", None, hdr)):
            if strikes >= 6:
                res[label] = {"result": "error", "detail": "relay not answering — stopped"}
                break
            try:
                body = await asyncio.wait_for(api.raw_cmd(
                    method, "esw", data=data, no_parse=True, extra=extra), CALL_TIMEOUT)
            except asyncio.TimeoutError:
                body = {"_error": "timeout"}
            strikes = strikes + 1 if "timeout" in str(
                body.get("_error", "") if isinstance(body, dict) else "").lower() else 0
            kind, payload = _classify(body)
            if kind == "data":
                res[label] = {"data": payload}
                break
            res[label] = {"result": kind, "detail": str(payload)[:300]}
        out[url] = res
    return out


async def run_switch_scan(hass, coordinator):
    """Read every managed switch through the gateway — one file, read-only."""
    api = coordinator.api
    nid = "reyee_switch_scan"
    await coordinator._read_slow()          # fresh neighbour list
    targets = coordinator.switch_targets()
    if not targets:
        async_create(hass, "No managed switches found on the network.",
                     title="Reyee Switch Scan", notification_id=nid)
        return
    if not api.sid:
        await api.login()
    report, lines = {"via_gateway": api.host, "switches": {}}, []
    for i, (ip, model) in enumerate(targets.items(), 1):
        async_create(hass, f"Reading switch {i} of {len(targets)}: {ip} ({model}) "
                           "through the router…", title="Reyee Switch Scan",
                     notification_id=nid)
        probe = await _relay_probe(api, ip)
        ok = sorted(m for m, r in probe.items()
                    if any(isinstance(v, dict) and "data" in v for v in r.values()))
        entry = {"model": model, "modules_with_data": ok, "probe": probe}
        if not ok:
            # ES-series: try the gateway's "esw" CGI relay instead
            sn = next((n.get("sn") for n in coordinator._slow.get("neighbors", [])
                       if n.get("ip") == ip), None)
            esw = await _esw_probe(api, ip, sn)
            ok = sorted(u for u, r in esw.items()
                        if any(isinstance(v, dict) and "data" in v for v in r.values()))
            entry.update({"sn": sn, "esw_probe": esw, "esw_urls_with_data": ok})
        report["switches"][ip] = entry
        lines.append(f"- {ip} ({model}): **{len(ok)}** modules answered"
                     + (f" — {', '.join(ok[:12])}{'…' if len(ok) > 12 else ''}" if ok else ""))
    path = hass.config.path("reyee_switches.json")
    await hass.async_add_executor_job(
        _write, path, json.dumps(report, ensure_ascii=False, indent=2, default=str))
    async_create(hass, "Done.\n\n" + "\n".join(lines) +
                 "\n\nSend back **one file**: `/config/reyee_switches.json`",
                 title="Reyee Switch Scan", notification_id=nid)
