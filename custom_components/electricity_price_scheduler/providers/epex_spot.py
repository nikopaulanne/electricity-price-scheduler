"""Read a price series from the EPEX Spot integration, including aWATTar."""

from datetime import date

from ..core.models import PriceSnapshot
from ._helpers import bounded_rows, dated_snapshot, live_entity_attributes, local_timezone, row_field
from .base import EpexSpotConfig, ProviderConfigError, ProviderHass

PRICE_CURRENCIES = {
    "€/kWh": "EUR", "EUR/kWh": "EUR",
    "£/kWh": "GBP", "GBP/kWh": "GBP",
}


class EpexSpotProvider:
    async def async_fetch_snapshot(
        self, hass: ProviderHass, config: EpexSpotConfig, *, today_local: date,
    ) -> PriceSnapshot:
        attributes = live_entity_attributes(hass, config.price_entity_id)
        unit = attributes.get("unit_of_measurement")
        if not isinstance(unit, str) or PRICE_CURRENCIES.get(unit) != config.currency:
            raise ProviderConfigError("EPEX Spot price sensor currency or unit changed")
        rows = [
            {
                "start": row_field(item, "start_time"),
                "end": row_field(item, "end_time"),
                "price": row_field(item, "price_per_kwh"),
            }
            for item in bounded_rows(attributes.get("data", ()), field="EPEX Spot prices")
        ]
        return dated_snapshot(
            rows, source_unit="currency/kWh", currency=config.currency,
            provider=config.provider_type, today_local=today_local, zone=local_timezone(hass),
        )
