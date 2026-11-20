"""Initialize real HA registries needed by isolated config-entry tests."""

from homeassistant.config_entries import ConfigEntries
from homeassistant.helpers import area_registry, device_registry, entity_registry, floor_registry, frame
from homeassistant.loader import async_setup as async_setup_loader


async def async_prepare_hass(hass):
    async_setup_loader(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    frame.async_setup(hass)
    device_registry.async_setup(hass)
    await floor_registry.async_load(hass, load_empty=True)
    await area_registry.async_load(hass, load_empty=True)
    await device_registry.async_load(hass, load_empty=True)
    await entity_registry.async_load(hass, load_empty=True)
