"""Discover price feeds already configured in Home Assistant."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .providers.epex_spot import PRICE_CURRENCIES

_OCTOPUS_CURRENT_RATES = re.compile(
    r"event\.octopus_energy_electricity_[a-z0-9_]+_current_day_rates"
)


def source_key(data: Mapping[str, object]) -> str:
    """Identify a configured source within the setup picker."""
    provider = data["provider_type"]
    entity_id = data.get("price_entity_id")
    if entity_id:
        return f"{provider}:{entity_id}"
    entry_id = data.get("source_config_entry_id", "")
    area = data.get("area", "")
    return f"{provider}:{entry_id}:{area}"


def currency_from_unit(unit: object) -> str | None:
    if not isinstance(unit, str):
        return None
    match = re.fullmatch(r"([A-Z]{3})\s*/\s*(?:kWh|MWh|Wh)", unit.strip(), re.IGNORECASE)
    return match[1].upper() if match else None


def discover_price_sources(
    hass: HomeAssistant,
) -> dict[str, tuple[str, dict[str, Any]]]:
    """List compatible loaded sources with user-facing names and opaque keys."""
    choices: dict[str, tuple[str, dict[str, Any]]] = {}

    def add(label: str, provider: str, **data: Any) -> None:
        data["provider_type"] = provider
        choices[source_key(data)] = (label, data)

    tibber_currencies: set[str] = set()
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.disabled:
            continue
        entity_id = entry.entity_id
        state = hass.states.get(entity_id)
        if state is None:
            continue
        attrs = state.attributes
        name = attrs.get("friendly_name") or entry.name or entry.original_name or entity_id
        if entry.platform == "nordpool" and entity_id.startswith("sensor.") and "raw_today" in attrs:
            add(f"Nordpool · {name}", "community_nordpool", price_entity_id=entity_id)
        elif (
            entry.platform == "entsoe"
            and entity_id.startswith("sensor.")
            and "prices_today" in attrs
        ):
            currency = currency_from_unit(attrs.get("unit_of_measurement"))
            config_entry = (
                hass.config_entries.async_get_entry(entry.config_entry_id)
                if entry.config_entry_id else None
            )
            period = (config_entry.options.get("period") or config_entry.data.get("period")) if config_entry else None
            interval = {"PT15M": 15, "PT60M": 60, None: 60}.get(period)
            if currency and interval:
                add(
                    f"ENTSO-E · {name}", "entsoe", price_entity_id=entity_id,
                    currency=currency, interval_minutes=interval,
                )
        elif (
            entry.platform == "epex_spot"
            and entity_id.startswith("sensor.")
            and entry.unique_id.endswith((" MarketPrice", " Total Price"))
            and (currency := PRICE_CURRENCIES.get(attrs.get("unit_of_measurement"))) is not None
        ):
            source_entry = (
                hass.config_entries.async_get_entry(entry.config_entry_id)
                if entry.config_entry_id else None
            )
            source_name = source_entry.title if source_entry else "EPEX Spot"
            add(
                f"{source_name} · {name}", "epex_spot",
                price_entity_id=entity_id, currency=currency,
            )
        elif entry.platform == "octopus_energy" and _OCTOPUS_CURRENT_RATES.fullmatch(entity_id) and "_export_" not in entity_id:
            add(
                f"Octopus Energy · {name}", "octopus_energy",
                price_entity_id=entity_id, currency="GBP",
            )
        elif entry.platform == "amberelectric" and hass.services.has_service("amberelectric", "get_forecasts"):
            for channel in ("general", "controlled_load"):
                if entry.unique_id.endswith(f"-current-{channel}") and entry.config_entry_id:
                    add(
                        f"Amber Electric · {name}", "amber",
                        source_config_entry_id=entry.config_entry_id, price_entity_id=entity_id,
                        channel_type=channel, currency="AUD",
                    )
                    break
        elif entry.platform == "tibber":
            currency = currency_from_unit(attrs.get("unit_of_measurement"))
            if currency:
                tibber_currencies.add(currency)

    if hass.services.has_service("nordpool", "get_prices_for_date"):
        for entry in hass.config_entries.async_entries("nordpool"):
            if entry.state != ConfigEntryState.LOADED:
                continue
            currency, areas = entry.data.get("currency"), entry.data.get("areas")
            if not isinstance(currency, str) or not isinstance(areas, (list, tuple)):
                continue
            for area in areas:
                if isinstance(area, str):
                    add(
                        f"{entry.title} · {area} ({currency})", "core_nordpool",
                        source_config_entry_id=entry.entry_id, area=area, currency=currency,
                    )

    if hass.services.has_service("tibber", "get_prices"):
        entries = hass.config_entries.async_entries("tibber")
        if len(entries) == 1 and entries[0].state == ConfigEntryState.LOADED:
            fields: dict[str, str] = {}
            if len(tibber_currencies) == 1:
                fields["currency"] = next(iter(tibber_currencies))
            add("Tibber", "tibber", **fields)

    return choices
