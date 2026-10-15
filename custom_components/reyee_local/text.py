"""Text entities — managed-switch port descriptions (switch hub entries only)."""
from .switch_hub import is_switch_entry, async_setup_platform_for_switch


async def async_setup_entry(hass, entry, async_add_entities):
    if is_switch_entry(entry):
        await async_setup_platform_for_switch(hass, entry, async_add_entities, "text")
