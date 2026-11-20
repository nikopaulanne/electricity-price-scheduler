"""Read-only schedule and diagnostic sensor entities."""

from __future__ import annotations

from datetime import timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DATA_ENTRIES
from .const import DOMAIN
from .coordinator import DataStatus, OptimizerCoordinator
from .entity import OptimizerEntity

_UTC = timezone.utc


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OptimizerCoordinator = hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
    if entry.data.get("scenario") in {"threshold", "schedule"}:
        from .price_sensor import add_price_sensors

        add_price_sensors(coordinator, async_add_entities)
        return
    async_add_entities(
        [
            PhaseSensor(coordinator),
            DecisionReasonSensor(coordinator),
            NextCutoffStartSensor(coordinator),
            NextCutoffEndSensor(coordinator),
            ScheduleSensor(coordinator),
            DataStatusSensor(coordinator),
            ModeledSavingsSensor(coordinator),
        ]
    )


class PhaseSensor(OptimizerEntity, SensorEntity):
    """Current schedule phase, or unavailable when source data is invalid."""

    _attr_translation_key = "phase"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["normal", "preheat", "shutdown", "recovery"]
    _attr_icon = "mdi:timeline-clock-outline"

    def __init__(self, coordinator: OptimizerCoordinator) -> None:
        super().__init__(coordinator, "phase")

    @property
    def native_value(self) -> str | None:
        snapshot = self.coordinator.data
        return snapshot.phase if snapshot is not None and snapshot.available else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self.coordinator.data
        if snapshot is None:
            return {}
        return {
            "data_status": snapshot.status.value,
            "current_period": _period_attributes(
                snapshot.current_period,
                locked=snapshot.current_period == snapshot.locked_period,
            ),
            "next_change": _iso_utc(snapshot.next_change),
            "locked": snapshot.locked_period is not None,
            "locked_settings_known": _locked_settings_known(snapshot),
            "locked_period": _period_attributes(snapshot.locked_period, locked=True),
            "locked_effective_settings": snapshot.locked_effective_settings,
            "decision_reason": snapshot.decision_reason,
            "effective_settings": snapshot.effective_settings or {},
            "warnings": list(snapshot.warnings),
        }


class DecisionReasonSensor(OptimizerEntity, SensorEntity):
    """Human-readable machine state explaining the current schedule decision."""

    _attr_translation_key = "decision_reason"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = [
        "source_unavailable",
        "invalid_data",
        "preheat",
        "shutdown",
        "recovery",
        "waiting_for_prices",
        "next_cutoff_selected",
        "no_cutoff_selected",
    ]
    _attr_icon = "mdi:comment-question-outline"

    def __init__(self, coordinator: OptimizerCoordinator) -> None:
        super().__init__(coordinator, "decision_reason")

    @property
    def available(self) -> bool:
        # Keep failure reasons visible even when the schedule itself is unavailable.
        return self.coordinator.data is not None

    @property
    def native_value(self) -> str | None:
        snapshot = self.coordinator.data
        return snapshot.decision_reason if snapshot is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self.coordinator.data
        if snapshot is None:
            return {}
        return {
            "phase": snapshot.phase,
            "data_status": snapshot.status.value,
            "next_change": _iso_utc(snapshot.next_change),
            "locked": snapshot.locked_period is not None,
            "locked_settings_known": _locked_settings_known(snapshot),
            "effective_settings": snapshot.effective_settings or {},
            "locked_effective_settings": snapshot.locked_effective_settings,
            "warnings": list(snapshot.warnings),
        }


class NextCutoffStartSensor(OptimizerEntity, SensorEntity):
    """Next not-yet-started shutdown boundary as an aware UTC timestamp."""

    _attr_translation_key = "next_cutoff_start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-start"

    def __init__(self, coordinator: OptimizerCoordinator) -> None:
        super().__init__(coordinator, "next_cutoff_start")

    @property
    def native_value(self):
        snapshot = self.coordinator.data
        if snapshot is None or not snapshot.available or snapshot.next_period is None:
            return None
        return snapshot.next_period.shutdown_start.astimezone(_UTC)


class NextCutoffEndSensor(OptimizerEntity, SensorEntity):
    """Next shutdown end (the start of recovery) as UTC timestamp."""

    _attr_translation_key = "next_cutoff_end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-end"

    def __init__(self, coordinator: OptimizerCoordinator) -> None:
        super().__init__(coordinator, "next_cutoff_end")

    @property
    def native_value(self):
        snapshot = self.coordinator.data
        if snapshot is None or not snapshot.available or snapshot.next_period is None:
            return None
        return snapshot.next_period.recovery_start.astimezone(_UTC)


class ScheduleSensor(OptimizerEntity, SensorEntity):
    """Count of selected windows and their documented plan attributes."""

    _attr_translation_key = "schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: OptimizerCoordinator) -> None:
        super().__init__(coordinator, "schedule")

    @property
    def native_value(self) -> int | None:
        snapshot = self.coordinator.data
        if snapshot is None or not snapshot.available or snapshot.result is None:
            return None
        return len(snapshot.result.periods)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self.coordinator.data
        if snapshot is None or not snapshot.available or snapshot.result is None:
            return {}
        periods = [
            _period_attributes(period, locked=period == snapshot.locked_period)
            for period in snapshot.result.periods
        ]
        return {
            "periods": periods,
            "currency": snapshot.result.currency,
            "provider": snapshot.price_snapshot.provider if snapshot.price_snapshot else None,
            "data_status": snapshot.status.value,
            "tomorrow_available": (
                snapshot.price_snapshot.tomorrow_available
                if snapshot.price_snapshot
                else False
            ),
            "warnings": list(snapshot.warnings),
            "next_change": _iso_utc(snapshot.next_change),
            "locked": snapshot.locked_period is not None,
            "locked_settings_known": _locked_settings_known(snapshot),
            "locked_period": _period_attributes(snapshot.locked_period, locked=True),
            "effective_settings": snapshot.effective_settings or {},
            "locked_effective_settings": snapshot.locked_effective_settings,
            "last_success": _iso_utc(snapshot.last_success),
            "calculated_at": _iso_utc(snapshot.calculated_at),
            "candidates_scanned": snapshot.result.candidates_scanned,
            "candidates_accepted": snapshot.result.candidates_accepted,
            "slot_count": snapshot.result.slot_count,
        }


class DataStatusSensor(OptimizerEntity, SensorEntity):
    """Always-available machine-readable source/calculation status."""

    _attr_translation_key = "data_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = [status.value for status in DataStatus]
    _attr_icon = "mdi:information-outline"

    def __init__(self, coordinator: OptimizerCoordinator) -> None:
        super().__init__(coordinator, "data_status")

    @property
    def native_value(self) -> str | None:
        snapshot = self.coordinator.data
        return snapshot.status.value if snapshot is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self.coordinator.data
        if snapshot is None:
            return {}
        return {
            "available": snapshot.available,
            "error_code": snapshot.error_code,
            "last_success": _iso_utc(snapshot.last_success),
            "calculated_at": _iso_utc(snapshot.calculated_at),
            "warnings": list(snapshot.warnings),
        }


class ModeledSavingsSensor(OptimizerEntity, SensorEntity):
    """Optional modeled estimate for the 1 kW reference load."""

    _attr_translation_key = "modeled_savings_per_kw"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:cash-clock"

    def __init__(self, coordinator: OptimizerCoordinator) -> None:
        super().__init__(coordinator, "modeled_savings_per_kw")

    @property
    def native_unit_of_measurement(self) -> str | None:
        snapshot = self.coordinator.data
        if snapshot and snapshot.result:
            return f"{snapshot.result.currency}/kW"
        if snapshot and snapshot.price_snapshot:
            return f"{snapshot.price_snapshot.currency}/kW"
        return None

    @property
    def native_value(self) -> float | None:
        snapshot = self.coordinator.data
        if snapshot is None or not snapshot.available or snapshot.result is None:
            return None
        return snapshot.result.total_savings_per_kw

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self.coordinator.data
        if snapshot is None or not snapshot.available:
            return {"estimate_type": "modeled", "reference_load_kw": 1}
        return {
            "estimate_type": "modeled",
            "reference_load_kw": 1,
            "not_measured_bill_savings": True,
            "data_status": snapshot.status.value,
        }


def _period_attributes(period, *, locked: bool = False) -> dict[str, object] | None:
    if period is None:
        return None
    return {
        "preheat_start": _iso_utc(period.preheat_start),
        "shutdown_start": _iso_utc(period.shutdown_start),
        "recovery_start": _iso_utc(period.recovery_start),
        "recovery_end": _iso_utc(period.recovery_end),
        "cost_saving_per_kw": period.savings_per_kw,
        "cost_saving_percent": period.savings_percent,
        "shutdown_duration_hours": period.shutdown_duration_hours,
        "price_difference_per_kwh": period.price_difference_per_kwh,
        "minimum_price_difference_per_kwh": period.min_price_diff_per_kwh,
        "locked": locked,
    }


def _locked_settings_known(snapshot) -> bool | None:
    if snapshot.locked_period is None:
        return None
    return snapshot.locked_effective_settings is not None


def _iso_utc(value) -> str | None:
    return value.astimezone(_UTC).isoformat() if value is not None else None
