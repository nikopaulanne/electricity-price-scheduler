"""Adapter for Home Assistant's public Tibber ``get_prices`` action."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone

from ..core.models import PriceSnapshot
from ..core.normalization import PriceDataError, normalize_slots
from ._helpers import bounded_rows, local_timezone, parse_aware_datetime, total_rows_within_limit
from .base import (
    ProviderConfigError,
    ProviderDataError,
    ProviderHass,
    ProviderUnavailableError,
    TibberConfig,
)

ACTION_DOMAIN = "tibber"
ACTION_NAME = "get_prices"
ACTION_TIMEOUT_SECONDS = 30.0


class TibberHomeRequired(ProviderConfigError):
    code = "tibber_home_required"


class TibberHomeNotFound(ProviderConfigError):
    code = "tibber_home_not_found"


class TibberProvider:
    """Fetch already-published prices from the configured Tibber home."""

    async def async_fetch_snapshot(
        self,
        hass: ProviderHass,
        config: TibberConfig,
        *,
        today_local: date,
    ) -> PriceSnapshot:
        if not isinstance(config, TibberConfig):
            raise ProviderConfigError("Tibber provider config is required")
        currency = _required_text(config.currency, "Tibber currency").upper()
        home_name = config.home_name.strip() if isinstance(config.home_name, str) else None
        if home_name == "":
            home_name = None
        if not isinstance(today_local, date) or hasattr(today_local, "hour"):
            raise ProviderConfigError("today_local must be a local calendar date")

        entries = getattr(hass, "config_entries", None)
        if entries is not None and len(entries.async_entries(ACTION_DOMAIN)) != 1:
            raise ProviderConfigError("Tibber price selection requires exactly one configured account")
        zone = local_timezone(hass)
        start = datetime.combine(today_local, datetime.min.time(), tzinfo=zone)
        end = datetime.combine(
            date.fromordinal(today_local.toordinal() + 2), datetime.min.time(), tzinfo=zone
        )
        try:
            response = await asyncio.wait_for(
                hass.services.async_call(
                    ACTION_DOMAIN,
                    ACTION_NAME,
                    {"start": start.isoformat(), "end": end.isoformat()},
                    blocking=True,
                    return_response=True,
                ),
                timeout=ACTION_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            raise ProviderUnavailableError("Tibber price action failed") from err

        if not isinstance(response, Mapping):
            raise ProviderDataError("Tibber action returned an invalid response")
        homes = response.get("prices")
        if not isinstance(homes, Mapping) or not homes:
            raise ProviderUnavailableError("Tibber returned no home prices")
        if home_name is None:
            if len(homes) != 1:
                raise TibberHomeRequired("Select a Tibber home")
            home_name = next(iter(homes))
        if home_name not in homes:
            raise TibberHomeNotFound("Selected Tibber home was not returned")

        rows = bounded_rows(homes[home_name], field="Tibber prices")
        total_rows_within_limit(rows)
        tomorrow_local = date.fromordinal(today_local.toordinal() + 1)
        local_dates = {today_local, tomorrow_local}
        dated: list[tuple[datetime, object]] = []
        for row in rows:
            start_value = _field(row, "start_time")
            parsed = parse_aware_datetime(start_value, field="Tibber start_time")
            if parsed.astimezone(zone).date() in local_dates:
                dated.append((parsed.astimezone(timezone.utc), row))
        dated.sort(key=lambda item: item[0])
        if not dated or not any(stamp.astimezone(zone).date() == today_local for stamp, _ in dated):
            raise ProviderUnavailableError("Tibber has no prices for the current HA-local day")

        # The action provides starts only. Infer the supported cadence from the
        # timestamps and leave gaps intact instead of extending a preceding price.
        if len(dated) < 2:
            raise ProviderDataError("Tibber returned fewer than two price starts")
        if any(stamp.minute % 15 or stamp.second or stamp.microsecond for stamp, _ in dated):
            raise ProviderDataError("Tibber price times are not quarter-hour aligned")
        duration = timedelta(minutes=15 if any(stamp.minute for stamp, _ in dated) else 60)
        if not any(right[0] - left[0] == duration for left, right in zip(dated, dated[1:])):
            raise ProviderDataError("Tibber price cadence could not be verified")
        intervals = [
            {"start": stamp, "end": stamp + duration, "price": _field(row, "price")}
            for stamp, row in dated
        ]

        try:
            slots = normalize_slots(
                intervals,
                source_unit="currency/kWh",
                currency=currency,
            )
        except PriceDataError as err:
            raise ProviderDataError(str(err)) from err
        slots = tuple(
            slot for slot in slots if slot.start.astimezone(zone).date() in local_dates
        )
        if not slots or not any(slot.start.astimezone(zone).date() == today_local for slot in slots):
            raise ProviderUnavailableError("Tibber returned no valid current-day prices")

        tomorrow_available = any(
            slot.start.astimezone(zone).date() == tomorrow_local for slot in slots
        )
        warnings = ["tibber_prices_include_energy_and_taxes"]
        if not tomorrow_available:
            warnings.append("tibber_tomorrow_prices_unavailable")
        return PriceSnapshot(
            slots=slots,
            currency=currency,
            provider="tibber",
            coverage_start=slots[0].start,
            coverage_end=slots[-1].end,
            tomorrow_available=tomorrow_available,
            warnings=tuple(warnings),
        )


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderConfigError(f"{label} must be configured")
    return value.strip()


def _field(row: object, name: str) -> object:
    if isinstance(row, Mapping):
        if name not in row:
            raise ProviderDataError(f"Tibber price is missing {name!r}")
        return row[name]
    try:
        return getattr(row, name)
    except AttributeError as err:
        raise ProviderDataError(f"Tibber price is missing {name!r}") from err
