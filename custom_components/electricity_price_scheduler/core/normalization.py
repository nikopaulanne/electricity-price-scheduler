"""Provider-neutral interval and unit normalization."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from ..const import (
    MAX_SLOTS,
    SLOT_MINUTES,
    UNIT_CENT_PER_KWH,
    UNIT_CURRENCY_PER_KWH,
    UNIT_CURRENCY_PER_MWH,
    UNIT_SNT_PER_KWH,
)
from .models import PriceSlot


class PriceDataError(ValueError):
    """Input price data cannot be interpreted safely."""


_UTC = timezone.utc


def _parse_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise PriceDataError(f"{field} is empty")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as err:
            raise PriceDataError(f"{field} is not a valid ISO 8601 datetime") from err
    else:
        raise PriceDataError(f"{field} must be a datetime or ISO 8601 string")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PriceDataError(f"{field} must include a UTC offset")
    return parsed.astimezone(_UTC)


def _get_field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        if name not in item:
            raise PriceDataError(f"slot is missing {name!r}")
        return item[name]
    try:
        return getattr(item, name)
    except AttributeError as err:
        raise PriceDataError(f"slot is missing {name!r}") from err


def _price_field(item: Any) -> Any:
    for name in ("price", "value", "price_per_kwh"):
        if isinstance(item, Mapping) and name in item:
            return item[name]
        if not isinstance(item, Mapping) and hasattr(item, name):
            return getattr(item, name)
    raise PriceDataError("slot is missing 'price' or 'value'")


def _unit_factor(source_unit: str, currency: str) -> float:
    if not isinstance(source_unit, str) or not source_unit.strip():
        raise PriceDataError("source_unit must be provided")
    unit = source_unit.strip().lower().replace(" ", "").replace("€", "eur")
    curr = currency.strip().upper()
    aliases = {
        UNIT_CURRENCY_PER_KWH: "currency/kwh",
        UNIT_CURRENCY_PER_MWH: "currency/mwh",
        UNIT_CENT_PER_KWH: "cent/kwh",
        "c/kwh": "cent/kwh",
        "ct/kwh": "cent/kwh",
        "cent/kwh": "cent/kwh",
        "cents/kwh": "cent/kwh",
        "cent/wh": "cent/wh",
        "c/wh": "cent/wh",
        "ct/wh": "cent/wh",
        "cent/mwh": "cent/mwh",
        "c/mwh": "cent/mwh",
        "ct/mwh": "cent/mwh",
        UNIT_SNT_PER_KWH: "snt/kwh",
        "sent/kwh": "snt/kwh",
        "snt/kwh": "snt/kwh",
        "snt/wh": "snt/wh",
        "snt/mwh": "snt/mwh",
        "øre/kwh": "ore/kwh",
        "öre/kwh": "ore/kwh",
        "ore/kwh": "ore/kwh",
        "øre/wh": "ore/wh",
        "öre/wh": "ore/wh",
        "ore/wh": "ore/wh",
        "øre/mwh": "ore/mwh",
        "öre/mwh": "ore/mwh",
        "ore/mwh": "ore/mwh",
    }
    unit = aliases.get(unit, unit)

    # Generic names are useful for integrations which expose the selected
    # currency separately. Explicit currency units must match that currency.
    if unit == "currency/kwh":
        return 1.0
    if unit == "currency/mwh":
        return 0.001
    if unit == "currency/wh":
        return 1000.0
    if unit == "cent/kwh":
        if curr != "EUR":
            raise PriceDataError("cent/kWh is only supported with EUR currency")
        return 0.01
    if unit == "cent/wh":
        if curr != "EUR":
            raise PriceDataError("cent/Wh is only supported with EUR currency")
        return 10.0
    if unit == "cent/mwh":
        if curr != "EUR":
            raise PriceDataError("cent/MWh is only supported with EUR currency")
        return 0.00001
    if unit == "snt/kwh":
        if curr != "EUR":
            raise PriceDataError("snt/kWh is only supported with EUR currency")
        return 0.01
    if unit == "snt/wh":
        if curr != "EUR":
            raise PriceDataError("snt/Wh is only supported with EUR currency")
        return 10.0
    if unit == "snt/mwh":
        if curr != "EUR":
            raise PriceDataError("snt/MWh is only supported with EUR currency")
        return 0.00001
    if unit == "ore/kwh":
        if curr != "NOK":
            raise PriceDataError("øre/kWh is only supported with NOK currency")
        return 0.01
    if unit == "ore/wh":
        if curr != "NOK":
            raise PriceDataError("øre/Wh is only supported with NOK currency")
        return 10.0
    if unit == "ore/mwh":
        if curr != "NOK":
            raise PriceDataError("øre/MWh is only supported with NOK currency")
        return 0.00001

    for code in ("EUR", "NOK", "SEK", "DKK", "GBP", "PLN"):
        if curr != code and unit.startswith(code.lower() + "/"):
            raise PriceDataError(f"source unit {source_unit!r} conflicts with currency {curr}")
        if curr == code:
            if unit == f"{code.lower()}/kwh":
                return 1.0
            if unit == f"{code.lower()}/mwh":
                return 0.001
            if unit == f"{code.lower()}/wh":
                return 1000.0
    raise PriceDataError(f"unsupported source unit: {source_unit!r}")


def normalize_slots(
    raw_intervals: Iterable[Any], *, source_unit: str, currency: str
) -> tuple[PriceSlot, ...]:
    """Convert raw provider intervals to ordered UTC slots priced per kWh.

    Exact multiples of 15 minutes are split into quarter-hour intervals. Other
    positive interval lengths are preserved without rounding. Gaps are kept as
    gaps and are never filled with invented prices; the optimizer will not
    create a candidate spanning a gap.

    Raises :class:`PriceDataError` for malformed, overlapping, naive-time,
    non-finite, or oversized data. Identical duplicate intervals are removed.
    """
    if not isinstance(currency, str) or not currency.strip():
        raise PriceDataError("currency must be provided")
    normalized_currency = currency.strip().upper()
    factor = _unit_factor(source_unit, normalized_currency)
    slots: list[PriceSlot] = []

    try:
        iterator = iter(raw_intervals)
    except TypeError as err:
        raise PriceDataError("raw_intervals must be iterable") from err

    raw_count = 0
    for item in iterator:
        raw_count += 1
        if raw_count > MAX_SLOTS * 2:
            raise PriceDataError(f"raw interval count exceeds safety limit of {MAX_SLOTS * 2}")
        start = _parse_datetime(_get_field(item, "start"), "start")
        end = _parse_datetime(_get_field(item, "end"), "end")
        if end <= start:
            raise PriceDataError("slot end must be later than start")
        try:
            raw_price = float(_price_field(item))
        except (TypeError, ValueError, OverflowError) as err:
            raise PriceDataError("slot price must be numeric") from err
        if not math.isfinite(raw_price):
            raise PriceDataError("slot price must be finite")
        price = raw_price * factor
        if not math.isfinite(price):
            raise PriceDataError("normalized slot price is not finite")

        duration_seconds = (end - start).total_seconds()
        quarter_seconds = SLOT_MINUTES * 60
        pieces = int(duration_seconds // quarter_seconds) if duration_seconds % quarter_seconds == 0 else 1
        if len(slots) + pieces > MAX_SLOTS:
            raise PriceDataError(f"normalized slot count exceeds maximum of {MAX_SLOTS}")
        if pieces > 1:
            for index in range(pieces):
                piece_start = start + timedelta(seconds=quarter_seconds * index)
                slots.append(PriceSlot(piece_start, piece_start + timedelta(seconds=quarter_seconds), price))
        else:
            slots.append(PriceSlot(start, end, price))

    slots.sort(key=lambda slot: (slot.start, slot.end, slot.price_per_kwh))
    deduplicated: list[PriceSlot] = []
    for slot in slots:
        if deduplicated and slot.start == deduplicated[-1].start and slot.end == deduplicated[-1].end:
            if slot.price_per_kwh != deduplicated[-1].price_per_kwh:
                raise PriceDataError("conflicting duplicate price intervals")
            continue
        if deduplicated and slot.start < deduplicated[-1].end:
            raise PriceDataError("overlapping price intervals")
        deduplicated.append(slot)
    return tuple(deduplicated)
