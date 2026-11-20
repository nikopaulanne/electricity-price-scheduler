"""Forms for price threshold and cheapest time profiles."""

from __future__ import annotations

from datetime import time
import math
from typing import Any

import voluptuous as vol

from homeassistant.helpers import selector

PRESET_SCENARIOS = {
    "heating": "hvac",
    "cheap_now": "threshold",
    "water_heater": "schedule",
    "appliance": "schedule",
    "ev": "schedule",
}
_DAY_OPTIONS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def scenario_selector() -> selector.SelectSelector:
    return _select(list(PRESET_SCENARIOS), "scenario")


def _select(options: list[str], translation_key: str) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(options=options, translation_key=translation_key)
    )


def _number(
    minimum: float, maximum: float, step: float, unit: str
) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


def price_schema(
    scenario: str,
    values: dict[str, Any],
    currency: str,
    *,
    setup: bool,
    preset: str = "appliance",
    default_profile_name: str | None = None,
) -> vol.Schema:
    def required(key, default=vol.UNDEFINED):
        return vol.Required(key, default=values.get(key, default))

    def optional(key):
        return vol.Optional(key, default=values.get(key, vol.UNDEFINED))

    schema: dict[Any, Any] = {}
    price = _number(-100, 100, 0.001, f"{currency}/kWh")
    if setup:
        schema[required(
            "profile_name", default_profile_name or vol.UNDEFINED
        )] = selector.TextSelector()
    if scenario == "threshold":
        default_price = 0.05 if currency == "EUR" else 0
        schema[required("threshold_price_per_kwh", default_price)] = price
        if setup:
            return vol.Schema(schema)
    else:
        schema[required("required_duration_minutes", 180)] = _number(15, 1440, 15, "min")
        if setup:
            schema[required("window_start", "22:00:00")] = selector.TimeSelector()
            schema[required("window_end", "07:00:00")] = selector.TimeSelector()
            return vol.Schema(schema)

    schema[optional("window_start")] = selector.TimeSelector()
    schema[optional("window_end")] = selector.TimeSelector()
    selected_days = values.get("weekdays")
    default_days = (
        [_DAY_OPTIONS[day] for day in selected_days]
        if selected_days and all(isinstance(day, int) for day in selected_days)
        else selected_days or vol.UNDEFINED
    )
    schema[vol.Optional("weekdays", default=default_days)] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=_DAY_OPTIONS, translation_key="weekdays", multiple=True
        )
    )
    if scenario == "schedule":
        default_mode = "cheapest_split" if preset == "ev" else "cheapest_contiguous"
        schema[required("mode", default_mode)] = _select(
            ["cheapest_contiguous", "cheapest_split"], "schedule_mode"
        )
        schema[required("min_run_minutes", 15)] = _number(15, 1440, 15, "min")
        schema[optional("price_ceiling_per_kwh")] = price
        schema[required("fallback", "price")] = _select(
            ["price", "deadline"], "fallback"
        )
    return vol.Schema(schema)


def validate_price_settings(
    scenario: str, values: dict[str, Any], *, preset: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and normalize form data for the runtime."""
    name = values.get("profile_name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        return None, "invalid_profile_name"

    def number(key: str, default: Any = None) -> float:
        raw = values.get(key, default)
        if isinstance(raw, bool):
            raise ValueError(key)
        numeric = float(raw)
        if not math.isfinite(numeric):
            raise ValueError(key)
        return numeric

    try:
        start = values.get("window_start") or None
        end = values.get("window_end") or None
        if bool(start) != bool(end) or (scenario == "schedule" and not start):
            return None, "invalid_window"
        options: dict[str, Any] = {}
        if start and end:
            start_time = time.fromisoformat(start)
            end_time = time.fromisoformat(end)
            if start_time == end_time or start_time.tzinfo or end_time.tzinfo:
                return None, "invalid_window"
            options.update(window_start=start_time.isoformat(), window_end=end_time.isoformat())
        days = values.get("weekdays") or []
        if days:
            options["weekdays"] = sorted({_DAY_OPTIONS.index(day) for day in days})
        if scenario == "threshold":
            options.update(mode="threshold", threshold_price_per_kwh=number("threshold_price_per_kwh"))
            return options, None
        duration = number("required_duration_minutes")
        min_run = number("min_run_minutes", 15)
        mode = values.get("mode", "cheapest_split" if preset == "ev" else "cheapest_contiguous")
        fallback = values.get("fallback", "price")
        if mode not in {"cheapest_contiguous", "cheapest_split"} or fallback not in {"price", "deadline"}:
            return None, "invalid_settings"
        if not 15 <= duration <= 1440 or duration % 15:
            return None, "invalid_schedule_duration"
        if mode == "cheapest_split" and (not 15 <= min_run <= duration or min_run % 15):
            return None, "invalid_schedule_duration"
        options.update(
            required_duration_minutes=int(duration),
            min_run_minutes=int(min_run) if mode == "cheapest_split" else 15,
            mode=mode,
            fallback=fallback,
        )
        if values.get("price_ceiling_per_kwh") is not None:
            options["price_ceiling_per_kwh"] = number("price_ceiling_per_kwh")
        return options, None
    except (TypeError, ValueError, OverflowError):
        return None, "invalid_settings"
