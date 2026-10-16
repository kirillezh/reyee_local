"""Split the integration page into collapsible groups using config subentries.

  • "Router"             — the gateway (+ all hub entities)
  • "Switches"           — managed / PoE switches (added with "+ Add switch")
  • "Access Points"      — APs seen by the gateway
  • "Connected Devices"  — every client tracked on the network

Needs Home Assistant 2025.3+ (config subentries). On older versions every helper
here is a no-op and the integration behaves exactly as before.
"""
import logging
from types import MappingProxyType

from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.config_entries import ConfigSubentry
except ImportError:  # HA < 2025.3
    ConfigSubentry = None

NETWORK = "network"
SWITCHES = "switches"
APS = "aps"
CLIENTS = "clients"

_TITLES = {
    NETWORK: "Router",
    SWITCHES: "Switches",
    APS: "Access Points",
    CLIENTS: "Connected Devices",
}
_OLD_TITLES = {NETWORK: ("Reyee Network",)}


def subentry_id(entry, kind):
    """Return the subentry id for `kind`, or None when unsupported/missing."""
    for sub in (getattr(entry, "subentries", None) or {}).values():
        if sub.unique_id == kind:
            return sub.subentry_id
    return None


def ensure_subentries(hass, entry):
    """Create the two group subentries if they don't exist yet."""
    if ConfigSubentry is None:
        return
    for kind, title in _TITLES.items():
        sid = subentry_id(entry, kind)
        if sid:
            sub = entry.subentries[sid]
            if sub.title in _OLD_TITLES.get(kind, ()):
                try:
                    hass.config_entries.async_update_subentry(entry, sub, title=title)
                except Exception:  # noqa: BLE001
                    pass
            continue
        try:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType({}),
                    subentry_type=kind,
                    title=title,
                    unique_id=kind,
                ),
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Reyee: could not create %s group: %s", kind, err)


def switch_records(entry):
    """Switches added to this router entry: list of dicts
    {host, username, password, key, title, model, sn}."""
    sid = subentry_id(entry, SWITCHES)
    if not sid:
        return []
    return [dict(r) for r in (entry.subentries[sid].data.get("switches") or [])]


def set_switch_records(hass, entry, records):
    sid = subentry_id(entry, SWITCHES)
    if not sid:
        return False
    sub = entry.subentries[sid]
    hass.config_entries.async_update_subentry(
        entry, sub, data={**dict(sub.data), "switches": list(records)})
    return True


def ap_records(entry):
    """APs added to this router entry (the Access Points group)."""
    sid = subentry_id(entry, APS)
    if not sid:
        return []
    return [dict(r) for r in (entry.subentries[sid].data.get("aps") or [])]


def set_ap_records(hass, entry, records):
    sid = subentry_id(entry, APS)
    if not sid:
        return False
    sub = entry.subentries[sid]
    hass.config_entries.async_update_subentry(
        entry, sub, data={**dict(sub.data), "aps": list(records)})
    return True


def bind(entry, async_add_entities, kind):
    """Wrap a platform's async_add_entities so entities land in a group."""
    sid = subentry_id(entry, kind)
    if not sid:
        return async_add_entities

    def _add(new_entities, update_before_add=False):
        async_add_entities(new_entities, update_before_add, config_subentry_id=sid)

    return _add


def _current_subentries(device, entry_id):
    cur = getattr(device, "config_subentry_id", ...)
    if cur is not ...:  # HA 2026.9+: one entry / one subentry per device
        return {cur}
    return set(device.config_entries_subentries.get(entry_id, set()))


def _move(registry, device, entry_id, target):
    current = _current_subentries(device, entry_id)
    if current == {target}:
        return
    try:
        registry.async_update_device(device.id, new_config_subentry_id=target)
        return
    except TypeError:
        pass  # HA < 2026.9 — use the add/remove API
    registry.async_update_device(
        device.id, add_config_entry_id=entry_id, add_config_subentry_id=target)
    for old in current - {target}:
        registry.async_update_device(
            device.id, remove_config_entry_id=entry_id, remove_config_subentry_id=old)


def _group_for(entry, device, ids):
    """Which group a device belongs in."""
    from .coordinator import is_switch_model
    ours = [i[1] for i in device.identifiers if i[0] == DOMAIN]
    if not ours:
        return ids[CLIENTS]
    keys = {r.get("key") for r in switch_records(entry)}
    for ident in ours:
        if ident in keys or ident.startswith(f"{entry.entry_id}_sw_"):
            return ids[SWITCHES]
        if "_infra_" in ident:
            return ids[SWITCHES] if is_switch_model(device.model, None) else ids[APS]
    return ids[NETWORK]


def migrate_devices(hass, entry):
    """Move existing devices into the right group (no delete/re-add needed).

    Our own devices (gateway, APs, switches) carry a DOMAIN identifier; client
    devices are created by HA's ScannerEntity from the MAC alone.
    """
    ids = {k: subentry_id(entry, k) for k in _TITLES}
    if not all(ids.values()):
        return
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        try:
            _move(registry, device, entry.entry_id, _group_for(entry, device, ids))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Reyee: could not regroup device %s: %s", device.id, err)
