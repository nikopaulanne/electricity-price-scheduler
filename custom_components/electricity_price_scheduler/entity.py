"""Shared entity behavior for one virtual optimizer service per entry."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OptimizerCoordinator


class OptimizerEntity(CoordinatorEntity[OptimizerCoordinator]):
    """Base entity attached to its entry-specific virtual service device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: OptimizerCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=coordinator.entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        snapshot = self.coordinator.data
        if snapshot is None:
            return False
        if getattr(self, "_attr_translation_key", None) == "data_status":
            return True
        return snapshot.available
