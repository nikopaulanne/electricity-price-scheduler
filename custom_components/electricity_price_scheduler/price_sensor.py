"""Read-only entities for threshold and cheapest-period profiles."""

from __future__ import annotations

from datetime import timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import OptimizerEntity
from .price_runtime import PriceCoordinator, build_price_decision_attributes

_UTC = timezone.utc


def add_price_sensors(
    coordinator: PriceCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    """Add price scenario sensors for one profile."""
    async_add_entities(
        [
            CurrentPriceSensor(coordinator),
            PriceScheduleSensor(coordinator),
            NextPriceStartSensor(coordinator),
            NextPriceEndSensor(coordinator),
            PriceDataStatusSensor(coordinator),
        ]
    )


class CurrentPriceSensor(OptimizerEntity, SensorEntity):
    """Current source price in the source currency per kWh."""

    _attr_translation_key = "current_price"
    _attr_icon = "mdi:currency-eur"

    def __init__(self, coordinator: PriceCoordinator) -> None:
        super().__init__(coordinator, "current_price")

    @property
    def native_unit_of_measurement(self) -> str | None:
        snapshot = self.coordinator.data
        price_snapshot = snapshot.price_snapshot if snapshot else None
        return f"{price_snapshot.currency}/kWh" if price_snapshot else None

    @property
    def native_value(self) -> float | None:
        snapshot = self.coordinator.data
        return snapshot.current_price if snapshot and snapshot.available else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self.coordinator.data
        if snapshot is None:
            return {}
        attrs = _decision_attributes(snapshot)
        return {
            key: attrs[key]
            for key in (
                "status", "active", "reason_code", "current_price", "currency",
                "threshold_price_per_kwh", "threshold_comparison", "window_start",
                "window_end", "provider", "calculated_at", "last_success",
            )
        }


class PriceScheduleSensor(OptimizerEntity, SensorEntity):
    """Number of selected operating periods and the current plan explanation."""

    _attr_translation_key = "price_schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: PriceCoordinator) -> None:
        super().__init__(coordinator, "price_schedule")

    @property
    def native_value(self) -> int | None:
        snapshot = self.coordinator.data
        if snapshot is None or not snapshot.available or snapshot.result is None:
            return None
        return len(_runs(snapshot.result.selected_slots))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self.coordinator.data
        if snapshot is None:
            return {}
        return _decision_attributes(snapshot)


class NextPriceStartSensor(OptimizerEntity, SensorEntity):
    """Start of the next selected interval that has not started yet."""

    _attr_translation_key = "next_price_start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-start"

    def __init__(self, coordinator: PriceCoordinator) -> None:
        super().__init__(coordinator, "next_price_start")

    @property
    def native_value(self):
        snapshot = self.coordinator.data
        if snapshot is None or not snapshot.available or snapshot.result is None:
            return None
        now = snapshot.calculated_at
        run = next((run for run in _runs(snapshot.result.selected_slots) if now is not None and run[0].start > now), None)
        return run[0].start.astimezone(_UTC) if run else None


class NextPriceEndSensor(OptimizerEntity, SensorEntity):
    """End of the next selected operating period."""

    _attr_translation_key = "next_price_end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-end"

    def __init__(self, coordinator: PriceCoordinator) -> None:
        super().__init__(coordinator, "next_price_end")

    @property
    def native_value(self):
        snapshot = self.coordinator.data
        if snapshot is None or not snapshot.available or snapshot.result is None:
            return None
        now = snapshot.calculated_at
        run = next((run for run in _runs(snapshot.result.selected_slots) if now is not None and run[0].start > now), None)
        return run[-1].end.astimezone(_UTC) if run else None


class PriceDataStatusSensor(OptimizerEntity, SensorEntity):
    """Machine-readable source and planner status."""

    _attr_translation_key = "price_data_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_options = [
        "active", "scheduled", "above_threshold", "waiting_for_prices", "no_prices",
        "no_window", "price_ceiling", "deadline", "constraints_unsatisfied",
        "insufficient_duration", "source_unavailable", "invalid_data", "completed",
    ]
    _attr_icon = "mdi:information-outline"

    def __init__(self, coordinator: PriceCoordinator) -> None:
        super().__init__(coordinator, "price_data_status")

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None

    @property
    def native_value(self) -> str | None:
        snapshot = self.coordinator.data
        return snapshot.status if snapshot else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        snapshot = self.coordinator.data
        if snapshot is None:
            return {}
        attrs = _decision_attributes(snapshot)
        return {
            key: attrs[key]
            for key in (
                "available", "status", "active", "reason_code", "error_code",
                "current_price", "currency", "threshold_price_per_kwh",
                "threshold_comparison", "mode", "window_start", "window_end",
                "weekdays", "required_duration_minutes", "min_run_minutes",
                "price_ceiling_per_kwh", "fallback", "deadline", "provider",
                "next_change", "calculated_at", "last_success",
            )
        }


def _decision_attributes(snapshot) -> dict[str, object]:
    price_snapshot = snapshot.price_snapshot
    attrs = build_price_decision_attributes(
        snapshot.config,
        snapshot.result,
        status=snapshot.status,
        active=snapshot.active,
        reason_code=snapshot.reason_code,
        current_price=snapshot.current_price,
        currency=price_snapshot.currency if price_snapshot else None,
        error_code=snapshot.error_code,
        now=snapshot.calculated_at,
        last_success=snapshot.last_success,
    )
    attrs["available"] = snapshot.available
    attrs["provider"] = price_snapshot.provider if price_snapshot else None
    return attrs


def _runs(slots):
    """Group adjacent selected price slots into continuous operating periods."""
    runs = []
    for slot in slots:
        if runs and runs[-1][-1].end.astimezone(_UTC) == slot.start.astimezone(_UTC):
            runs[-1].append(slot)
        else:
            runs.append([slot])
    return runs

def _iso_utc(value) -> str | None:
    return value.astimezone(_UTC).isoformat() if value is not None else None
