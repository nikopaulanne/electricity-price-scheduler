"""Adapter for the community ``custom-components/nordpool`` integration.

This reads only the selected entity's public state attributes. The source's
``raw_today`` values are already transformed by that integration and may
include VAT/additional-cost templates; see the snapshot warning.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date

from ..const import DOMAIN
from ..core.models import PriceSnapshot
from ..core.normalization import PriceDataError, normalize_slots
from ._helpers import (
    bounded_rows,
    local_timezone,
    row_field,
    rows_for_local_dates,
    total_rows_within_limit,
)
from .base import (
    CommunityNordpoolConfig,
    ProviderConfigError,
    ProviderDataError,
    ProviderHass,
    ProviderUnavailableError,
)

_SNAPSHOT_CACHE = f"{DOMAIN}.community_price_snapshots"
_MAX_CACHE_ENTRIES = 8


class CommunityNordpoolProvider:
    """Fetch already-published price intervals from a configured sensor."""

    async def async_fetch_snapshot(
        self,
        hass: ProviderHass,
        config: CommunityNordpoolConfig,
        *,
        today_local: date,
    ) -> PriceSnapshot:
        if not isinstance(config, CommunityNordpoolConfig):
            raise ProviderConfigError("Community Nordpool provider config is required")
        entity_id = config.price_entity_id
        if not isinstance(entity_id, str) or "." not in entity_id:
            raise ProviderConfigError("A valid Nordpool price sensor must be selected")

        state = hass.states.get(entity_id)
        if state is None:
            raise ProviderUnavailableError("Selected Nordpool sensor does not exist")
        state_value = getattr(state, "state", None)
        if not isinstance(state_value, str) or state_value.lower() in {
            "unknown", "unavailable", "none", "",
        }:
            raise ProviderUnavailableError("Selected Nordpool sensor is unavailable")

        attributes = getattr(state, "attributes", None)
        if not isinstance(attributes, Mapping):
            raise ProviderDataError("Selected sensor has no readable attributes")
        # HA replaces the State object on every state/attribute update. The
        # normalized snapshot can be shared by profiles while that source
        # state, local day, timezone and unit settings remain unchanged.
        zone = local_timezone(hass)
        hass_data = getattr(hass, "data", None)
        cache = hass_data.setdefault(_SNAPSHOT_CACHE, {}) if hass_data is not None else None
        cache_key = (config, today_local, zone)
        if cache is not None:
            cached = cache.pop(cache_key, None)
            if cached is not None and cached[0] is state:
                cache[cache_key] = cached
                return cached[1]

        source_currency = attributes.get("currency")
        configured_currency = _validate_currency(config.currency)
        # The setup flow asks for an explicit currency when source metadata
        # omits it. A supplied source currency still has authority over config.
        if isinstance(source_currency, str) and source_currency.strip() and source_currency.strip().upper() != configured_currency:
            raise ProviderConfigError(
                "Configured currency does not match the selected sensor currency"
            )

        try:
            raw_today = bounded_rows(attributes.get("raw_today"), field="raw_today")
        except ProviderDataError as err:
            if "must be a list" in str(err):
                raise ProviderUnavailableError(
                    "Selected Nordpool sensor has no current-day price list"
                ) from err
            raise
        raw_tomorrow_value = attributes.get("raw_tomorrow", ())
        if raw_tomorrow_value is None:
            raw_tomorrow_value = ()
        raw_tomorrow = bounded_rows(raw_tomorrow_value, field="raw_tomorrow")
        total_rows_within_limit(raw_today, raw_tomorrow)

        tomorrow_local = date.fromordinal(today_local.toordinal() + 1)
        local_dates = {today_local, tomorrow_local}
        today_rows = tuple(
            rows_for_local_dates(raw_today, dates={today_local}, timezone=zone)
        )
        tomorrow_rows = tuple(
            rows_for_local_dates(raw_tomorrow, dates={tomorrow_local}, timezone=zone)
        )
        if not today_rows:
            raise ProviderUnavailableError(
                "Selected Nordpool sensor has no prices for the current HA-local day"
            )

        source_unit, divisor = _source_unit_and_divisor(
            attributes=attributes,
            currency=configured_currency,
            unit_override=config.unit_override,
        )
        all_rows = _adapt_rows(today_rows, tomorrow_rows, value_divisor=divisor)
        try:
            slots = normalize_slots(
                all_rows, source_unit=source_unit, currency=configured_currency
            )
        except PriceDataError as err:
            raise ProviderDataError(str(err)) from err

        # Guard against a source that returns a row outside the requested
        # local-day window despite the attribute names.
        slots = tuple(
            slot
            for slot in slots
            if slot.start.astimezone(zone).date() in local_dates
        )
        if not any(slot.start.astimezone(zone).date() == today_local for slot in slots):
            raise ProviderUnavailableError(
                "Selected Nordpool sensor has no normalized current-day prices"
            )

        warnings = ["community_prices_may_include_vat_or_additional_costs"]
        if not tomorrow_rows:
            warnings.append("community_tomorrow_prices_unavailable")

        snapshot = PriceSnapshot(
            slots=slots,
            currency=configured_currency,
            provider="community_nordpool",
            coverage_start=slots[0].start if slots else None,
            coverage_end=slots[-1].end if slots else None,
            tomorrow_available=bool(tomorrow_rows),
            warnings=tuple(warnings),
        )
        if cache is not None:
            if len(cache) >= _MAX_CACHE_ENTRIES:
                del cache[next(iter(cache))]
            cache[cache_key] = (state, snapshot)
        return snapshot


def _validate_currency(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderConfigError("A price currency must be configured")
    return value.strip().upper()


def _source_unit_and_divisor(
    *, attributes: Mapping[str, object], currency: str, unit_override: str | None
) -> tuple[str, float]:
    if unit_override is not None:
        if not isinstance(unit_override, str) or not unit_override.strip():
            raise ProviderConfigError("Explicit source unit override is empty")
        # An override describes the actual raw value unit in full; it also
        # resolves whether values have already been converted to minor units.
        return unit_override.strip(), 1.0

    energy_unit = attributes.get("unit")
    if not isinstance(energy_unit, str) or energy_unit.strip().lower() not in {
        "kwh", "mwh", "wh",
    }:
        raise ProviderDataError(
            "Nordpool sensor is missing a supported unit attribute; configure an explicit unit override"
        )
    energy_unit = energy_unit.strip().lower()
    cents = attributes.get("price_in_cents")
    if not isinstance(cents, bool):
        raise ProviderDataError(
            "Nordpool sensor is missing price_in_cents metadata; configure an explicit unit override"
        )
    # The community integration exposes value in its selected energy scale,
    # and when price_in_cents is true it has already multiplied by 100. Divide
    # the value here, then let the core convert currency/<energy-unit> to kWh.
    return f"{currency}/{energy_unit}", 100.0 if cents else 1.0


def _adapt_rows(
    today_rows: tuple[object, ...],
    tomorrow_rows: tuple[object, ...],
    *,
    value_divisor: float,
) -> Iterator[object]:
    for row in (*today_rows, *tomorrow_rows):
        if value_divisor == 1.0:
            yield row
            continue
        value = row_field(row, "value")
        if isinstance(value, bool):
            raise ProviderDataError("Nordpool price value must be numeric")
        try:
            normalized_value = float(value) / value_divisor
        except (TypeError, ValueError, OverflowError) as err:
            raise ProviderDataError("Nordpool price value must be numeric") from err
        yield {
            "start": row_field(row, "start"),
            "end": row_field(row, "end"),
            "value": normalized_value,
        }
