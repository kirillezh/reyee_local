"""WAN / internet connectivity binary sensor."""
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorEntity, BinarySensorDeviceClass,
)
from homeassistant.helpers.device_registry import DeviceInfo, CONNECTION_NETWORK_MAC
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .switch_hub import is_switch_entry, async_setup_platform_for_switch, setup_router_switches
from .subentries import NETWORK, SWITCHES, APS, bind, switch_records
from .coordinator import VPN_SERVERS

_LOGGER = logging.getLogger(__name__)


_INFRA_TYPES = {
    "MSW": "Switch", "SW": "Switch", "SWITCH": "Switch",
    "EAP": "Access Point", "AP": "Access Point",
}


async def async_setup_entry(hass, entry, async_add_entities):
    if is_switch_entry(entry):
        return await async_setup_platform_for_switch(hass, entry, async_add_entities, "binary_sensor")
    await setup_router_switches(hass, entry, async_add_entities, "binary_sensor")
    raw_add = async_add_entities
    async_add_entities = bind(entry, async_add_entities, NETWORK)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ReyeeInternetOnline(coordinator, entry)])
    async_add_entities([ReyeeVpnServer(coordinator, entry, key, name)
                        for key, name in VPN_SERVERS])

    seen = set()

    add_switch = bind(entry, raw_add, SWITCHES)
    add_ap = bind(entry, raw_add, APS)
    added = switch_records(entry)
    added_ips = {r.get("host") for r in added}
    added_sns = {r.get("sn") for r in added if r.get("sn")}

    def _add_infra():
        sw, ap = [], []
        for n in (coordinator.data or {}).get("topology", []):
            if n.get("depth", 0) == 0:
                continue  # skip the gateway itself
            sn = n.get("sn") or n.get("mac") or n.get("name")
            if sn and sn not in seen and n.get("type") in _INFRA_TYPES:
                seen.add(sn)
                if n.get("ip") in added_ips or sn in added_sns:
                    continue  # added under Switches with its full controls
                is_sw = _INFRA_TYPES[n["type"]] == "Switch"
                (sw if is_sw else ap).append(ReyeeInfraDevice(coordinator, entry, sn))
        if sw:
            add_switch(sw)
        if ap:
            add_ap(ap)

    _add_infra()

    seen_ports = set()

    def _add_ports():
        new = []
        for sn, sw in (coordinator.data or {}).get("switches", {}).items():
            for p in sw.get("ports", []):
                key = (sn, p.get("lpid"))
                if key not in seen_ports:
                    seen_ports.add(key)
                    new.append(ReyeeSwitchPort(coordinator, entry, sn, p.get("lpid")))
        if new:
            add_switch(new)

    _add_ports()
    entry.async_on_unload(coordinator.async_add_listener(_add_ports))
    entry.async_on_unload(coordinator.async_add_listener(_add_infra))


class ReyeeInternetOnline(CoordinatorEntity, BinarySensorEntity):
    _attr_name = "Reyee WAN Online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wan_online"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Ruijie",
            name=entry.title,
            configuration_url=f"http://{entry.data['host']}",
        )

    @property
    def is_on(self):
        # networkConnect is the router's own internet-reachability check
        return bool((self.coordinator.data or {}).get("internet_up"))

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        return {
            "active_uplink": d.get("active_wan"),
            "public_ip": d.get("wan_ip"),
        }


class ReyeeInfraDevice(CoordinatorEntity, BinarySensorEntity):
    """A downstream Reyee switch or AP as its own device, connected via the gateway."""
    _attr_has_entity_name = True
    _attr_name = None
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, entry, sn):
        super().__init__(coordinator)
        self._sn = sn
        self._attr_unique_id = f"{entry.entry_id}_infra_{sn}"
        node = self._node() or {}
        kind = _INFRA_TYPES.get(node.get("type"), "Device")
        name = node.get("name") or sn
        info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_infra_{sn}")},
            "name": name,
            "model": node.get("model"),
            "manufacturer": "Ruijie",
            "via_device": (DOMAIN, entry.entry_id),
        }
        if node.get("mac"):
            info["connections"] = {(CONNECTION_NETWORK_MAC, node["mac"])}
        if node.get("ip"):
            info["configuration_url"] = f"http://{node['ip']}"
        self._attr_device_info = DeviceInfo(**info)
        self._kind = kind

    def _node(self):
        for n in (self.coordinator.data or {}).get("topology", []):
            if (n.get("sn") or n.get("mac") or n.get("name")) == self._sn:
                return n
        return None

    @property
    def is_on(self):
        n = self._node()
        return bool(n and n.get("online"))

    @property
    def icon(self):
        return "mdi:switch" if self._kind == "Switch" else "mdi:access-point"

    @property
    def extra_state_attributes(self):
        n = self._node() or {}
        return {
            "type": self._kind,
            "model": n.get("model"),
            "ip": n.get("ip"),
            "mac": n.get("mac"),
            "serial": n.get("sn"),
            "uplink_port": n.get("uplink_port"),
        }


class ReyeeVpnServer(CoordinatorEntity, BinarySensorEntity):
    """Whether a VPN server (OpenVPN / L2TP / PPTP) is enabled on the gateway."""
    _attr_icon = "mdi:vpn"

    def __init__(self, coordinator, entry, key, name):
        super().__init__(coordinator)
        self._key = key
        self._attr_name = f"Reyee {name}"
        self._attr_unique_id = f"{entry.entry_id}_vpn_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Ruijie",
            name=entry.title,
            configuration_url=f"http://{entry.data['host']}",
        )

    @property
    def available(self):
        return super().available and self._key in (self.coordinator.data or {}).get("vpn", {})

    @property
    def is_on(self):
        return bool((self.coordinator.data or {}).get("vpn", {}).get(self._key))


class ReyeeSwitchPort(CoordinatorEntity, BinarySensorEntity):
    """Link state of one port on a managed switch (read through the gateway)."""
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, entry, sn, lpid):
        super().__init__(coordinator)
        self._sn, self._lpid = sn, lpid
        sw = (coordinator.data or {}).get("switches", {}).get(sn, {})
        port = self._port() or {}
        self._attr_name = f"Port {port.get('name') or lpid}"
        self._attr_unique_id = f"{entry.entry_id}_swport_{sn}_{lpid}"
        info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_infra_{sn}")},
            "manufacturer": "Ruijie",
            "model": sw.get("model"),
            "via_device": (DOMAIN, entry.entry_id),
        }
        if sw.get("ip"):
            info["configuration_url"] = f"http://{sw['ip']}"
        self._attr_device_info = DeviceInfo(**info)

    def _port(self):
        sw = (self.coordinator.data or {}).get("switches", {}).get(self._sn, {})
        for p in sw.get("ports", []):
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
        return {
            "speed": p.get("speed"),
            "description": p.get("description") or None,
            "pvid": p.get("pvid"),
            "vlans": p.get("vlans") or None,
            "rx_rate": p.get("rx_rate"),
            "tx_rate": p.get("tx_rate"),
            "rx_bytes": p.get("rx_bytes"),
            "tx_bytes": p.get("tx_bytes"),
            "crc_errors": p.get("crc_errors"),
        }
