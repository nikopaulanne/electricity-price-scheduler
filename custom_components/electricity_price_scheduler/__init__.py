"""Home Assistant integration entry points.

Keep HA imports inside setup functions so importing the pure core package does
not require a Home Assistant runtime.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .const import ACTIVE_WINDOW_STORE_VERSION, DOMAIN

if TYPE_CHECKING:  # pragma: no cover - static typing only
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS = ("sensor", "binary_sensor")
DATA_ENTRIES = "entries"
DATA_OPTIMIZER_SEMAPHORE = "optimizer_semaphore"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Set up one optimizer profile."""
    from .coordinator import OptimizerCoordinator

    domain_data = hass.data.setdefault(DOMAIN, {})
    entries = domain_data.setdefault(DATA_ENTRIES, {})
    semaphore = domain_data.setdefault(
        DATA_OPTIMIZER_SEMAPHORE, asyncio.Semaphore(1)
    )
    if entry.data.get("scenario") in {"threshold", "schedule"}:
        from .price_runtime import PriceCoordinator

        coordinator = PriceCoordinator(hass, entry, semaphore)
    else:
        coordinator = OptimizerCoordinator(hass, entry, semaphore)
    platforms_loaded = False
    try:
        await coordinator.async_config_entry_first_refresh()
        entries[entry.entry_id] = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        platforms_loaded = True
        await coordinator.async_start()
    except BaseException:
        entries.pop(entry.entry_id, None)
        if platforms_loaded:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        await coordinator.async_shutdown()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Unload platforms and the entry's runtime listeners."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        from homeassistant.exceptions import ConfigEntryError

        raise ConfigEntryError("Could not unload optimizer platforms")
    domain_data = hass.data.get(DOMAIN, {})
    coordinator = domain_data.get(DATA_ENTRIES, {}).pop(entry.entry_id, None)
    if coordinator is not None:
        await coordinator.async_shutdown()
    # Keep the per-HA executor semaphore alive across rapid unload/reload: a
    # thread already running cannot be stopped by cancelling its asyncio task.


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove profile-scoped persisted data when HA deletes the config entry."""
    from homeassistant.helpers.storage import Store

    store = Store(
        hass,
        ACTIVE_WINDOW_STORE_VERSION,
        f"{DOMAIN}.{entry.entry_id}.active_window",
    )
    await store.async_remove()
