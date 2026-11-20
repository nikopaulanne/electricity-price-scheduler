"""Price-only scenario runtime for threshold and cheapest-period profiles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_point_in_time, async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .core.models import PriceSlot, PriceSnapshot
from .core.price_schedule import PriceScheduleConfig, PriceScheduleResult, plan_price_schedule
from .providers import (
    ProviderError,
    ProviderUnavailableError,
    async_fetch_snapshot,
    provider_config,
    source_entities,
)

if TYPE_CHECKING:  # pragma: no cover
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_UTC = timezone.utc
_REFRESH_INTERVAL = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class PriceRuntimeSnapshot:
    """Immutable state shared by the price scenario entities."""

    available: bool
    status: str
    active: bool | None
    reason_code: str | None
    price_snapshot: PriceSnapshot | None
    result: PriceScheduleResult | None
    current_price: float | None
    last_success: datetime | None
    calculated_at: datetime | None
    error_code: str | None = None
    config: PriceScheduleConfig | None = None


class PriceCoordinator(DataUpdateCoordinator[PriceRuntimeSnapshot]):
    """Fetch prices and evaluate a bounded price scenario without HVAC work."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, semaphore: asyncio.Semaphore):
        super().__init__(
            hass,
            _LOGGER,
            name=f"Nord Pool price profile {entry.title}",
            update_interval=_REFRESH_INTERVAL,
            config_entry=entry,
            always_update=False,
        )
        self.hass = hass
        self.entry = entry
        self._semaphore = semaphore
        self._active = True
        self._update_lock = asyncio.Lock()
        self._generation = 0
        self._price_snapshot: PriceSnapshot | None = None
        self._last_success: datetime | None = None
        self._remove_transition = None
        self._remove_state_listener = None
        self._last_error: str | None = None
        self._latched_slots: tuple[PriceSlot, ...] = ()
        self._occurrence_end: datetime | None = None
        self._lock_loaded = False
        self._stored_lock_signature: tuple[object, ...] | None = None
        identity = [dict(entry.data), dict(entry.options), hass.config.time_zone]
        self._settings_fingerprint = hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode()
        ).hexdigest()
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}_price_schedule")

    async def async_start(self) -> None:
        """Watch source changes and schedule the next semantic boundary."""
        entity_ids = source_entities(self.entry.data)
        if entity_ids:
            self._remove_state_listener = async_track_state_change_event(
                self.hass, entity_ids, self._async_handle_state_change
            )
        self._schedule_next_transition()

    async def async_shutdown(self) -> None:
        if not self._active:
            return
        self._active = False
        self._generation += 1
        if self._remove_transition is not None:
            self._remove_transition()
            self._remove_transition = None
        if self._remove_state_listener is not None:
            self._remove_state_listener()
            self._remove_state_listener = None
        await super().async_shutdown()

    async def _async_update_data(self) -> PriceRuntimeSnapshot:
        from homeassistant.util import dt as dt_util

        async with self._update_lock:
            self._generation += 1
            generation = self._generation
            now = dt_util.utcnow().astimezone(_UTC)
            try:
                config = parse_price_schedule_config(str(self.entry.data.get("scenario", "threshold")), dict(self.entry.options))
                await self._async_load_lock(config)
                provider = provider_config(self.entry.data)
                if provider.provider_type == "amber" and config.mode != "threshold":
                    raise ValueError("Amber price profiles require threshold mode")
                snapshot = await async_fetch_snapshot(
                    self.hass, provider, today_local=dt_util.now().date()
                )
                if not self._active or generation != self._generation:
                    return self.data or _unavailable("source_unavailable", "source_unavailable", now)
                self._price_snapshot = snapshot
                self._last_success = now
                has_live_lock = (
                    config.mode != "threshold"
                    and self._occurrence_end is not None
                    and self._occurrence_end > now
                )
                if has_live_lock and not _supports_latched_coverage(
                    snapshot.slots, self._latched_slots, now, self._occurrence_end
                ):
                    runtime = _unavailable(
                        "invalid_data", "insufficient_price_coverage", now, self._last_success,
                        config=config,
                    )
                    self._schedule_next_transition(runtime)
                    return runtime
                if has_live_lock:
                    result = _locked_result(self._latched_slots, now)
                else:
                    self._latched_slots = ()
                    self._occurrence_end = None
                    result = await self._async_plan(snapshot, now, config)
                    if not self._active or generation != self._generation:
                        return self.data or _unavailable("source_unavailable", "source_unavailable", now)
                    self._update_latch(result, now, config.mode, config)
                await self._async_save_lock(config)
                current = _price_at(snapshot.slots, now)
                status, reason_code = _price_decision(config, result, current)
                runtime = PriceRuntimeSnapshot(
                    available=True,
                    status=status,
                    active=result.active,
                    reason_code=reason_code,
                    price_snapshot=snapshot,
                    result=result,
                    current_price=current,
                    last_success=now,
                    calculated_at=now,
                    config=config,
                )
                self._last_error = None
                self._schedule_next_transition(runtime)
                return runtime
            except ProviderError as err:
                self._price_snapshot = None
                code = err.code
                if code != self._last_error:
                    _LOGGER.warning("Price profile source update failed (%s)", code)
                    self._last_error = code
                runtime = _unavailable(
                    "source_unavailable" if isinstance(err, ProviderUnavailableError) else "invalid_data",
                    code,
                    now,
                    self._last_success,
                    config=locals().get("config"),
                )
                self._schedule_next_transition(runtime)
                return runtime
            except (ValueError, TypeError, OverflowError):
                self._price_snapshot = None
                runtime = _unavailable("invalid_data", "invalid_data", now, self._last_success, config=locals().get("config"))
                self._schedule_next_transition(runtime)
                return runtime
            except Exception:
                _LOGGER.exception("Price profile calculation failed")
                self._price_snapshot = None
                runtime = _unavailable("invalid_data", "calculation_failed", now, self._last_success, config=locals().get("config"))
                self._schedule_next_transition(runtime)
                return runtime

    async def _async_plan(
        self, snapshot: PriceSnapshot, now: datetime, config: PriceScheduleConfig
    ) -> PriceScheduleResult:
        async with self._semaphore:
            job = self.hass.async_add_executor_job(
                plan_price_schedule, snapshot.slots, now, config, self.hass.config.time_zone
            )
            try:
                return await asyncio.shield(job)
            except asyncio.CancelledError:
                await asyncio.gather(job, return_exceptions=True)
                raise

    def _update_latch(
        self, result: PriceScheduleResult, now: datetime, mode: str, config: PriceScheduleConfig
    ) -> None:
        """Lock one schedule occurrence through its window end, including split gaps."""
        if mode == "threshold":
            self._latched_slots = ()
            self._occurrence_end = None
            return
        if self._occurrence_end is not None and self._occurrence_end > now:
            return
        self._latched_slots = tuple(slot for slot in result.selected_slots if slot.end > now)
        self._occurrence_end = (
            result.occurrence_end
            or _occurrence_end(self._latched_slots, config, self.hass.config.time_zone)
        ) if self._latched_slots else None

    async def _async_load_lock(self, config: PriceScheduleConfig) -> None:
        if self._lock_loaded:
            return
        self._lock_loaded = True
        if config.mode == "threshold":
            return
        stored = await self._store.async_load()
        if not isinstance(stored, dict) or stored.get("config") != self._settings_fingerprint:
            if stored is not None:
                await self._store.async_remove()
            return
        try:
            slots = tuple(
                PriceSlot(
                    datetime.fromisoformat(item["start"]),
                    datetime.fromisoformat(item["end"]),
                    float(item["price"]),
                )
                for item in stored.get("slots", [])
            )
            self._latched_slots = slots
            raw_until = stored.get("occurrence_end")
            self._occurrence_end = datetime.fromisoformat(raw_until) if raw_until else None
            from homeassistant.util import dt as dt_util

            if self._occurrence_end is not None and self._occurrence_end <= dt_util.utcnow():
                self._latched_slots = ()
                self._occurrence_end = None
                await self._store.async_remove()
                self._stored_lock_signature = None
                return
            self._stored_lock_signature = (*tuple((s.start, s.end, s.price_per_kwh) for s in self._latched_slots), self._occurrence_end)
        except (KeyError, TypeError, ValueError, OverflowError):
            _LOGGER.warning("Ignoring invalid saved price schedule lock")

    async def _async_save_lock(self, config: PriceScheduleConfig) -> None:
        if config.mode == "threshold":
            return
        signature = (*tuple((s.start, s.end, s.price_per_kwh) for s in self._latched_slots), self._occurrence_end)
        if signature == self._stored_lock_signature:
            return
        if not self._latched_slots and self._occurrence_end is None:
            await self._store.async_remove()
            self._stored_lock_signature = None
            return
        await self._store.async_save({
            "config": self._settings_fingerprint,
            "occurrence_end": self._occurrence_end.isoformat() if self._occurrence_end else None,
            "slots": [
                {"start": slot.start.isoformat(), "end": slot.end.isoformat(), "price": slot.price_per_kwh}
                for slot in self._latched_slots
            ],
        })
        self._stored_lock_signature = signature

    async def _async_handle_state_change(self, event: object) -> None:
        if self._active:
            await self.async_request_refresh()

    async def _async_handle_transition(self, now: datetime) -> None:
        if self.entry.data.get("provider_type") == "amber" and self._active:
            await self.async_refresh()
            return
        async with self._update_lock:
            await self._async_update_at_boundary(now)

    async def _async_update_at_boundary(self, now: datetime) -> None:
        from homeassistant.util import dt as dt_util

        if not self._active or self._price_snapshot is None:
            return
        now_utc = now.astimezone(_UTC) if now.tzinfo else dt_util.utcnow().astimezone(_UTC)
        config = parse_price_schedule_config(str(self.entry.data.get("scenario", "threshold")), dict(self.entry.options))
        if config.mode != "threshold" and self._occurrence_end is not None and self._occurrence_end > now_utc:
            result = _locked_result(self._latched_slots, now_utc)
        else:
            self._latched_slots = ()
            self._occurrence_end = None
            result = await self._async_plan(self._price_snapshot, now_utc, config)
            if not self._active:
                return
            self._update_latch(result, now_utc, config.mode, config)
        await self._async_save_lock(config)
        current = _price_at(self._price_snapshot.slots, now_utc)
        status, reason_code = _price_decision(config, result, current)
        runtime = PriceRuntimeSnapshot(
            available=True,
            status=status,
            active=result.active,
            reason_code=reason_code,
            price_snapshot=self._price_snapshot,
            result=result,
            current_price=current,
            last_success=self._last_success,
            calculated_at=now_utc,
            config=config,
        )
        self.async_set_updated_data(runtime)
        self._schedule_next_transition(runtime)

    def _schedule_next_transition(self, snapshot: PriceRuntimeSnapshot | None = None) -> None:
        from homeassistant.util import dt as dt_util

        if self._remove_transition is not None:
            self._remove_transition()
            self._remove_transition = None
        runtime = snapshot or self.data
        if not self._active or runtime is None or not runtime.available or runtime.price_snapshot is None:
            return
        now = dt_util.utcnow().astimezone(_UTC)
        scenario = str(self.entry.data.get("scenario", "threshold"))
        mode = parse_price_schedule_config(scenario, dict(self.entry.options)).mode
        if mode == "threshold":
            boundaries = [slot.start for slot in runtime.price_snapshot.slots if slot.start > now]
            boundaries.extend(slot.end for slot in runtime.price_snapshot.slots if slot.end > now)
        elif self._occurrence_end is not None and self._occurrence_end > now:
            boundaries = [self._occurrence_end]
            for run in _group_runs(self._latched_slots):
                boundaries.extend(point for point in (run[0].start, run[-1].end) if point > now)
        elif runtime.result is not None and runtime.result.occurrence_end is not None:
            boundaries = [runtime.result.occurrence_end]
        else:
            boundaries = []
        if runtime.result is not None and runtime.result.next_transition is not None:
            if runtime.result.next_transition > now:
                boundaries.append(runtime.result.next_transition)
        if not boundaries:
            return
        next_transition = min(boundaries)

        @callback
        def _handle(now_local: datetime) -> None:
            self.hass.async_create_task(
                self._async_handle_transition(dt_util.utcnow().astimezone(_UTC))
            )

        self._remove_transition = async_track_point_in_time(self.hass, _handle, next_transition)


def parse_price_schedule_config(scenario: str, options: dict[str, object]) -> PriceScheduleConfig:
    """Build a validated core config from stable entry scenario/options data."""
    if scenario not in {"threshold", "schedule"}:
        raise ValueError("unsupported price scenario")
    mode = "threshold" if scenario == "threshold" else str(options.get("mode", "cheapest_contiguous"))
    if mode not in {"threshold", "cheapest_contiguous", "cheapest_split"}:
        raise ValueError("unsupported price scenario mode")
    weekdays_raw = options.get("weekdays")
    weekdays = None
    if weekdays_raw is not None:
        if not isinstance(weekdays_raw, (list, tuple, set)):
            raise ValueError("weekdays must be a list")
        weekdays = tuple(int(day) for day in weekdays_raw)
    deadline = options.get("deadline")
    if isinstance(deadline, str):
        deadline = datetime.fromisoformat(deadline)
    if deadline is not None and (not isinstance(deadline, datetime) or deadline.tzinfo is None):
        raise ValueError("deadline must be a timezone-aware datetime")
    return PriceScheduleConfig(
        mode=mode,
        threshold_price_per_kwh=_optional_float(options, "threshold_price_per_kwh"),
        required_duration_minutes=_optional_int(options, "required_duration_minutes", 0),
        window_start=_optional_time(options.get("window_start")),
        window_end=_optional_time(options.get("window_end")),
        weekdays=weekdays,
        min_run_minutes=_optional_int(options, "min_run_minutes", 15),
        price_ceiling_per_kwh=_optional_float(options, "price_ceiling_per_kwh"),
        fallback=options.get("fallback") if isinstance(options.get("fallback"), str) else None,
        deadline=deadline,
    )


def build_price_decision_attributes(
    config: PriceScheduleConfig | None,
    result: PriceScheduleResult | None,
    *,
    status: str,
    active: bool | None,
    reason_code: str | None = None,
    current_price: float | None = None,
    currency: str | None = None,
    error_code: str | None = None,
    now: datetime | None = None,
    last_success: datetime | None = None,
) -> dict[str, object]:
    """Return a serializable explanation of a price decision.

    This intentionally has no Home Assistant dependencies so onboarding
    previews and entity attributes can use the same decision vocabulary.
    ``result`` may be omitted for source/configuration faults.
    """
    selected_runs = _group_runs(result.selected_slots) if result is not None else ()
    next_run = next(
        (run for run in selected_runs if now is None or run[0].start > now), None
    )
    active_run = next(
        (
            run for run in selected_runs
            if now is not None and run[0].start <= now < run[-1].end
        ),
        None,
    )
    threshold = config.threshold_price_per_kwh if config is not None else None
    if current_price is None or threshold is None:
        comparison = None
    else:
        comparison = "at_or_below" if current_price <= threshold else "above"
    window_start = config.window_start if config is not None else None
    window_end = config.window_end if config is not None else None
    selected_periods = [
        {
            "start": _iso_utc(run[0].start),
            "end": _iso_utc(run[-1].end),
            "duration_minutes": round(
                (run[-1].end.astimezone(_UTC) - run[0].start.astimezone(_UTC)).total_seconds() / 60
            ),
            "slot_prices_per_kwh": [slot.price_per_kwh for slot in run],
        }
        for run in selected_runs
    ]
    return {
        "status": status,
        "active": active,
        "reason_code": reason_code or (result.reason_code if result is not None else None),
        "error_code": error_code,
        "current_price": current_price,
        "currency": currency,
        "threshold_price_per_kwh": threshold,
        "threshold_comparison": comparison,
        "mode": config.mode if config is not None else None,
        "window_start": _time_text(window_start),
        "window_end": _time_text(window_end),
        "weekdays": list(config.weekdays) if config is not None and config.weekdays is not None else None,
        "required_duration_minutes": config.required_duration_minutes if config is not None and config.mode != "threshold" else None,
        "min_run_minutes": config.min_run_minutes if config is not None and config.mode == "cheapest_split" else None,
        "price_ceiling_per_kwh": config.price_ceiling_per_kwh if config is not None else None,
        "fallback": config.fallback if config is not None else None,
        "deadline": _iso_utc(config.deadline) if config is not None and config.deadline is not None else None,
        "selected_run_count": len(selected_runs),
        "selected_periods": selected_periods,
        "active_period_end": _iso_utc(active_run[-1].end) if active_run else None,
        "next_period_start": _iso_utc(next_run[0].start) if next_run else None,
        "next_period_end": _iso_utc(next_run[-1].end) if next_run else None,
        "next_change": _iso_utc(result.next_transition) if result is not None else None,
        "occurrence_end": _iso_utc(result.occurrence_end) if result is not None else None,
        "warnings": list(result.warnings) if result is not None else [],
        "calculated_at": _iso_utc(now),
        "last_success": _iso_utc(last_success),
    }


def _iso_utc(value: datetime | None) -> str | None:
    return value.astimezone(_UTC).isoformat() if value is not None else None


def _time_text(value: time | str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, time):
        return value
    timespec = "microseconds" if value.microsecond else "seconds" if value.second else "minutes"
    return value.isoformat(timespec=timespec)


def _optional_time(value: object) -> time | str | None:
    if value is None or value == "":
        return None
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        return value
    raise ValueError("time option must be text")


def _optional_float(options: dict[str, object], key: str) -> float | None:
    value = options.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{key} must be numeric")
    parsed = float(value)
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise ValueError(f"{key} must be finite")
    return parsed


def _optional_int(options: dict[str, object], key: str, default: int) -> int:
    value = options.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be integer")
    parsed = int(value)
    if parsed != value:
        raise ValueError(f"{key} must be integer")
    return parsed



def _occurrence_end(
    selected: tuple[PriceSlot, ...], config: PriceScheduleConfig, timezone_name: str
) -> datetime | None:
    if not selected:
        return None
    if config.window_start is None or config.window_end is None:
        return selected[-1].end
    zone = ZoneInfo(timezone_name)
    start_time = _optional_time(config.window_start)
    end_time = _optional_time(config.window_end)
    if start_time is None or end_time is None:
        return selected[-1].end
    local_day = selected[0].start.astimezone(zone).date()
    if selected[0].start.astimezone(zone).timetz().replace(tzinfo=None) < start_time:
        local_day -= timedelta(days=1)
    end_day = local_day + timedelta(days=1 if end_time <= start_time else 0)
    return datetime.combine(end_day, end_time, tzinfo=zone).astimezone(_UTC)

def _group_runs(slots: tuple[PriceSlot, ...]) -> tuple[tuple[PriceSlot, ...], ...]:
    runs: list[list[PriceSlot]] = []
    for slot in slots:
        if runs and runs[-1][-1].end.astimezone(_UTC) == slot.start.astimezone(_UTC):
            runs[-1].append(slot)
        else:
            runs.append([slot])
    return tuple(tuple(run) for run in runs)

def _locked_result(slots: tuple[PriceSlot, ...], now: datetime) -> PriceScheduleResult:
    active = any(slot.start <= now < slot.end for slot in slots)
    transition_points = [
        point
        for run in _group_runs(slots)
        for point in (run[0].start, run[-1].end)
        if point > now
    ]
    return PriceScheduleResult(
        active=active,
        status="active" if active else "scheduled" if transition_points else "completed",
        reason_code="schedule_locked",
        selected_slots=slots,
        next_transition=min(transition_points, default=None),
    )


def _supports_latched_coverage(
    prices: tuple[PriceSlot, ...], selected: tuple[PriceSlot, ...], now: datetime,
    occurrence_end: datetime | None,
) -> bool:
    """Ensure the active interval or an unstarted selection is still covered."""
    current = tuple(slot for slot in selected if slot.start <= now < slot.end)
    required = current or tuple(slot for slot in selected if slot.end > now)
    if not required:
        return occurrence_end is not None and occurrence_end > now
    for selected_slot in required:
        cursor = max(now, selected_slot.start)
        for price_slot in prices:
            if price_slot.end <= cursor:
                continue
            if price_slot.start > cursor:
                break
            cursor = max(cursor, price_slot.end)
            if cursor >= selected_slot.end:
                break
        if cursor < selected_slot.end:
            return False
    return bool(required)

def _price_decision(
    config: PriceScheduleConfig, result: PriceScheduleResult, current_price: float | None,
) -> tuple[str, str]:
    """Derive a stable explanation for the current signal."""
    if config.mode == "threshold" and not result.active:
        if current_price is None and result.status != "no_prices":
            return "waiting_for_prices", "waiting_for_current_price"
        limit = config.threshold_price_per_kwh
        if current_price is not None and limit is not None:
            if current_price > limit:
                return "above_threshold", "above_threshold"
            if result.status == "scheduled":
                return result.status, "outside_allowed_window"
    return result.status, result.reason_code


def _price_at(slots: tuple[PriceSlot, ...], now: datetime) -> float | None:
    return next((slot.price_per_kwh for slot in slots if slot.start <= now < slot.end), None)


def _unavailable(
    status: str, error_code: str, calculated_at: datetime, last_success: datetime | None = None,
    *, config: PriceScheduleConfig | None = None,
) -> PriceRuntimeSnapshot:
    return PriceRuntimeSnapshot(
        available=False,
        status=status,
        active=None,
        reason_code=status,
        price_snapshot=None,
        result=None,
        current_price=None,
        last_success=last_success,
        calculated_at=calculated_at,
        error_code=error_code,
        config=config,
    )
