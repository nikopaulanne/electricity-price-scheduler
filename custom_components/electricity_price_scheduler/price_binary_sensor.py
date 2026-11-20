"""Active state entity for threshold and cheapest-period profiles."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import OptimizerEntity
from .price_runtime import PriceCoordinator


def add_price_binary_sensors(
    coordinator: PriceCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    """Add price scenario binary sensors for one profile."""
    async_add_entities([PriceActiveBinarySensor(coordinator)])


class PriceActiveBinarySensor(OptimizerEntity, BinarySensorEntity):
    """On while the price scenario currently selects the profile as active."""

    _attr_translation_key = "price_active"
    _attr_icon = "mdi:flash"

    def __init__(self, coordinator: PriceCoordinator) -> None:
        super().__init__(coordinator, "price_active")

    @property
    def is_on(self) -> bool | None:
        snapshot = self.coordinator.data
        if snapshot is None or not snapshot.available:
            return None
        return snapshot.active
