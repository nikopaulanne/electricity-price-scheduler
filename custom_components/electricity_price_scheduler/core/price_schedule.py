"""Bounded, provider-neutral price threshold and load scheduling helpers.

This module deliberately has no Home Assistant dependencies. Inputs are UTC
price intervals; optional daily windows are interpreted in an IANA timezone.
The planner is stateless, so callers that need a schedule to remain fixed
while prices update should latch a returned occurrence until it ends.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Literal, Optional, Sequence

from .models import PriceSlot

_UTC = timezone.utc
_MAX_PRICE_SLOTS = 200
_MAX_DURATION_MINUTES = 24 * 60
_MAX_HORIZON = timedelta(days=14)


class PriceScheduleError(ValueError):
    """Price-schedule settings or input intervals are invalid."""


@dataclass(frozen=True, slots=True)
class PriceScheduleConfig:
    """Settings for a non-HVAC price scenario.

    `mode` is `threshold`, `cheapest_contiguous`, or `cheapest_split`. A daily
    window can cross midnight. Weekdays use Python's Monday=0 numbering and
    apply to the local date on which the window starts.
    """

    mode: Literal["threshold", "cheapest_contiguous", "cheapest_split"]
    threshold_price_per_kwh: Optional[float] = None
    required_duration_minutes: int = 0
    window_start: Optional[time | str] = None
    window_end: Optional[time | str] = None
    weekdays: Optional[tuple[int, ...]] = None
    min_run_minutes: int = 15
    price_ceiling_per_kwh: Optional[float] = None
    fallback: Optional[Literal["deadline", "price"]] = None
    deadline: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class PriceScheduleResult:
    """Chosen intervals and compact status metadata for a price scenario."""

    active: bool
    status: str
    reason_code: str
    selected_slots: tuple[PriceSlot, ...] = ()
    warnings: tuple[str, ...] = ()
    next_transition: Optional[datetime] = None
    occurrence_end: Optional[datetime] = None


def _time_value(value: Optional[time | str], name: str) -> Optional[time]:
    if value is None:
        return None
    if isinstance(value, time):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = time.fromisoformat(value)
        except ValueError as err:
            raise PriceScheduleError(f"{name} must be an ISO local time such as 06:30") from err
    else:
        raise PriceScheduleError(f"{name} must be a local time or ISO local time string")
    if parsed.tzinfo is not None:
        raise PriceScheduleError(f"{name} must not include a timezone")
    return parsed


def _validate(
    slots: Sequence[PriceSlot], now: datetime, config: PriceScheduleConfig, timezone_name: str
) -> tuple[time | None, time | None, ZoneInfo]:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise PriceScheduleError("now must be a timezone-aware datetime")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError) as err:
        raise PriceScheduleError("timezone_name must be a valid IANA timezone") from err
    if len(slots) > _MAX_PRICE_SLOTS:
        raise PriceScheduleError(f"price slot count exceeds maximum of {_MAX_PRICE_SLOTS}")
    previous_end = None
    for slot in slots:
        if slot.start.tzinfo is None or slot.end.tzinfo is None:
            raise PriceScheduleError("price slot datetimes must include a timezone")
        start, end = slot.start.astimezone(_UTC), slot.end.astimezone(_UTC)
        if end <= start or (previous_end is not None and start < previous_end):
            raise PriceScheduleError("price slots must be positive and non-overlapping")
        if not math.isfinite(slot.price_per_kwh):
            raise PriceScheduleError("prices must be finite")
        previous_end = end
    if slots:
        first_start = min(slot.start.astimezone(_UTC) for slot in slots)
        last_end = max(slot.end.astimezone(_UTC) for slot in slots)
        if last_end - first_start > _MAX_HORIZON:
            raise PriceScheduleError("price horizon exceeds maximum of 14 days")
    if config.mode not in ("threshold", "cheapest_contiguous", "cheapest_split"):
        raise PriceScheduleError("unsupported price schedule mode")
    start_time = _time_value(config.window_start, "window_start")
    end_time = _time_value(config.window_end, "window_end")
    if (start_time is None) != (end_time is None):
        raise PriceScheduleError("window_start and window_end must be configured together")
    if config.weekdays is not None and any(day not in range(7) for day in config.weekdays):
        raise PriceScheduleError("weekdays must contain values from 0 (Monday) to 6 (Sunday)")
    if config.threshold_price_per_kwh is not None and not math.isfinite(config.threshold_price_per_kwh):
        raise PriceScheduleError("threshold price must be finite")
    if config.price_ceiling_per_kwh is not None and not math.isfinite(config.price_ceiling_per_kwh):
        raise PriceScheduleError("price ceiling must be finite")
    if config.mode == "threshold" and config.threshold_price_per_kwh is None:
        raise PriceScheduleError("threshold mode requires threshold_price_per_kwh")
    if config.mode != "threshold":
        duration = config.required_duration_minutes
        if isinstance(duration, bool) or not isinstance(duration, int) or not 0 < duration <= _MAX_DURATION_MINUTES:
            raise PriceScheduleError("required_duration_minutes must be between 1 and 1440")
        if config.min_run_minutes <= 0:
            raise PriceScheduleError("min_run_minutes must be positive")
    if config.fallback not in (None, "deadline", "price"):
        raise PriceScheduleError("fallback must be 'deadline', 'price', or None")
    if config.deadline is not None and (config.deadline.tzinfo is None or config.deadline.utcoffset() is None):
        raise PriceScheduleError("deadline must be timezone-aware")
    return start_time, end_time, zone


def _window_bounds(
    day: date, start_time: time, end_time: time, zone: ZoneInfo
) -> tuple[datetime, datetime]:
    start_local = datetime.combine(day, start_time, tzinfo=zone)
    end_day = day + timedelta(days=end_time <= start_time)
    end_local = datetime.combine(end_day, end_time, tzinfo=zone)
    return start_local.astimezone(_UTC), end_local.astimezone(_UTC)


def _next_window(
    now_utc: datetime,
    start_time: time,
    end_time: time,
    weekdays: tuple[int, ...] | None,
    zone: ZoneInfo,
) -> tuple[datetime, datetime] | None:
    local_day = now_utc.astimezone(zone).date() - timedelta(days=1)
    for offset in range(17):
        day = local_day + timedelta(days=offset)
        if weekdays is not None and day.weekday() not in weekdays:
            continue
        left, right = _window_bounds(day, start_time, end_time, zone)
        if right > now_utc:
            return left, right
    return None


def _eligible_occurrences(
    slots: Sequence[PriceSlot], now_utc: datetime, config: PriceScheduleConfig,
    start_time: time | None, end_time: time | None, zone: ZoneInfo,
    *, include_partial: bool = False,
) -> list[tuple[PriceSlot, ...]]:
    future = [slot for slot in slots if slot.end.astimezone(_UTC) > now_utc]
    if start_time is None or end_time is None:
        if config.weekdays is not None:
            future = [slot for slot in future if slot.start.astimezone(zone).weekday() in config.weekdays]
        return [tuple(future)] if future else []
    if not slots:
        return []
    local_min = min(slot.start.astimezone(zone).date() for slot in future) if future else now_utc.astimezone(zone).date()
    local_max = max(slot.end.astimezone(zone).date() for slot in future) if future else local_min
    first_day = min(local_min, now_utc.astimezone(zone).date()) - timedelta(days=1)
    occurrences: list[tuple[PriceSlot, ...]] = []
    day = first_day
    while day <= local_max:
        if config.weekdays is None or day.weekday() in config.weekdays:
            left, right = _window_bounds(day, start_time, end_time, zone)
            occurrence = []
            for slot in future:
                start = max(left, slot.start.astimezone(_UTC))
                end = min(right, slot.end.astimezone(_UTC))
                if start >= end:
                    continue
                if start == slot.start and end == slot.end:
                    occurrence.append(slot)
                elif include_partial and end > now_utc:
                    occurrence.append(PriceSlot(start, end, slot.price_per_kwh))
            if occurrence and right > now_utc:
                occurrences.append(tuple(occurrence))
        day += timedelta(days=1)
    return occurrences


def _average_price(slots: Sequence[PriceSlot]) -> float:
    duration = sum((s.end.astimezone(_UTC) - s.start.astimezone(_UTC)).total_seconds() for s in slots)
    return sum(s.price_per_kwh * (s.end.astimezone(_UTC) - s.start.astimezone(_UTC)).total_seconds() for s in slots) / duration


def _contiguous_choice(slots: Sequence[PriceSlot], required: int, ceiling: float | None) -> tuple[PriceSlot, ...] | None:
    best: tuple[float, tuple[PriceSlot, ...]] | None = None
    for start in range(len(slots)):
        candidate: list[PriceSlot] = []
        minutes = 0
        for slot in slots[start:]:
            if candidate and candidate[-1].end.astimezone(_UTC) != slot.start.astimezone(_UTC):
                break
            if ceiling is not None and slot.price_per_kwh > ceiling:
                break
            candidate.append(slot)
            minutes += round((slot.end.astimezone(_UTC) - slot.start.astimezone(_UTC)).total_seconds() / 60)
            if minutes == required:
                chosen = tuple(candidate)
                key = (_average_price(chosen), chosen[0].start.astimezone(_UTC))
                if best is None or key < (best[0], best[1][0].start.astimezone(_UTC)):
                    best = (key[0], chosen)
                break
            if minutes > required:
                break
    return None if best is None else best[1]


def _split_choice(slots: Sequence[PriceSlot], required: int, min_run: int, ceiling: float | None) -> tuple[PriceSlot, ...] | None:
    """Min-cost interval DP for uniform, slot-aligned intervals."""
    n = len(slots)
    if not slots:
        return None
    lengths = [round((s.end.astimezone(_UTC) - s.start.astimezone(_UTC)).total_seconds() / 60) for s in slots]
    slot_minutes = lengths[0]
    if slot_minutes <= 0 or any(length != slot_minutes for length in lengths):
        raise PriceScheduleError("split scheduling requires equal-length price slots")
    if required % slot_minutes or min_run % slot_minutes:
        raise PriceScheduleError("split duration and minimum run must align with the price slot length")
    target_units = required // slot_minutes
    min_run_units = min_run // slot_minutes
    if min_run_units > target_units:
        return None
    if (n + 1) * (target_units + 1) > 30_000:
        raise PriceScheduleError("split schedule exceeds the bounded computation size")

    prefix_cost = [0.0]
    for slot in slots:
        seconds = (slot.end.astimezone(_UTC) - slot.start.astimezone(_UTC)).total_seconds()
        prefix_cost.append(prefix_cost[-1] + slot.price_per_kwh * seconds / 3600)

    # At most 96 selected units fit in a day of quarter-hour prices. Costs
    # and compact back pointers keep memory bounded for small HA hosts.
    infinity = float("inf")
    costs: list[list[float]] = [[infinity] * (target_units + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int, int] | None]] = [[None] * (target_units + 1) for _ in range(n + 1)]
    costs[0][0] = 0.0
    for end in range(1, n + 1):
        costs[end] = costs[end - 1].copy()  # leave slot end-1 unused
        for used in range(target_units + 1):
            if costs[end - 1][used] != infinity:
                back[end][used] = (end - 1, used, 0)
        if ceiling is None or slots[end - 1].price_per_kwh <= ceiling:
            for begin in range(end - 1, -1, -1):
                if ceiling is not None and slots[begin].price_per_kwh > ceiling:
                    break
                if begin < end - 1 and slots[begin].end.astimezone(_UTC) != slots[begin + 1].start.astimezone(_UTC):
                    break
                block_units = end - begin
                if block_units < min_run_units:
                    continue
                if block_units > target_units:
                    break
                block_cost = prefix_cost[end] - prefix_cost[begin]
                for prior_units in range(target_units - block_units + 1):
                    prior_cost = costs[begin][prior_units]
                    total = prior_units + block_units
                    candidate_cost = prior_cost + block_cost
                    if prior_cost != infinity and candidate_cost < costs[end][total]:
                        costs[end][total] = candidate_cost
                        back[end][total] = (begin, prior_units, block_units)
    if costs[n][target_units] == infinity:
        return None
    chosen_blocks: list[tuple[int, ...]] = []
    end, units = n, target_units
    while end > 0:
        pointer = back[end][units]
        if pointer is None:
            return None
        previous_end, previous_units, block_units = pointer
        if block_units:
            chosen_blocks.append(tuple(range(previous_end, end)))
        end, units = previous_end, previous_units
    chosen_indices = [index for block in reversed(chosen_blocks) for index in block]
    return tuple(slots[i] for i in chosen_indices)


def plan_price_schedule(
    slots: Sequence[PriceSlot],
    now: datetime,
    config: PriceScheduleConfig,
    timezone_name: str,
) -> PriceScheduleResult:
    """Evaluate a threshold or choose the cheapest eligible usage intervals.

    The solver accepts at most 200 slots. `deadline` is a hard UTC instant;
    when a price ceiling and deadline cannot both be met, `fallback` controls
    which constraint is relaxed. A fallback is only used after the strict
    plan fails and is recorded in `warnings`.
    """
    start_time, end_time, zone = _validate(slots, now, config, timezone_name)
    ordered = tuple(sorted(slots, key=lambda s: s.start.astimezone(_UTC)))
    now_utc = now.astimezone(_UTC)
    occurrences = _eligible_occurrences(
        ordered,
        now_utc,
        config,
        start_time,
        end_time,
        zone,
        include_partial=config.mode == "threshold",
    )
    if config.mode == "threshold":
        candidates = tuple(slot for group in occurrences for slot in group)
        selected = tuple(slot for slot in candidates if slot.price_per_kwh <= config.threshold_price_per_kwh)
        window_active = True
        if start_time is not None and end_time is not None:
            active_window = _next_window(now_utc, start_time, end_time, config.weekdays, zone)
            window_active = bool(active_window and active_window[0] <= now_utc < active_window[1])
        current = next((s for s in selected if s.start.astimezone(_UTC) <= now_utc < s.end.astimezone(_UTC)), None)
        if not window_active:
            current = None
        transition_points = [s.start.astimezone(_UTC) for s in candidates if s.start.astimezone(_UTC) > now_utc]
        transition_points += [s.end.astimezone(_UTC) for s in candidates if s.end.astimezone(_UTC) > now_utc]
        if start_time is not None and end_time is not None:
            horizon_end = max((s.end.astimezone(_UTC) for s in ordered), default=now_utc + timedelta(days=2))
            day = now_utc.astimezone(zone).date() - timedelta(days=1)
            last_day = horizon_end.astimezone(zone).date() + timedelta(days=1)
            while day <= last_day:
                if config.weekdays is None or day.weekday() in config.weekdays:
                    left, right = _window_bounds(day, start_time, end_time, zone)
                    transition_points.extend(point for point in (left, right) if point > now_utc)
                day += timedelta(days=1)
        next_transition = min(transition_points) if transition_points else None
        if not candidates:
            return PriceScheduleResult(False, "no_prices", "no_eligible_prices", (), (), next_transition)
        return PriceScheduleResult(
            current is not None,
            "active" if current else "scheduled" if selected else "above_threshold",
            "price_at_or_below_limit" if current else "no_qualifying_price_now",
            selected,
            (),
            next_transition,
        )

    occurrence_end: datetime | None = None
    if start_time is not None and end_time is not None:
        bounds = _next_window(now_utc, start_time, end_time, config.weekdays, zone)
        if bounds is None:
            return PriceScheduleResult(False, "no_window", "no_applicable_window")
        left, occurrence_end = bounds
        deadline_utc = config.deadline.astimezone(_UTC) if config.deadline else None
        coverage_target = min(occurrence_end, deadline_utc) if deadline_utc else occurrence_end
        # Wait for the selected occurrence to be fully published before
        # locking a cheapest plan. This avoids switching to tomorrow while
        # today's price coverage is still arriving.
        relevant = tuple(
            s for s in ordered
            if s.start.astimezone(_UTC) < coverage_target and s.end.astimezone(_UTC) > left
        )
        coverage_end = max((s.end.astimezone(_UTC) for s in relevant), default=None)
        if coverage_end is None or coverage_end < coverage_target:
            return PriceScheduleResult(
                False,
                "waiting_for_prices",
                "waiting_for_window_prices",
                occurrence_end=occurrence_end,
            )
        occurrence = tuple(
            s for s in relevant
            if s.start.astimezone(_UTC) >= max(left, now_utc)
            and s.end.astimezone(_UTC) <= occurrence_end
        )
        if deadline_utc is not None:
            occurrence = tuple(s for s in occurrence if s.end.astimezone(_UTC) <= deadline_utc)
        if not occurrence:
            return PriceScheduleResult(
                False,
                "no_window",
                "no_full_interval_in_window",
                occurrence_end=occurrence_end,
            )
    else:
        if not occurrences:
            return PriceScheduleResult(False, "no_prices", "no_future_prices")
        occurrence = min(occurrences, key=lambda group: group[0].start.astimezone(_UTC))
        occurrence = tuple(slot for slot in occurrence if slot.start.astimezone(_UTC) >= now_utc)
        occurrence_end = max((s.end.astimezone(_UTC) for s in occurrence), default=None)
        if not occurrence:
            return PriceScheduleResult(False, "no_prices", "no_future_prices")
    deadline = config.deadline.astimezone(_UTC) if config.deadline else None
    target = config.required_duration_minutes

    def solve(ceiling: float | None, end_limit: datetime | None) -> tuple[PriceSlot, ...] | None:
        eligible = tuple(s for s in occurrence if (end_limit is None or s.end.astimezone(_UTC) <= end_limit))
        if config.mode == "cheapest_contiguous":
            return _contiguous_choice(eligible, target, ceiling)
        return _split_choice(eligible, target, config.min_run_minutes, ceiling)

    chosen = solve(config.price_ceiling_per_kwh, deadline)
    warnings: list[str] = []
    if chosen is None and config.fallback == "deadline":
        chosen = solve(None, deadline)
        if chosen is not None:
            warnings.append("price_ceiling_relaxed")
    elif chosen is None and config.fallback == "price":
        chosen = solve(config.price_ceiling_per_kwh, None)
        if chosen is not None:
            warnings.append("deadline_relaxed")
    if chosen is None:
        ignoring_ceiling = (
            solve(None, deadline) is not None if config.price_ceiling_per_kwh is not None else False
        )
        ignoring_deadline = solve(config.price_ceiling_per_kwh, None) is not None if deadline else False
        if ignoring_ceiling and ignoring_deadline:
            status = "constraints_unsatisfied"
            reason_code = "constraints_unsatisfied"
        elif ignoring_ceiling:
            status = "price_ceiling"
            reason_code = "price_ceiling"
        elif ignoring_deadline:
            status = "deadline"
            reason_code = "deadline"
        else:
            status = "insufficient_duration"
            reason_code = "insufficient_duration"
        return PriceScheduleResult(False, status, reason_code, occurrence_end=occurrence_end)
    active = any(s.start.astimezone(_UTC) <= now_utc < s.end.astimezone(_UTC) for s in chosen)
    return PriceScheduleResult(
        active,
        "active" if active else "scheduled",
        "cheapest_intervals_selected" if not warnings else warnings[0],
        chosen,
        tuple(warnings),
        min((s.end.astimezone(_UTC) for s in chosen if s.end.astimezone(_UTC) > now_utc), default=None),
        occurrence_end,
    )
