"""Read VAT-inclusive electricity rates from the Octopus Energy HA integration."""

from dataclasses import replace
from datetime import date
import re

from ..core.models import PriceSnapshot
from ._helpers import (
    bounded_rows,
    dated_snapshot,
    live_entity_attributes,
    local_timezone,
    row_field,
    total_rows_within_limit,
)
from .base import OctopusEnergyConfig, ProviderConfigError, ProviderHass, ProviderUnavailableError

_CURRENT_DAY = re.compile(r"event\.octopus_energy_electricity_[a-z0-9_]+_current_day_rates")


def next_day_entity_id(price_entity_id: str) -> str:
    if not _CURRENT_DAY.fullmatch(price_entity_id) or "_export_" in price_entity_id:
        raise ProviderConfigError("Select a standard Octopus Energy electricity rates event")
    return price_entity_id.removesuffix("_current_day_rates") + "_next_day_rates"


class OctopusEnergyProvider:
    async def async_fetch_snapshot(
        self, hass: ProviderHass, config: OctopusEnergyConfig, *, today_local: date,
    ) -> PriceSnapshot:
        if config.currency != "GBP":
            raise ProviderConfigError("Octopus electricity rates must be in GBP")
        next_entity = next_day_entity_id(config.price_entity_id)
        today_attrs = live_entity_attributes(hass, config.price_entity_id)
        today = bounded_rows(today_attrs.get("rates", ()), field="Octopus current day rates")
        try:
            next_attrs = live_entity_attributes(hass, next_entity)
        except ProviderUnavailableError:
            next_attrs = {}
        tomorrow = bounded_rows(next_attrs.get("rates", ()), field="Octopus next day rates")
        total_rows_within_limit(today, tomorrow)
        rows = [
            {
                "start": row_field(item, "start"),
                "end": row_field(item, "end"),
                "price": row_field(item, "value_inc_vat"),
            }
            for item in (*today, *tomorrow)
        ]
        snapshot = dated_snapshot(
            rows, source_unit="GBP/kWh", currency=config.currency, provider=config.provider_type,
            today_local=today_local, zone=local_timezone(hass),
        )
        if not snapshot.tomorrow_available:
            return replace(snapshot, warnings=("octopus_energy_tomorrow_prices_unavailable",))
        return snapshot
