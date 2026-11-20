"""Price adapter for the JaccoR/hass-entso-e average price sensor."""

from dataclasses import replace
from datetime import date, timedelta, timezone

from ..core.models import PriceSnapshot
from ._helpers import (
    bounded_rows,
    dated_snapshot,
    live_entity_attributes,
    local_timezone,
    parse_aware_datetime,
    row_field,
    total_rows_within_limit,
)
from .base import EntsoeConfig, ProviderConfigError, ProviderDataError, ProviderHass


class EntsoeProvider:
    async def async_fetch_snapshot(
        self, hass: ProviderHass, config: EntsoeConfig, *, today_local: date,
    ) -> PriceSnapshot:
        attrs = live_entity_attributes(hass, config.entity_id)
        source_unit = attrs.get("unit_of_measurement")
        if source_unit not in {f"{config.currency}/kWh", f"{config.currency}/MWh"}:
            raise ProviderConfigError("ENTSO-E source currency or unit changed")
        if config.interval_minutes not in {15, 60}:
            raise ProviderDataError("Unsupported ENTSO-E interval")
        today = bounded_rows(attrs.get("prices_today"), field="ENTSO-E today")
        tomorrow = bounded_rows(attrs.get("prices_tomorrow", ()), field="ENTSO-E tomorrow")
        total_rows_within_limit(today, tomorrow)
        rows = []
        duration = timedelta(minutes=config.interval_minutes)
        for item in (*today, *tomorrow):
            start = parse_aware_datetime(row_field(item, "time"), field="time").astimezone(timezone.utc)
            rows.append({"start": start, "end": start + duration, "price": row_field(item, "price")})
        snapshot = dated_snapshot(
            rows, source_unit="currency/" + source_unit.split("/")[1],
            currency=config.currency, provider=config.provider_type,
            today_local=today_local, zone=local_timezone(hass),
        )
        warnings = ["entsoe_prices_may_include_vat_or_modifier"]
        if not snapshot.tomorrow_available:
            warnings.append("entsoe_tomorrow_prices_unavailable")
        return replace(snapshot, warnings=tuple(warnings))
