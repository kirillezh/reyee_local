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
from urllib.parse import urljoin, urlparse

import async_timeout
from homeassistant.components.persistent_notification import async_create

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
    re.compile(r"""module["']?\s*:\s*["'`]([A-Za-z][A-Za-z0-9_\-]{1,40})["'`]"""),
    re.compile(r"""["']module["']\s*,\s*["']([A-Za-z][A-Za-z0-9_\-]{1,40})["']"""),
)
_SCRIPT_SRC_RE = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)
_JS_REF_RE = re.compile(r"""["'`]((?:\.{0,2}/)?[A-Za-z0-9_\-./]*?[A-Za-z0-9_\-]+\.js)["'`]""")
# webpack chunk maps: {12:"a1b2c3d4", "chunk-x":"deadbeef"}
_CHUNK_PAIR_RE = re.compile(r"""["']?([A-Za-z0-9_\-]+)["']?\s*:\s*["']([0-9a-f]{6,20})["']""")
_CHUNK_PREFIX_RE = re.compile(r"""["']((?:static/)?js/[A-Za-z0-9_\-/]*)["']\s*\+""")

MAX_JS_FILES = 400
MAX_FETCHES = 1500
SNIPPET = 260


async def _fetch_text(session, url, timeout=15):
    try:
        async with async_timeout.timeout(timeout):
            async with session.get(url, ssl=False) as resp:
                if resp.status != 200:
                    return None
                return await resp.text(errors="ignore")
    except Exception:  # noqa: BLE001
        return None


async def _collect_js(api):
    """Fetch the eWeb entry pages and crawl every JS file they reference."""
    base = api._base or await api._resolve_base()
    host = urlparse(base).netloc
    session = api.session

    pages = ["/", "/cgi-bin/luci/", "/index.html", "/cgi-bin/luci/eweb/"]
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

    for page in pages:
        html = await _fetch_text(session, base + page)
        if html:
            for src in _SCRIPT_SRC_RE.findall(html):
                _add(src, base + page)

    prefixes = set()
    attempts = 0
    while queue and len(files) < MAX_JS_FILES and attempts < MAX_FETCHES:
        attempts += 1
        url = queue.pop(0)
        text = await _fetch_text(session, url)
        if not text:
            continue
        files[url] = text
        for ref in _JS_REF_RE.findall(text):
            _add(ref, url)
            if not ref.startswith(("/", "http")):
                _add("/" + ref.lstrip("./"), base + "/")
        # Guess webpack lazy chunks from hash maps
        local_prefixes = set(_CHUNK_PREFIX_RE.findall(text))
        prefixes.update(local_prefixes)
        if local_prefixes:
            for cid, h in _CHUNK_PAIR_RE.findall(text):
                for pre in prefixes:
                    for cand in (f"{pre}{cid}.{h}.js", f"{pre}{h}.js"):
                        _add("/" + cand.lstrip("/"), base + "/")

    return base, files


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
                name = m.group(1)
                i = m.start()
                ctx = text[max(0, i - SNIPPET): i + SNIPPET]
                rec = modules.setdefault(name, {"files": set(), "methods": set(), "snippets": []})
                rec["files"].add(fname)
                rec["methods"].update(_METHOD_RE.findall(ctx))
                if len(rec["snippets"]) < 4:
                    rec["snippets"].append(re.sub(r"\s+", " ", ctx))
    return modules, methods, api_paths


def _classify(body):
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


async def _probe(api, modules):
    sem = asyncio.Semaphore(4)
    results = {}

    async def one(name, rec):
        seen_getters = [m for m in rec["methods"] if m.endswith(".get")]
        order = [g for g in GETTERS if g in seen_getters] + \
                [g for g in GETTERS if g not in seen_getters]
        out = {}
        async with sem:
            for method in order:
                variants = [(None, False), (None, True)]
                if method.startswith("acConfig"):
                    variants.append(({"groupId": "0"}, False))
                for data, no_parse in variants:
                    body = await api.raw_cmd(method, name, data=data, no_parse=no_parse)
                    kind, payload = _classify(body)
                    if kind == "data":
                        out[method] = {"noParse": no_parse, "request_data": data,
                                       "data": payload}
                        break
                    out.setdefault(method, {"result": kind,
                                            "detail": str(payload)[:300]})
        results[name] = out

    await asyncio.gather(*(one(n, r) for n, r in modules.items()))
    return results


def _write(path, content):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


async def run_discovery(hass, coordinator):
    api = coordinator.api
    async_create(hass, "Discovering Reyee modules… this can take a few minutes.",
                 title="Reyee Discovery", notification_id="reyee_discovery")
    if not api.sid:
        await api.login()

    base, files = await _collect_js(api)
    modules, methods, api_paths = _extract(files)
    probe = await _probe(api, modules) if modules else {}

    with_data, writable_new, rows = [], [], []
    for name in sorted(modules, key=str.lower):
        rec = modules[name]
        pr = probe.get(name, {})
        got = [m for m, v in pr.items() if "data" in v]
        setters = sorted(m for m in rec["methods"] if not m.endswith(".get"))
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
        "methods_seen": sorted(methods),
        "api_paths_seen": sorted(api_paths),
        "modules": {
            n: {
                "already_in_integration": n in KNOWN,
                "methods_in_js": sorted(r["methods"]),
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

    json_path = hass.config.path("reyee_discovery.json")
    md_path = hass.config.path("reyee_discovery.md")
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
            f"Full results saved to `/config/reyee_discovery.json` and "
            f"`/config/reyee_discovery.md` — send the .json back for the next build."
        )
    async_create(hass, msg, title="Reyee Discovery", notification_id="reyee_discovery")
    _LOGGER.info("Reyee discovery: %d js files, %d modules, %d with data",
                 len(files), len(modules), len(with_data))
