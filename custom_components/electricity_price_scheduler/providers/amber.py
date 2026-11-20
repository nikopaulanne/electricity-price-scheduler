"""Read Amber's current consumption rate and forecast from Home Assistant."""

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone

from ..const import MAX_SLOTS
from ..core.models import PriceSnapshot
from ..core.normalization import PriceDataError, normalize_slots
from ._helpers import (
    bounded_rows,
    live_entity_attributes,
    local_timezone,
    parse_aware_datetime,
    price_action,
    row_field,
)
from .base import AmberConfig, ProviderConfigError, ProviderDataError, ProviderHass, ProviderUnavailableError

_UTC = timezone.utc


def _interval(row) -> dict:
    start = parse_aware_datetime(row_field(row, "start_time"), field="Amber start").astimezone(_UTC)
    end = parse_aware_datetime(row_field(row, "end_time"), field="Amber end").astimezone(_UTC)
    duration = end - start
    if isinstance(row, Mapping) and (minutes := row.get("duration")) in (5, 30):
        nominal = timedelta(minutes=minutes)
        # Amber reports the first whole second inside some settlement intervals.
        if duration == nominal - timedelta(seconds=1) and not (end.second or end.microsecond):
            start = end - nominal
            duration = nominal
        if duration != nominal:
            raise ProviderDataError("Amber interval duration conflicts with its timestamps")
    if duration not in (timedelta(minutes=5), timedelta(minutes=30)):
        raise ProviderDataError("Unsupported Amber interval duration")
    return {"start": start, "end": end, "price": row_field(row, "per_kwh")}


class AmberProvider:
    async def async_fetch_snapshot(
        self, hass: ProviderHass, config: AmberConfig, *, today_local: date,
    ) -> PriceSnapshot:
        if (
            config.currency != "AUD"
            or config.channel_type not in {"general", "controlled_load"}
            or not config.price_entity_id
        ):
            raise ProviderConfigError("Select an Amber consumption price sensor in AUD")
        attrs = live_entity_attributes(hass, config.price_entity_id)
        current_channel = attrs.get("channel_type")
        if current_channel == "controlledLoad":
            current_channel = "controlled_load"
        if current_channel != config.channel_type:
            raise ProviderConfigError("Amber current sensor does not match the selected channel")
        now = datetime.now(_UTC)
        current = _interval(attrs)
        if not current["start"] <= now < current["end"]:
            raise ProviderUnavailableError("Amber current price interval is unavailable")

        warnings = []
        try:
            response = await price_action(hass, "amberelectric", "get_forecasts", {
                "config_entry_id": config.config_entry_id, "channel_type": config.channel_type,
            })
        except ProviderUnavailableError:
            response = {}
            warnings.append("amber_forecast_unavailable")

        tomorrow = today_local + timedelta(days=1)
        limit = datetime.combine(tomorrow + timedelta(days=1), datetime.min.time(), tzinfo=local_timezone(hass))
        forecast = []
        for raw in bounded_rows(response.get("forecasts", ()), field="Amber forecast"):
            row = _interval(raw)
            if current["end"] <= row["start"] < limit:
                forecast.append(row)
        forecast.sort(key=lambda row: row["start"])

        zone = local_timezone(hass)
        intervals = []
        slot_budget = MAX_SLOTS
        for row in (current, *forecast):
            cost = 2 if row["end"] - row["start"] == timedelta(minutes=30) else 1
            if cost > slot_budget:
                warnings.append("amber_forecast_truncated")
                break
            intervals.append(row)
            slot_budget -= cost
        try:
            slots = normalize_slots(intervals, source_unit="currency/kWh", currency="AUD")
        except PriceDataError as err:
            raise ProviderDataError(str(err)) from err
        return PriceSnapshot(
            slots, "AUD", config.provider_type,
            coverage_start=slots[0].start, coverage_end=slots[-1].end,
            tomorrow_available=slots[-1].start.astimezone(zone).date() == tomorrow,
            warnings=tuple(warnings),
        )
