"""Read-only binary state for the planned cutoff phase."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DATA_ENTRIES
from .const import DOMAIN
from .coordinator import OptimizerCoordinator
from .entity import OptimizerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OptimizerCoordinator = hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
    if entry.data.get("scenario") in {"threshold", "schedule"}:
        from .price_binary_sensor import add_price_binary_sensors

        add_price_binary_sensors(coordinator, async_add_entities)
        return
    async_add_entities([CutoffActiveBinarySensor(coordinator)])


class CutoffActiveBinarySensor(OptimizerEntity, BinarySensorEntity):
    """On only while an available schedule is in its shutdown phase."""

    _attr_translation_key = "cutoff_active"
    _attr_icon = "mdi:flash-off"

    def __init__(self, coordinator: OptimizerCoordinator) -> None:
        super().__init__(coordinator, "cutoff_active")

    @property
    def is_on(self) -> bool | None:
        snapshot = self.coordinator.data
        if snapshot is None or not snapshot.available:
            return None
        return snapshot.phase == "shutdown"
