"""Small bounded parsing helpers shared by price providers."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, tzinfo
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..const import MAX_SLOTS
from ..core.models import PriceSnapshot
from ..core.normalization import PriceDataError, normalize_slots
from .base import ProviderConfigError, ProviderDataError, ProviderUnavailableError

MAX_RAW_INTERVALS = MAX_SLOTS * 2


def local_timezone(hass: object) -> tzinfo:
    """Resolve Home Assistant's configured timezone without importing HA."""
    config = getattr(hass, "config", None)
    value = getattr(config, "time_zone", None)
    if isinstance(value, tzinfo):
        return value
    if not isinstance(value, str) or not value:
        raise ProviderConfigError("Home Assistant timezone is unavailable")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise ProviderConfigError("Home Assistant timezone is invalid") from err


def bounded_rows(value: object, *, field: str) -> Sequence[object]:
    """Validate a provider list before copying, expanding, or normalizing it."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ProviderDataError(f"{field} must be a list of price intervals")
    if len(value) > MAX_RAW_INTERVALS:
        raise ProviderDataError(
            f"{field} has more than {MAX_RAW_INTERVALS} raw intervals"
        )
    return value


def row_field(row: object, field: str) -> object:
    if isinstance(row, Mapping):
        if field not in row:
            raise ProviderDataError(f"price interval is missing {field!r}")
        return row[field]
    try:
        return getattr(row, field)
    except AttributeError as err:
        raise ProviderDataError(f"price interval is missing {field!r}") from err


def parse_aware_datetime(value: object, *, field: str) -> datetime:
    """Parse an aware datetime or ISO 8601 timestamp, preserving its instant."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ProviderDataError(f"{field} timestamp is empty")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as err:
            raise ProviderDataError(f"{field} timestamp is invalid") from err
    else:
        raise ProviderDataError(f"{field} must be an aware datetime or ISO timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderDataError(f"{field} timestamp must include a UTC offset")
    return parsed


def rows_for_local_dates(
    rows: Sequence[object], *, dates: set[date], timezone: tzinfo
) -> Iterator[object]:
    """Yield only intervals whose starts fall on requested HA-local dates."""
    for row in rows:
        start = parse_aware_datetime(row_field(row, "start"), field="start")
        if start.astimezone(timezone).date() in dates:
            yield row


def total_rows_within_limit(*rows: Sequence[object]) -> None:
    if sum(len(rows_) for rows_ in rows) > MAX_RAW_INTERVALS:
        raise ProviderDataError(
            f"combined raw interval count exceeds {MAX_RAW_INTERVALS}"
        )


def live_entity_attributes(hass: object, entity_id: str) -> Mapping[str, object]:
    """Read attributes only from a live Home Assistant state."""
    state = hass.states.get(entity_id)
    if state is None or str(state.state).lower() in {"unknown", "unavailable", "none", ""}:
        raise ProviderUnavailableError("Price entity is unavailable")
    if not isinstance(state.attributes, Mapping):
        raise ProviderDataError("Price entity has invalid attributes")
    return state.attributes


async def price_action(hass, domain: str, action: str, data: dict[str, object]) -> Mapping:
    """Call a Home Assistant price action with a timeout and a safe error."""
    try:
        async with asyncio.timeout(30):
            response = await hass.services.async_call(
                domain, action, data, blocking=True, return_response=True,
            )
    except Exception as err:
        raise ProviderUnavailableError(f"{domain} price action is unavailable") from err
    if not isinstance(response, Mapping):
        raise ProviderDataError(f"{domain} price action returned an invalid response")
    return response


def dated_snapshot(rows, *, source_unit: str, currency: str, provider: str,
                   today_local: date, zone: tzinfo) -> PriceSnapshot:
    """Normalize two local days while preserving missing intervals."""
    tomorrow = today_local + timedelta(days=1)
    try:
        slots = normalize_slots(
            rows_for_local_dates(rows, dates={today_local, tomorrow}, timezone=zone),
            source_unit=source_unit, currency=currency,
        )
    except PriceDataError as err:
        raise ProviderDataError(str(err)) from err
    if not slots or not any(slot.start.astimezone(zone).date() == today_local for slot in slots):
        raise ProviderUnavailableError("No current-day prices")
    return PriceSnapshot(
        slots, currency, provider,
        coverage_start=slots[0].start,
        coverage_end=slots[-1].end,
        tomorrow_available=slots[-1].start.astimezone(zone).date() == tomorrow,
    )
