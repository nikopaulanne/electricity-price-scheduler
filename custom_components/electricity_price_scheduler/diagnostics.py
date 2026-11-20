"""Bounded diagnostics that exclude credentials, IDs and raw price history."""

from __future__ import annotations

from datetime import timezone
from typing import TYPE_CHECKING, Any

from . import DATA_ENTRIES
from .const import DOMAIN
from .coordinator import OptimizerCoordinator

_UTC = timezone.utc

if TYPE_CHECKING:  # pragma: no cover - static typing only
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return status/counters and safe settings without entity IDs or prices."""
    coordinator: OptimizerCoordinator | None = (
        hass.data.get(DOMAIN, {}).get(DATA_ENTRIES, {}).get(entry.entry_id)
    )
    snapshot = coordinator.data if coordinator is not None else None
    options = entry.options
    scenario = entry.data.get("scenario", "hvac")
    diagnostics: dict[str, Any] = {
        "provider_type": (
            "custom_price_sensor" if entry.data.get("custom_price_sensor") is True
            else entry.data.get("provider_type")
        ),
        "currency": entry.data.get("currency"),
        "scenario": scenario,
        "options": {
            key: options.get(key)
            for key in (
                "max_cutoff_minutes",
                "min_price_diff_per_kwh",
                "max_periods",
                "multiplier_mode",
                "preheat_factor",
                "recovery_factor",
                "max_cutoff_entity_unit",
                "min_price_diff_entity_unit",
                "mode",
                "required_duration_minutes",
                "min_run_minutes",
                "fallback",
            )
            if key in options
        },
    }
    if snapshot is None:
        diagnostics["runtime"] = {"status": "not_loaded"}
        return diagnostics

    if scenario in {"threshold", "schedule"}:
        diagnostics["runtime"] = {
            "status": snapshot.status,
            "available": snapshot.available,
            "error_code": snapshot.error_code,
            "reason_code": snapshot.reason_code,
            "next_change": _iso_utc(snapshot.result.next_transition) if snapshot.result else None,
            "last_success": _iso_utc(snapshot.last_success),
            "calculated_at": _iso_utc(snapshot.calculated_at),
        }
        return diagnostics

    diagnostics["runtime"] = {
        "status": snapshot.status.value,
        "decision_reason": snapshot.decision_reason,
        "next_change": _iso_utc(snapshot.next_change),
        "effective_settings": snapshot.effective_settings,
        "locked": snapshot.locked_period is not None,
        "locked_effective_settings": snapshot.locked_effective_settings,
        "available": snapshot.available,
        "error_code": snapshot.error_code,
        "provider": snapshot.price_snapshot.provider if snapshot.price_snapshot else None,
        "slot_count": snapshot.result.slot_count if snapshot.result else None,
        "candidates_scanned": snapshot.result.candidates_scanned if snapshot.result else None,
        "candidates_accepted": snapshot.result.candidates_accepted if snapshot.result else None,
        "period_count": len(snapshot.result.periods) if snapshot.result else None,
        "tomorrow_available": (
            snapshot.price_snapshot.tomorrow_available
            if snapshot.price_snapshot
            else None
        ),
        "last_success": _iso_utc(snapshot.last_success),
        "calculated_at": _iso_utc(snapshot.calculated_at),
        "warnings": list(snapshot.warnings),
    }
    return diagnostics


def _iso_utc(value) -> str | None:
    return value.astimezone(_UTC).isoformat() if value else None
