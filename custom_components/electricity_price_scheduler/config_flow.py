"""Config and options flows for price optimization profiles."""

from __future__ import annotations

from collections.abc import Mapping
import math
import re
from typing import Any
from zoneinfo import ZoneInfo

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_MAX_CUTOFF_MINUTES,
    DEFAULT_MAX_PERIODS,
    DEFAULT_MIN_PRICE_DIFF_PER_KWH,
    DEFAULT_MULTIPLIER_MODE,
    DEFAULT_PREHEAT_FACTOR,
    DEFAULT_RECOVERY_FACTOR,
    DOMAIN,
    MAX_CUTOFF_MINUTES,
    MAX_UI_MAX_PERIODS,
    MULTIPLIER_MODE_FIXED,
    MULTIPLIER_MODE_OUTDOOR_TEMPERATURE,
)
from .core.models import OptimizerConfig, PriceSnapshot
from .price_flow import (
    PRESET_SCENARIOS,
    price_schema,
    scenario_selector,
    validate_price_settings,
)
from .providers import (
    ProviderConfigError,
    ProviderDataError,
    ProviderUnavailableError,
    async_fetch_snapshot,
    custom_price_coverage_expired,
    provider_config,
)

from .source_flow import discover_price_sources, source_key

_AUTOMATION_GUIDE_URL = (
    "https://github.com/nikopaulanne/electricity-price-scheduler"
    "/blob/main/docs/scenarios.md#connect-the-signal-to-an-automation"
)
_CURRENCY_OPTIONS = ["EUR", "NOK", "SEK", "DKK", "GBP", "PLN", "AUD", "USD", "CHF", "CAD", "NZD"]
_UNIT_OVERRIDE_OPTIONS = [
    "EUR/kWh", "EUR/MWh", "EUR/Wh",
    "NOK/kWh", "NOK/MWh", "NOK/Wh",
    "SEK/kWh", "SEK/MWh", "SEK/Wh",
    "DKK/kWh", "DKK/MWh", "DKK/Wh",
    "GBP/kWh", "GBP/MWh", "GBP/Wh",
    "PLN/kWh", "PLN/MWh", "PLN/Wh",
    "AUD/kWh", "AUD/MWh", "AUD/Wh",
    "USD/kWh", "USD/MWh", "USD/Wh",
    "CHF/kWh", "CHF/MWh", "CHF/Wh",
    "CAD/kWh", "CAD/MWh", "CAD/Wh",
    "NZD/kWh", "NZD/MWh", "NZD/Wh",
    "snt/kWh", "snt/MWh", "snt/Wh",
    "cent/kWh", "cent/MWh", "cent/Wh",
    "øre/kWh", "øre/MWh", "øre/Wh",
]
_TEMP_CELSIUS = {UnitOfTemperature.CELSIUS, "C", "°C"}
_TEMP_FAHRENHEIT = {UnitOfTemperature.FAHRENHEIT, "F", "°F"}
_TEMP_KELVIN = {UnitOfTemperature.KELVIN, "K"}


def _entity_selector() -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


_CUSTOM_PRICE_SOURCE_KEY = "custom_price_sensor"


def _market_source_choices(hass: HomeAssistant) -> dict[str, tuple[str, dict[str, Any]]]:
    """Include an explicit option for sensors using the supported price format."""
    choices = discover_price_sources(hass)
    choices[_CUSTOM_PRICE_SOURCE_KEY] = (
        "My price sensor",
        {"provider_type": _CUSTOM_PRICE_SOURCE_KEY},
    )
    return choices


def _market_source_selector(
    choices: dict[str, tuple[str, dict[str, Any]]],
) -> selector.SelectSelector:
    return selector.SelectSelector(selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(
                value=key,
                label=label,
            )
            for key, (label, _data) in choices.items()
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
        translation_key="custom_price_source",
    ))


def _text_selector() -> selector.TextSelector:
    return selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT))


def _number_selector(
    *, minimum: float, maximum: float, step: float, unit: str | None = None
) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def _select_selector(
    options: list[str], *, translation_key: str | None = None
) -> selector.SelectSelector:
    config = selector.SelectSelectorConfig(
        options=options, mode=selector.SelectSelectorMode.DROPDOWN
    )
    if translation_key is not None:
        config["translation_key"] = translation_key
    return selector.SelectSelector(config)


def _currency(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if re.fullmatch(r"[A-Z]{3}", normalized):
        return normalized
    return None


def _source_currency_and_unit(state: Any) -> tuple[str | None, bool]:
    """Return source currency and whether a user unit override is needed."""
    attributes = getattr(state, "attributes", {})
    currency = _currency(attributes.get("currency")) if isinstance(attributes, Mapping) else None
    energy_unit = attributes.get("unit") if isinstance(attributes, Mapping) else None
    cents = attributes.get("price_in_cents") if isinstance(attributes, Mapping) else None
    metadata_ok = (
        isinstance(energy_unit, str)
        and energy_unit.strip().lower() in {"kwh", "mwh", "wh"}
        and isinstance(cents, bool)
    )
    return currency, not metadata_ok


async def _async_default_profile_name(hass: HomeAssistant, preset: str) -> str:
    """Use the installation language for the editable suggested name."""
    language = hass.config.language or "en"
    messages = await async_get_translations(hass, language, "common", integrations={DOMAIN})
    return messages[f"component.{DOMAIN}.common.profile_name_{preset}"]


def _option_defaults(options: dict[str, Any], currency: str) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "max_cutoff_minutes": options.get(
            "max_cutoff_minutes", DEFAULT_MAX_CUTOFF_MINUTES
        ),
        "max_periods": options.get("max_periods", DEFAULT_MAX_PERIODS),
        "multiplier_mode": options.get("multiplier_mode", DEFAULT_MULTIPLIER_MODE),
        "preheat_factor": options.get("preheat_factor", DEFAULT_PREHEAT_FACTOR),
        "recovery_factor": options.get("recovery_factor", DEFAULT_RECOVERY_FACTOR),
        "temperature_entity_id": options.get("temperature_entity_id", ""),
    }
    if "min_price_diff_per_kwh" in options:
        defaults["min_price_diff_per_kwh"] = options["min_price_diff_per_kwh"]
    elif currency.upper() == "EUR":
        defaults["min_price_diff_per_kwh"] = DEFAULT_MIN_PRICE_DIFF_PER_KWH
    return defaults


def _settings_schema(
    defaults: dict[str, Any], currency: str, *, include_profile_name: bool = True,
    advanced: bool = False, user_input: dict[str, Any] | None = None,
) -> vol.Schema:
    submitted = user_input or {}
    max_cutoff = submitted.get("max_cutoff_minutes", defaults["max_cutoff_minutes"])
    threshold_default = submitted.get(
        "min_price_diff_per_kwh", defaults.get("min_price_diff_per_kwh", vol.UNDEFINED)
    )
    schema: dict[Any, Any] = {
        vol.Required("max_cutoff_minutes", default=max_cutoff): _number_selector(
            minimum=30, maximum=MAX_CUTOFF_MINUTES, step=15, unit="min"
        ),
        vol.Required("min_price_diff_per_kwh", default=threshold_default): _number_selector(
            minimum=0, maximum=100, step=0.001, unit=f"{currency}/kWh"
        ),
    }
    temperature = submitted.get("temperature_entity_id", defaults["temperature_entity_id"])
    schema[vol.Optional("temperature_entity_id", default=temperature or vol.UNDEFINED)] = _entity_selector()
    if advanced:
        schema.update({
            vol.Required("max_periods", default=submitted.get("max_periods", defaults["max_periods"])): _number_selector(
                minimum=1, maximum=MAX_UI_MAX_PERIODS, step=1
            ),
            vol.Required("multiplier_mode", default=submitted.get("multiplier_mode", defaults["multiplier_mode"])): _select_selector(
                [MULTIPLIER_MODE_FIXED, MULTIPLIER_MODE_OUTDOOR_TEMPERATURE],
                translation_key="multiplier_mode",
            ),
            vol.Required("preheat_factor", default=submitted.get("preheat_factor", defaults["preheat_factor"])): _number_selector(
                minimum=1, maximum=3, step=0.05
            ),
            vol.Required("recovery_factor", default=submitted.get("recovery_factor", defaults["recovery_factor"])): _number_selector(
                minimum=1, maximum=3, step=0.05
            ),
        })
        for key, unit_key, units, translation in (
            ("max_cutoff_entity_id", "max_cutoff_entity_unit", ["h", "min"], "duration_helper_unit"),
            ("min_price_diff_entity_id", "min_price_diff_entity_unit",
             ["currency_per_kwh", "subunit_per_kwh"], "price_helper_unit"),
        ):
            schema[vol.Optional(key, default=submitted.get(key) or defaults.get(key) or vol.UNDEFINED)] = (
                selector.EntitySelector(selector.EntitySelectorConfig(domain="input_number"))
            )
            schema[vol.Required(unit_key, default=submitted.get(unit_key, defaults.get(unit_key, units[0])))] = (
                _select_selector(units, translation_key=translation)
            )
    if include_profile_name:
        schema[vol.Required("profile_name", default=submitted.get("profile_name", defaults.get("profile_name", vol.UNDEFINED)))] = _text_selector()
    return vol.Schema(schema)


def _validate_settings(
    user_input: dict[str, Any], currency: str
) -> tuple[dict[str, Any] | None, str | None]:
    name = user_input.get("profile_name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        return None, "invalid_profile_name"
    try:
        max_cutoff_raw = user_input["max_cutoff_minutes"]
        threshold = float(user_input["min_price_diff_per_kwh"])
        max_cutoff_number = float(max_cutoff_raw)
        max_periods_raw = user_input.get("max_periods", DEFAULT_MAX_PERIODS)
        max_periods_number = float(max_periods_raw)
        preheat = float(user_input.get("preheat_factor", DEFAULT_PREHEAT_FACTOR))
        recovery = float(user_input.get("recovery_factor", DEFAULT_RECOVERY_FACTOR))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "invalid_settings"
    if any(
        isinstance(user_input.get(key), bool)
        for key in (
            "max_cutoff_minutes", "max_periods", "min_price_diff_per_kwh",
            "preheat_factor", "recovery_factor",
        )
        if key in user_input
    ):
        return None, "invalid_settings"
    if isinstance(max_cutoff_raw, bool) or not max_cutoff_number.is_integer():
        return None, "invalid_max_cutoff"
    if isinstance(max_periods_raw, bool) or not max_periods_number.is_integer():
        return None, "invalid_max_periods"
    max_cutoff = int(max_cutoff_number)
    max_periods = int(max_periods_number)
    if not 30 <= max_cutoff <= MAX_CUTOFF_MINUTES or max_cutoff % 15:
        return None, "invalid_max_cutoff"
    if not 1 <= max_periods <= MAX_UI_MAX_PERIODS:
        return None, "invalid_max_periods"
    if not math.isfinite(threshold) or threshold < 0:
        return None, "invalid_min_price_diff"
    if not math.isfinite(preheat) or not 1 <= preheat <= 3:
        return None, "invalid_preheat_factor"
    if not math.isfinite(recovery) or not 1 <= recovery <= 3:
        return None, "invalid_recovery_factor"
    temperature_entity_id = user_input.get("temperature_entity_id") or None
    mode = user_input.get(
        "multiplier_mode",
        MULTIPLIER_MODE_OUTDOOR_TEMPERATURE if temperature_entity_id else MULTIPLIER_MODE_FIXED,
    )
    if mode not in {MULTIPLIER_MODE_FIXED, MULTIPLIER_MODE_OUTDOOR_TEMPERATURE}:
        return None, "invalid_multiplier_mode"
    if mode == MULTIPLIER_MODE_OUTDOOR_TEMPERATURE:
        if not isinstance(temperature_entity_id, str) or "." not in temperature_entity_id:
            return None, "temperature_sensor_required"
    if temperature_entity_id and "." not in temperature_entity_id:
        return None, "invalid_temperature_sensor"

    # Reuse the core config validator contract at the UI boundary.
    try:
        OptimizerConfig(
            currency=currency,
            max_cutoff_minutes=max_cutoff,
            min_price_diff_per_kwh=threshold,
            max_periods=max_periods,
            preheat_factor=preheat,
            recovery_factor=recovery,
        )
    except (TypeError, ValueError):
        return None, "invalid_settings"
    return {
        "max_cutoff_minutes": max_cutoff,
        "min_price_diff_per_kwh": threshold,
        "max_periods": max_periods,
        "multiplier_mode": mode,
        "preheat_factor": preheat,
        "recovery_factor": recovery,
        "temperature_entity_id": temperature_entity_id,
    }, None


def _temperature_is_valid(hass: HomeAssistant, entity_id: str | None) -> bool:
    if not entity_id:
        return False
    state = hass.states.get(entity_id)
    if state is None:
        return False
    try:
        value = float(state.state)
    except (TypeError, ValueError, OverflowError):
        return False
    unit = getattr(state, "attributes", {}).get("unit_of_measurement")
    return math.isfinite(value) and unit in (
        _TEMP_CELSIUS | _TEMP_FAHRENHEIT | _TEMP_KELVIN
    )


def _provider_error(error: Exception) -> str:
    if isinstance(error, ProviderUnavailableError):
        return "source_unavailable"
    if isinstance(error, ProviderConfigError):
        return "currency_mismatch"
    if isinstance(error, ProviderDataError):
        message = str(error).lower()
        if "unit attribute" in message or "price_in_cents" in message or "unit override" in message:
            return "unit_override_required"
        if "currency" in message:
            return "currency_required"
        return "invalid_source_data"
    return "source_unavailable"


class NordpoolCutoffOptimizerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure a price source and optimizer profile."""

    VERSION = 1

    def __init__(self) -> None:
        self._source_data: dict[str, Any] = {}
        self._market_selection: str | None = None
        self._market_show_home = False
        self._preset = "heating"
        self._custom_price_source_data: dict[str, Any] = {}
        self._custom_reconfigure_options: dict[str, Any] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Open the scenario and price source picker."""
        return await self.async_step_market_source(user_input)

    async def _async_accept_source(self, data: dict[str, Any]):
        self._source_data = data
        scenario = PRESET_SCENARIOS[self._preset]
        if scenario != "hvac":
            self._source_data.update(scenario=scenario, preset=self._preset)
            return await self.async_step_price_setup()
        return await self.async_step_settings()

    async def async_step_market_source(self, user_input: dict[str, Any] | None = None):
        return await self._async_step_market_source(user_input, reconfigure=False)

    async def async_step_market_reconfigure(self, user_input: dict[str, Any] | None = None):
        return await self._async_step_market_source(user_input, reconfigure=True)

    async def _async_step_market_source(
        self, user_input: dict[str, Any] | None, *, reconfigure: bool,
    ):
        choices = _market_source_choices(self.hass)
        entry = self._get_reconfigure_entry() if reconfigure else None
        if entry is not None and self._market_selection is None:
            key = (
                _CUSTOM_PRICE_SOURCE_KEY
                if entry.data.get("custom_price_sensor")
                else source_key(entry.data)
            )
            if key in choices:
                self._market_selection = key
                self._market_show_home = bool(entry.data.get("home_name"))
        actual_sources = {
            key: choice for key, choice in choices.items()
            if key != _CUSTOM_PRICE_SOURCE_KEY
        }
        if not reconfigure and self._market_selection is None and len(actual_sources) == 1:
            self._market_selection = next(iter(actual_sources))
            if actual_sources[self._market_selection][1]["provider_type"] == "amber":
                self._preset = "cheap_now"
        elif not reconfigure and not actual_sources:
            self._market_selection = _CUSTOM_PRICE_SOURCE_KEY
        errors: dict[str, str] = {}
        price_input: dict[Any, Any] = {}
        if user_input is not None:
            selected = user_input.get("price_source")
            if not reconfigure:
                preset = user_input.get("scenario")
                if preset not in PRESET_SCENARIOS:
                    return self.async_abort(reason="invalid_scenario")
                self._preset = preset
            if selected not in choices:
                errors["price_source"] = "source_unavailable"
            else:
                if selected != self._market_selection:
                    self._market_show_home = False
                self._market_selection = selected

        chosen = choices.get(self._market_selection)
        source_data = dict(chosen[1]) if chosen else {}
        provider = source_data.get("provider_type")
        custom_price_sensor = self._market_selection == _CUSTOM_PRICE_SOURCE_KEY
        ask_currency = False
        needs_unit = False
        unit_override = None
        home_name = None
        if custom_price_sensor:
            source_data = {
                "provider_type": "community_nordpool",
                "custom_price_sensor": True,
            }
            previous = self._custom_price_source_data or (
                entry.data if entry and entry.data.get("custom_price_sensor") else {}
            )
            entity_id = (
                user_input.get("price_entity_id") if user_input
                else previous.get("price_entity_id")
            )
            if entity_id:
                source_data["price_entity_id"] = entity_id
                state = self.hass.states.get(entity_id)
                if state is None or str(state.state).lower() in {
                    "unknown", "unavailable", "none", "",
                }:
                    errors["price_entity_id"] = "source_unavailable"
                else:
                    currency, needs_unit = _source_currency_and_unit(state)
                    if currency:
                        source_data["currency"] = currency
                    if entity_id == previous.get("price_entity_id"):
                        if previous.get("currency"):
                            source_data.setdefault("currency", previous["currency"])
                        unit_override = previous.get("unit_override")
                    if user_input and user_input.get("unit_override"):
                        unit_override = user_input["unit_override"]
                    if needs_unit and unit_override:
                        source_data["unit_override"] = unit_override
            provider = "community_nordpool"
        elif provider == "community_nordpool":
            state = self.hass.states.get(source_data["price_entity_id"])
            currency, needs_unit = _source_currency_and_unit(state)
            if currency:
                source_data["currency"] = currency
            if entry and source_key(source_data) == source_key(entry.data):
                unit_override = entry.data.get("unit_override")
            if user_input and user_input.get("unit_override"):
                unit_override = user_input["unit_override"]
            if needs_unit and unit_override:
                source_data["unit_override"] = unit_override
        elif provider == "tibber":
            if entry and source_key(source_data) == source_key(entry.data):
                home_name = entry.data.get("home_name")
            if user_input and "home_name" in user_input:
                home_name = user_input["home_name"].strip() or None
            if home_name:
                source_data["home_name"] = home_name

        ask_currency = bool(
            ("price_entity_id" in source_data if custom_price_sensor else provider)
            and "currency" not in source_data
        )
        selected_currency = source_data.get("currency")
        if ask_currency and user_input:
            selected_currency = _currency(user_input.get("currency"))
            if selected_currency in _CURRENCY_OPTIONS:
                source_data["currency"] = selected_currency

        if (
            user_input is not None
            and not errors
            and (not custom_price_sensor or "price_entity_id" in source_data)
        ):
            scenario = entry.data.get("scenario") if entry else PRESET_SCENARIOS[self._preset]
            if provider == "amber" and scenario != "threshold":
                errors["price_source"] = "threshold_only_source"
            elif ask_currency and selected_currency not in _CURRENCY_OPTIONS:
                errors["currency"] = "currency_required"
            elif needs_unit and not unit_override:
                errors["unit_override"] = "unit_override_required"
            else:
                if custom_price_sensor:
                    source_data["currency"] = selected_currency
                    if unit_override:
                        source_data["unit_override"] = unit_override
                snapshot, error = await self._async_read_price_source(source_data)
                if error in {"tibber_home_required", "tibber_home_not_found"}:
                    self._market_show_home = True
                    errors["home_name"] = error
                elif error:
                    errors["base"] = error
                else:
                    options = None
                    if entry is not None:
                        options, price_input = self._market_reconfigure_options(
                            entry, source_data, user_input, errors,
                        )
                    if not errors:
                        if custom_price_sensor and snapshot is not None:
                            self._custom_price_source_data = source_data
                            self._custom_reconfigure_options = options
                            return self._show_custom_price_preview(snapshot)
                        if entry is None:
                            return await self._async_accept_source(source_data)
                        return self._apply_market_reconfigure(entry, source_data, options)

        schema: dict[Any, Any] = {}
        if not reconfigure:
            schema[vol.Required("scenario", default=self._preset)] = scenario_selector()
        schema[vol.Required("price_source", default=self._market_selection or vol.UNDEFINED)] = (
            _market_source_selector(choices)
        )
        if custom_price_sensor:
            schema[vol.Required(
                "price_entity_id",
                default=source_data.get("price_entity_id", vol.UNDEFINED),
            )] = _entity_selector()
        if ask_currency:
            schema[vol.Required("currency", default=selected_currency or vol.UNDEFINED)] = (
                _select_selector(_CURRENCY_OPTIONS)
            )
        if needs_unit:
            schema[vol.Required("unit_override", default=unit_override or vol.UNDEFINED)] = (
                _select_selector(_UNIT_OVERRIDE_OPTIONS)
            )
        if self._market_show_home:
            schema[vol.Required("home_name", default=home_name or vol.UNDEFINED)] = _text_selector()
        return self.async_show_form(
            step_id="market_reconfigure" if reconfigure else "market_source",
            data_schema=vol.Schema(schema | price_input),
            errors=errors,
        )

    def _market_reconfigure_options(self, entry, data, user_input, errors):
        """Preserve options and require a new monetary threshold when units change."""
        scenario = entry.data.get("scenario")
        price_key = {
            "threshold": "threshold_price_per_kwh",
            "schedule": "price_ceiling_per_kwh",
        }.get(scenario, "min_price_diff_per_kwh")
        currency = data["currency"]
        options = dict(entry.options)
        if currency != entry.data.get("currency"):
            if options.get("min_price_diff_entity_id"):
                errors["base"] = "currency_change_with_price_helper"
                return None, {}
            if scenario != "schedule" or price_key in options:
                price = user_input.get(price_key)
                if isinstance(price, (int, float)) and math.isfinite(price) and (scenario or price >= 0):
                    options[price_key] = float(price)
                else:
                    errors[price_key] = (
                        "currency_changed_threshold_required" if price is None else "invalid_price_value"
                    )
                    return None, {vol.Required(price_key): _number_selector(
                        minimum=-100 if scenario else 0, maximum=100,
                        step=0.001, unit=f"{currency}/kWh",
                    )}
        return options, {}

    def _apply_market_reconfigure(self, entry, data, options):
        if scenario := entry.data.get("scenario"):
            data = data | {"scenario": scenario, "preset": entry.data.get("preset", "appliance")}
        self.hass.config_entries.async_update_entry(entry, data=data, options=options)
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason="reconfigure_successful")

    async def _async_read_price_source(
        self, data: dict[str, Any],
    ) -> tuple[PriceSnapshot | None, str | None]:
        try:
            snapshot = await async_fetch_snapshot(
                self.hass, provider_config(data), today_local=dt_util.now().date(),
            )
        except (ProviderUnavailableError, ProviderConfigError, ProviderDataError) as err:
            if err.code in {"tibber_home_required", "tibber_home_not_found"}:
                return None, err.code
            return None, _provider_error(err)
        except (TypeError, ValueError, KeyError):
            return None, "invalid_source_data"
        if data.get("custom_price_sensor") and custom_price_coverage_expired(
            snapshot, dt_util.utcnow(),
        ):
            return None, "custom_price_coverage_expired"
        return snapshot, None

    async def async_step_custom_price_preview(self, user_input: dict[str, Any] | None = None):
        """Confirm the source after checking that it still has valid prices."""
        if user_input is not None and not user_input.get("continue"):
            return await self._async_step_market_source(
                None, reconfigure=self.source == config_entries.SOURCE_RECONFIGURE,
            )
        data = self._custom_price_source_data
        snapshot, error = await self._async_read_price_source(data)
        if error or user_input is None:
            return self._show_custom_price_preview(snapshot, error=error)
        if self._custom_reconfigure_options is not None:
            return self._apply_market_reconfigure(
                self._get_reconfigure_entry(), data, self._custom_reconfigure_options,
            )
        return await self._async_accept_source(data)

    def _show_custom_price_preview(
        self, snapshot: PriceSnapshot | None, *, error: str | None = None,
    ):
        now = dt_util.utcnow()
        current = next(
            (slot for slot in snapshot.slots if slot.start <= now < slot.end),
            None,
        ) if snapshot else None
        current_price = (
            f"{current.price_per_kwh:.6f}".rstrip("0").rstrip(".")
            if current else "—"
        )
        coverage_end = snapshot.coverage_end if snapshot else None
        local_end = dt_util.as_local(coverage_end).strftime("%Y-%m-%d %H:%M") if coverage_end else "—"
        return self.async_show_form(
            step_id="custom_price_preview",
            data_schema=vol.Schema({
                vol.Required("continue", default=error is None): selector.BooleanSelector(),
            }),
            errors={"base": error} if error else None,
            description_placeholders={
                "current_price": current_price,
                "currency": snapshot.currency if snapshot else self._custom_price_source_data["currency"],
                "slot_count": str(len(snapshot.slots)) if snapshot else "0",
                "coverage_end": local_end,
            },
        )

    async def async_step_price_setup(self, user_input: dict[str, Any] | None = None):
        """Collect only the values needed to start this price profile."""
        scenario = self._source_data["scenario"]
        currency = self._source_data["currency"]
        errors = {}
        if user_input is not None:
            options, error = validate_price_settings(scenario, user_input, preset=self._preset)
            if error:
                errors["base"] = error
            else:
                self._price_title = user_input["profile_name"].strip()
                self._price_options = options
                self._price_setup_input = user_input
                return await self.async_step_price_preview()
        return self.async_show_form(
            step_id="price_setup",
            data_schema=price_schema(
                scenario, user_input or {}, currency, setup=True, preset=self._preset,
                default_profile_name=await _async_default_profile_name(self.hass, self._preset),
            ),
            errors=errors,
            description_placeholders={
                "currency": currency,
            },
        )

    async def async_step_price_preview(self, user_input: dict[str, Any] | None = None):
        """Show the current decision without creating any entities yet."""
        if user_input is not None:
            if user_input.get("edit"):
                return self.async_show_form(
                    step_id="price_setup",
                    data_schema=price_schema(
                        self._source_data["scenario"], self._price_setup_input,
                        self._source_data["currency"], setup=True, preset=self._preset,
                        default_profile_name=await _async_default_profile_name(self.hass, self._preset),
                    ),
                    description_placeholders={"currency": self._source_data["currency"]},
                )
            return self.async_create_entry(
                title=self._price_title, data=self._source_data, options=self._price_options,
            )

        from .core.price_schedule import plan_price_schedule
        from .price_runtime import build_price_decision_attributes, parse_price_schedule_config

        source = provider_config(self._source_data)
        now = dt_util.utcnow()
        try:
            snapshot = await async_fetch_snapshot(
                self.hass, source, today_local=dt_util.now().date()
            )
            config = parse_price_schedule_config(self._source_data["scenario"], self._price_options)
            result = await self.hass.async_add_executor_job(
                plan_price_schedule, snapshot.slots, now, config, self.hass.config.time_zone
            )
            current_price = next(
                (slot.price_per_kwh for slot in snapshot.slots if slot.start <= now < slot.end),
                None,
            )
            explanation = build_price_decision_attributes(
                config,
                result,
                status=result.status,
                active=result.active,
                current_price=current_price,
                currency=snapshot.currency,
                now=now,
            )
            local = ZoneInfo(self.hass.config.time_zone)
            periods = explanation.get("selected_periods", [])
            placeholders = {
                "currency": str(snapshot.currency),
                "threshold": str(self._price_options.get("threshold_price_per_kwh", "")),
                "current_price": "" if current_price is None else str(current_price),
                "required_duration": str(
                    self._price_options.get("required_duration_minutes", "")
                ),
                "planned_duration": str(
                    sum(period.get("duration_minutes", 0) for period in periods)
                ),
                "start": "",
                "end": "",
                "next_start": "",
                "next_end": "",
            }
            if periods:
                first = periods[0]
                placeholders.update(
                    start=dt_util.parse_datetime(first["start"])
                    .astimezone(local)
                    .strftime("%Y-%m-%d %H:%M"),
                    end=dt_util.parse_datetime(first["end"])
                    .astimezone(local)
                    .strftime("%Y-%m-%d %H:%M"),
                )
            next_start = explanation.get("next_period_start")
            next_end = explanation.get("next_period_end")
            placeholders["next_start"] = (
                dt_util.parse_datetime(next_start).astimezone(local).strftime("%Y-%m-%d %H:%M")
                if next_start else ""
            )
            placeholders["next_end"] = (
                dt_util.parse_datetime(next_end).astimezone(local).strftime("%Y-%m-%d %H:%M")
                if next_end else ""
            )
            if self._source_data["scenario"] == "threshold":
                status = "no_prices" if current_price is None else result.status
                step = {
                    "active": "price_preview_threshold_active",
                    "scheduled": "price_preview_threshold_scheduled",
                    "above_threshold": "price_preview_threshold_above",
                    "no_prices": "price_preview_waiting",
                }.get(status, "price_preview_empty")
            else:
                step = {
                    "active": "price_preview_schedule_active",
                    "scheduled": "price_preview_schedule_scheduled",
                    "waiting_for_prices": "price_preview_waiting",
                    "no_prices": "price_preview_waiting",
                    "source_unavailable": "price_preview_unavailable",
                    "price_ceiling": "price_preview_price_ceiling",
                    "deadline": "price_preview_deadline",
                    "insufficient_duration": "price_preview_insufficient_duration",
                    "constraints_unsatisfied": "price_preview_constraints_unsatisfied",
                    "no_window": "price_preview_no_eligible",
                }.get(result.status, "price_preview_empty")
        except (ProviderUnavailableError, ProviderConfigError, ProviderDataError, ValueError):
            step = "price_preview_unavailable"
            placeholders = {"currency": self._source_data["currency"]}
        return self.async_show_form(
            step_id=step,
            description_placeholders={
                **placeholders, "automation_guide_url": _AUTOMATION_GUIDE_URL,
            },
            data_schema=vol.Schema({vol.Optional("edit", default=False): selector.BooleanSelector()}),
        )

    async def async_step_price_preview_empty(self, user_input=None):
        return await self.async_step_price_preview(user_input)

    async def async_step_price_preview_threshold_active(self, user_input=None):
        return await self.async_step_price_preview(user_input)

    async def async_step_price_preview_threshold_scheduled(self, user_input=None):
        return await self.async_step_price_preview(user_input)

    async def async_step_price_preview_threshold_above(self, user_input=None):
        return await self.async_step_price_preview(user_input)

    async def async_step_price_preview_schedule_active(self, user_input=None):
        return await self.async_step_price_preview(user_input)

    async def async_step_price_preview_schedule_scheduled(self, user_input=None):
        return await self.async_step_price_preview(user_input)

    async def async_step_price_preview_waiting(self, user_input=None):
        return await self.async_step_price_preview(user_input)

    async def async_step_price_preview_price_ceiling(self, user_input=None):
        return await self.async_step_price_preview(user_input)

    async def async_step_price_preview_deadline(self, user_input=None):
        return await self.async_step_price_preview(user_input)

    async def async_step_price_preview_insufficient_duration(self, user_input=None):
        return await self.async_step_price_preview(user_input)

    async def async_step_price_preview_constraints_unsatisfied(self, user_input=None):
        return await self.async_step_price_preview(user_input)

    async def async_step_price_preview_no_eligible(self, user_input=None):
        return await self.async_step_price_preview(user_input)

    async def async_step_price_preview_unavailable(self, user_input=None):
        return await self.async_step_price_preview(user_input)

    async def async_step_settings(self, user_input: dict[str, Any] | None = None):
        """Set initial profile options and create the config entry."""
        currency = self._source_data.get("currency", "EUR")
        defaults = _option_defaults({}, currency)
        errors: dict[str, str] = {}
        if user_input is not None:
            options, error = _validate_settings(user_input, currency)
            if error:
                errors["base"] = error
            elif options["multiplier_mode"] == MULTIPLIER_MODE_OUTDOOR_TEMPERATURE and not _temperature_is_valid(
                self.hass, options["temperature_entity_id"]
            ):
                errors["temperature_entity_id"] = "invalid_temperature_sensor"
            else:
                return self.async_create_entry(
                    title=user_input["profile_name"].strip(),
                    data=self._source_data,
                    options=options,
                )
        defaults["profile_name"] = await _async_default_profile_name(self.hass, "heating")
        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(
                defaults, currency, user_input=user_input, advanced=False
            ),
            errors=errors,
            description_placeholders={"currency": currency},
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Always expose the full source picker while preserving current source selection."""
        return await self.async_step_market_reconfigure(user_input)

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Return an options flow; current HA supplies its entry after init."""
        return NordpoolCutoffOptimizerOptionsFlow()


class NordpoolCutoffOptimizerOptionsFlow(config_entries.OptionsFlow):
    """Change model settings without changing the price source."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        entry = self.config_entry
        if entry.data.get("scenario") in {"threshold", "schedule"}:
            return await self.async_step_price_options(user_input)
        currency = entry.data.get("currency", "EUR")
        defaults = _option_defaults(dict(entry.options), currency)
        for key in (
            "max_cutoff_entity_id", "max_cutoff_entity_unit",
            "min_price_diff_entity_id", "min_price_diff_entity_unit",
        ):
            if key in entry.options:
                defaults[key] = entry.options[key]
        errors: dict[str, str] = {}
        if user_input is not None:
            options, error = _validate_settings(
                {
                    "profile_name": entry.title,
                    **user_input,
                    # A blank optional entity selector is omitted by HA.
                    # Treat omission as an intentional clear in the options flow.
                    "temperature_entity_id": user_input.get("temperature_entity_id"),
                },
                currency,
            )
            if error:
                if error == "temperature_sensor_required":
                    errors["temperature_entity_id"] = error
                else:
                    errors["base"] = error
            else:
                for key, unit_key, units in (
                    ("max_cutoff_entity_id", "max_cutoff_entity_unit", {"h", "min"}),
                    ("min_price_diff_entity_id", "min_price_diff_entity_unit", {"currency_per_kwh", "subunit_per_kwh"}),
                ):
                    entity_id = user_input.get(key)
                    if entity_id:
                        if not entity_id.startswith("input_number.") or user_input.get(unit_key) not in units:
                            errors[key] = "invalid_helper"
                        else:
                            options[key] = entity_id
                            options[unit_key] = user_input[unit_key]
                if options["multiplier_mode"] == MULTIPLIER_MODE_FIXED:
                    options["temperature_entity_id"] = None
                elif not _temperature_is_valid(self.hass, options["temperature_entity_id"]):
                    errors["temperature_entity_id"] = "invalid_temperature_sensor"
                if not errors:
                    return self.async_create_entry(title="", data=options)
        return self.async_show_form(
            step_id="init",
            data_schema=_settings_schema(
                defaults, currency, include_profile_name=False, advanced=True,
                user_input=user_input,
            ),
            errors=errors,
            description_placeholders={"currency": currency},
        )

    async def async_step_price_options(self, user_input: dict[str, Any] | None = None):
        entry = self.config_entry
        scenario = entry.data["scenario"]
        preset = entry.data.get("preset", "appliance")
        currency = entry.data.get("currency", "EUR")
        errors = {}
        if user_input is not None:
            options, error = validate_price_settings(scenario, user_input, preset=preset)
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(title="", data=options)
        return self.async_show_form(
            step_id="price_options",
            data_schema=price_schema(
                scenario, dict(entry.options) if user_input is None else user_input,
                currency, setup=False, preset=preset
            ),
            errors=errors,
            description_placeholders={"currency": currency},
        )
