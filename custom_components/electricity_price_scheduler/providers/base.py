"""Typed provider contracts and source-neutral dispatch.

The providers only acquire and normalize prices. They never run the optimizer
or control devices. Home Assistant types are intentionally not imported at
runtime so provider contracts can be tested with a small fake hass object.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Protocol, TypeAlias

from ..core.models import PriceSnapshot

if TYPE_CHECKING:  # pragma: no cover - only for static type checkers
    from homeassistant.core import HomeAssistant


class ProviderError(Exception):
    """Base class for safe, user-presentable provider failures."""

    code = "provider_error"


class ProviderConfigError(ProviderError):
    """Provider configuration is missing or conflicts with source metadata."""

    code = "invalid_provider_config"


class ProviderUnavailableError(ProviderError):
    """The selected source is missing, stale, or temporarily unreachable."""

    code = "source_unavailable"


class ProviderDataError(ProviderError):
    """The source responded, but its price data is malformed or unsafe."""

    code = "invalid_price_data"


@dataclass(frozen=True, slots=True)
class CoreNordpoolConfig:
    """Selection for the built-in Home Assistant Nord Pool integration."""

    source_config_entry_id: str
    area: str
    currency: str

    @property
    def provider_type(self) -> str:
        return "core_nordpool"


@dataclass(frozen=True, slots=True)
class CommunityNordpoolConfig:
    """Selection for the community ``custom-components/nordpool`` sensor."""

    price_entity_id: str
    currency: str
    # An explicit full source unit (for example ``NOK/kWh`` or ``øre/kWh``).
    # Use only when the entity's unit metadata is absent or ambiguous.
    unit_override: str | None = None

    @property
    def provider_type(self) -> str:
        return "community_nordpool"


@dataclass(frozen=True, slots=True)
class EntsoeConfig:
    entity_id: str
    currency: str
    interval_minutes: int = 60
    provider_type = "entsoe"


@dataclass(frozen=True, slots=True)
class EpexSpotConfig:
    price_entity_id: str
    currency: str = "EUR"
    provider_type = "epex_spot"


@dataclass(frozen=True, slots=True)
class OctopusEnergyConfig:
    price_entity_id: str
    currency: str = "GBP"
    provider_type = "octopus_energy"


@dataclass(frozen=True, slots=True)
class TibberConfig:
    currency: str
    home_name: str | None = None
    provider_type = "tibber"


@dataclass(frozen=True, slots=True)
class AmberConfig:
    config_entry_id: str
    currency: str
    channel_type: str = "general"
    price_entity_id: str | None = None
    provider_type = "amber"


ProviderConfig: TypeAlias = (
    CoreNordpoolConfig | CommunityNordpoolConfig | EntsoeConfig | EpexSpotConfig
    | OctopusEnergyConfig | TibberConfig | AmberConfig
)


def provider_config(data: Mapping[str, object]) -> ProviderConfig:
    """Decode the persisted source settings used by flows and coordinators."""
    def required(key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ProviderConfigError(f"Missing price source field: {key}")
        return value.strip()

    provider_type = data.get("provider_type")
    currency = required("currency")
    if provider_type == "community_nordpool":
        unit = data.get("unit_override")
        return CommunityNordpoolConfig(
            required("price_entity_id"), currency, unit if isinstance(unit, str) else None,
        )
    if provider_type == "core_nordpool":
        return CoreNordpoolConfig(
            required("source_config_entry_id"), required("area"), currency,
        )
    if provider_type == "entsoe":
        interval = data.get("interval_minutes")
        if interval not in (15, 60):
            raise ProviderConfigError("Unsupported ENTSO-E interval")
        return EntsoeConfig(required("price_entity_id"), currency, interval)
    if provider_type == "epex_spot":
        return EpexSpotConfig(required("price_entity_id"), currency)
    if provider_type == "octopus_energy":
        return OctopusEnergyConfig(required("price_entity_id"), currency)
    if provider_type == "tibber":
        home = data.get("home_name")
        return TibberConfig(currency, home.strip() if isinstance(home, str) else None)
    if provider_type == "amber":
        return AmberConfig(
            required("source_config_entry_id"), currency, required("channel_type"),
            required("price_entity_id"),
        )
    raise ProviderConfigError("Unsupported price source")


def source_entities(data: Mapping[str, object]) -> tuple[str, ...]:
    """State changes that can affect the configured source."""
    entity_id = data.get("price_entity_id")
    if not isinstance(entity_id, str):
        return ()
    if data.get("provider_type") == "octopus_energy":
        from .octopus import next_day_entity_id

        try:
            return entity_id, next_day_entity_id(entity_id)
        except ProviderConfigError:
            return (entity_id,)
    return (entity_id,)


class ServiceRegistry(Protocol):
    async def async_call(self, domain: str, service: str, service_data: dict[str, object], **kwargs: object) -> object: ...


class StateMachine(Protocol):
    def get(self, entity_id: str) -> object | None: ...


class ProviderHass(Protocol):
    services: ServiceRegistry
    states: StateMachine
    config: object


class PriceProvider(Protocol):
    async def async_fetch_snapshot(
        self,
        hass: ProviderHass | HomeAssistant,
        config: ProviderConfig,
        *,
        today_local: date,
    ) -> PriceSnapshot: ...


def get_provider(provider_type: str) -> PriceProvider:
    """Return a provider implementation for a persisted provider type."""
    if provider_type == "community_nordpool":
        from .community_nordpool import CommunityNordpoolProvider

        return CommunityNordpoolProvider()
    if provider_type == "core_nordpool":
        from .core_nordpool import CoreNordpoolProvider

        return CoreNordpoolProvider()
    if provider_type == "entsoe":
        from .entsoe import EntsoeProvider

        return EntsoeProvider()
    if provider_type == "epex_spot":
        from .epex_spot import EpexSpotProvider

        return EpexSpotProvider()
    if provider_type == "octopus_energy":
        from .octopus import OctopusEnergyProvider

        return OctopusEnergyProvider()
    if provider_type == "tibber":
        from .tibber import TibberProvider

        return TibberProvider()
    if provider_type == "amber":
        from .amber import AmberProvider

        return AmberProvider()
    raise ProviderConfigError(f"Unsupported price provider: {provider_type!r}")


async def async_fetch_snapshot(
    hass: ProviderHass | HomeAssistant,
    config: ProviderConfig,
    *,
    today_local: date,
) -> PriceSnapshot:
    """Fetch and normalize one bounded today/tomorrow price snapshot."""
    return await get_provider(config.provider_type).async_fetch_snapshot(
        hass, config, today_local=today_local
    )
