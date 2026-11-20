"""Adapter for Home Assistant's built-in Nord Pool public action.

The action shape and currency/MWh units follow the official HA action.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..core.models import PriceSnapshot
from ..core.normalization import PriceDataError, normalize_slots
from ._helpers import (
    bounded_rows,
    local_timezone,
    rows_for_local_dates,
    total_rows_within_limit,
)
from .base import (
    CoreNordpoolConfig,
    ProviderConfigError,
    ProviderDataError,
    ProviderHass,
    ProviderUnavailableError,
)

ACTION_DOMAIN = "nordpool"
ACTION_NAME = "get_prices_for_date"
ACTION_TIMEOUT_SECONDS = 30.0
_MARKET_ZONE = ZoneInfo("Europe/Oslo")


class CoreNordpoolProvider:
    """Fetch today and tomorrow through the documented HA action API."""

    async def async_fetch_snapshot(
        self,
        hass: ProviderHass,
        config: CoreNordpoolConfig,
        *,
        today_local: date,
    ) -> PriceSnapshot:
        if not isinstance(config, CoreNordpoolConfig):
            raise ProviderConfigError("Core Nord Pool provider config is required")
        entry_id = _required_text(config.source_config_entry_id, "Nord Pool config entry")
        area = _required_text(config.area, "Nord Pool market area")
        currency = _required_text(config.currency, "Nord Pool currency").upper()
        if not isinstance(today_local, date) or hasattr(today_local, "hour"):
            raise ProviderConfigError("today_local must be a local calendar date")

        zone = local_timezone(hass)
        tomorrow = today_local + timedelta(days=1)
        horizon_start = datetime.combine(today_local, time.min, tzinfo=zone)
        horizon_end = datetime.combine(tomorrow + timedelta(days=1), time.min, tzinfo=zone)
        primary = datetime.combine(today_local, time(12), tzinfo=zone).astimezone(_MARKET_ZONE).date()
        primary_tomorrow = datetime.combine(tomorrow, time(12), tzinfo=zone).astimezone(_MARKET_ZONE).date()
        first_market_date = horizon_start.astimezone(_MARKET_ZONE).date()
        last_market_date = (horizon_end.astimezone(_MARKET_ZONE) - timedelta(microseconds=1)).date()
        neighbours = {
            first_market_date + timedelta(days=offset)
            for offset in range((last_market_date - first_market_date).days + 1)
        } - {primary, primary_tomorrow}

        async def fetch(requested_date: date) -> tuple[object, ...]:
            return await self._fetch_day(
                hass, entry_id=entry_id, area=area, currency=currency,
                requested_date=requested_date,
            )

        payloads = [await fetch(primary)]
        tomorrow_available = False
        warnings: list[str] = []
        for market_date in (primary_tomorrow, *sorted(neighbours)):
            try:
                payloads.append(await fetch(market_date))
            except ProviderUnavailableError:
                if market_date == primary_tomorrow:
                    warnings.append("core_nordpool_tomorrow_prices_unavailable")
            else:
                if market_date == primary_tomorrow:
                    tomorrow_available = True

        total_rows_within_limit(*payloads)
        rows = (
            row for payload in payloads
            for row in rows_for_local_dates(payload, dates={today_local, tomorrow}, timezone=zone)
        )
        try:
            slots = normalize_slots(
                rows,
                source_unit="currency/MWh",
                currency=currency,
            )
        except PriceDataError as err:
            raise ProviderDataError(str(err)) from err

        if not slots or not any(slot.start.astimezone(zone).date() == today_local for slot in slots):
            raise ProviderUnavailableError("Nord Pool returned no valid current-day prices")

        return PriceSnapshot(
            slots=slots,
            currency=currency,
            provider="core_nordpool",
            coverage_start=slots[0].start,
            coverage_end=slots[-1].end,
            tomorrow_available=tomorrow_available and slots[-1].start.astimezone(zone).date() == tomorrow,
            warnings=tuple(warnings),
        )

    async def _fetch_day(
        self,
        hass: ProviderHass,
        *,
        entry_id: str,
        area: str,
        currency: str,
        requested_date: date,
    ) -> tuple[object, ...]:
        service_data: dict[str, object] = {
            "config_entry": entry_id,
            "date": requested_date.isoformat(),
            "areas": [area],
            "currency": currency,
        }
        try:
            response = await asyncio.wait_for(
                hass.services.async_call(
                    ACTION_DOMAIN,
                    ACTION_NAME,
                    service_data,
                    blocking=True,
                    return_response=True,
                ),
                timeout=ACTION_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            error = ProviderUnavailableError(
                f"Nord Pool price action failed for {requested_date.isoformat()}"
            )
            raise error from err

        if not isinstance(response, Mapping):
            raise ProviderDataError("Nord Pool action returned an invalid response")
        if area not in response:
            raise ProviderUnavailableError("Nord Pool returned no prices for the market date")
        rows = bounded_rows(response[area], field=f"Nord Pool {area} response")
        if not rows:
            raise ProviderUnavailableError("Nord Pool returned no prices for the market date")
        return tuple(rows)


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderConfigError(f"{label} must be selected")
    return value.strip()
