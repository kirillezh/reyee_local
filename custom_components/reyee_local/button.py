"""Buttons — force a refresh, run the deep probe."""
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .subentries import NETWORK, bind
from .diagnostics_probe import run_deep_probe
from .discovery import run_discovery, run_switch_scan

_LOGGER = logging.getLogger(__name__)


def _dev(entry):
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Ruijie",
        name=entry.title,
        configuration_url=f"http://{entry.data['host']}",
    )


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities = bind(entry, async_add_entities, NETWORK)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ReyeeRefreshButton(coordinator, entry),
        ReyeeProbeButton(coordinator, entry),
        ReyeeDiscoverButton(coordinator, entry),
        ReyeeScanSwitchesButton(coordinator, entry),
    ])


class ReyeeRefreshButton(CoordinatorEntity, ButtonEntity):
    _attr_name = "Reyee Refresh"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_refresh"
        self._attr_device_info = _dev(entry)

    async def async_press(self):
        await self.coordinator.async_request_refresh()


class ReyeeProbeButton(CoordinatorEntity, ButtonEntity):
    _attr_name = "Reyee Deep Probe"
    _attr_icon = "mdi:magnify-scan"
    _attr_entity_registry_enabled_default = False   # diagnostic, off by default

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_deep_probe"
        self._attr_device_info = _dev(entry)

    async def async_press(self):
        await run_deep_probe(self.hass, self.coordinator)


class ReyeeDiscoverButton(CoordinatorEntity, ButtonEntity):
    """Scan the router's eWeb JS for every API module and probe them read-only."""
    _attr_name = "Reyee Discover Modules"
    _attr_icon = "mdi:radar"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_discover_modules"
        self._attr_device_info = _dev(entry)

    async def async_press(self):
        self.hass.async_create_task(run_discovery(self.hass, self.coordinator))


class ReyeeScanSwitchesButton(CoordinatorEntity, ButtonEntity):
    """Find every managed switch on the network and scan each one (read-only)."""
    _attr_name = "Reyee Scan Switches"
    _attr_icon = "mdi:switch"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_scan_switches"
        self._attr_device_info = _dev(entry)

    async def async_press(self):
        self.hass.async_create_task(run_switch_scan(self.hass, self.coordinator))
