"""Bounded, dependency-free schedule optimizer."""

from __future__ import annotations

import bisect
import math
import numbers
from array import array
from datetime import datetime, timezone
from typing import Optional, Sequence

from ..const import (
    CUTOFF_STEP_MINUTES,
    MAX_CUTOFF_MINUTES,
    MAX_OPTIMIZER_PERIODS,
    MAX_SLOTS,
    MIN_MAX_PERIODS,
    SLOT_MINUTES,
)
from .models import CutoffPeriod, OptimizationResult, OptimizerConfig, PriceSlot


_UTC = timezone.utc


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(_UTC)


def _validate_config(config: OptimizerConfig) -> None:
    if not isinstance(config, OptimizerConfig):
        raise TypeError("config must be an OptimizerConfig")
    if not isinstance(config.currency, str) or not config.currency.strip():
        raise ValueError("currency must be provided")
    if config.slot_minutes != SLOT_MINUTES:
        raise ValueError(f"slot_minutes must be {SLOT_MINUTES}")
    for name, value in (
        ("min_cutoff_minutes", config.min_cutoff_minutes),
        ("max_cutoff_minutes", config.max_cutoff_minutes),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
        if value < SLOT_MINUTES * 2 or value > MAX_CUTOFF_MINUTES or value % CUTOFF_STEP_MINUTES:
            raise ValueError(f"{name} must be a 15-minute multiple between 30 and {MAX_CUTOFF_MINUTES}")
    if config.min_cutoff_minutes > config.max_cutoff_minutes:
        raise ValueError("min_cutoff_minutes must not exceed max_cutoff_minutes")
    if not isinstance(config.max_periods, int) or isinstance(config.max_periods, bool):
        raise ValueError("max_periods must be an integer")
    if not MIN_MAX_PERIODS <= config.max_periods <= MAX_OPTIMIZER_PERIODS:
        raise ValueError(f"max_periods must be between {MIN_MAX_PERIODS} and {MAX_OPTIMIZER_PERIODS}")
    for name, value in (
        ("min_price_diff_per_kwh", config.min_price_diff_per_kwh),
        ("preheat_factor", config.preheat_factor),
        ("recovery_factor", config.recovery_factor),
        ("cutoff_factor", config.cutoff_factor),
    ):
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            raise ValueError(f"{name} must be numeric")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError(f"{name} must be finite")
        if name == "min_price_diff_per_kwh" and numeric < 0:
            raise ValueError("min_price_diff_per_kwh must not be negative")
        if name != "min_price_diff_per_kwh" and numeric <= 0 and name != "cutoff_factor":
            raise ValueError(f"{name} must be greater than zero")
        if name == "cutoff_factor" and numeric < 0:
            raise ValueError("cutoff_factor must not be negative")


def _validate_slots(slots: Sequence[PriceSlot]) -> tuple[PriceSlot, ...]:
    if len(slots) > MAX_SLOTS:
        raise ValueError(f"slot count exceeds maximum of {MAX_SLOTS}")
    ordered = tuple(slots)
    previous_end: Optional[datetime] = None
    for slot in ordered:
        if not isinstance(slot, PriceSlot):
            raise TypeError("slots must contain PriceSlot values")
        start = _utc(slot.start, "slot.start")
        end = _utc(slot.end, "slot.end")
        try:
            price = float(slot.price_per_kwh)
        except (TypeError, ValueError, OverflowError) as err:
            raise ValueError("slot price must be numeric") from err
        if end <= start:
            raise ValueError("slot end must be later than start")
        if not math.isfinite(price):
            raise ValueError("slot price must be finite")
        if previous_end is not None and start < previous_end:
            raise ValueError("slots must be sorted and non-overlapping")
        previous_end = end
    return ordered


def _add_bit(bits: bytearray, row: int, col: int, bytes_per_row: int) -> None:
    offset = row * bytes_per_row + col // 8
    bits[offset] |= 1 << (col % 8)


def _has_bit(bits: bytearray, row: int, col: int, bytes_per_row: int) -> bool:
    return bool(bits[row * bytes_per_row + col // 8] & (1 << (col % 8)))


def optimize(
    slots: Sequence[PriceSlot],
    config: OptimizerConfig,
    *,
    not_before: datetime,
) -> OptimizationResult:
    """Find the maximum-saving non-overlapping schedule, limited to K periods.

    Slot intervals are half-open. Candidate preheat, cutoff, and recovery
    phases have equal elapsed durations and must fit entirely inside contiguous
    priced coverage. Candidate boundaries are actual slot boundaries; the
    requested duration is always a multiple of 15 minutes. ``not_before``
    constrains the start of preheat, not just the cutoff.
    """
    _validate_config(config)
    horizon = _utc(not_before, "not_before")
    slots = _validate_slots(slots)
    count = len(slots)
    if not count:
        return OptimizationResult((), 0.0, 0, 0, 0, config.currency.upper())

    # Prefix sums use compact numeric arrays. Cost values represent currency
    # for a 1 kW load because one kW × one hour is one kWh.
    duration_s = array("q", [0])
    energy_cost = array("d", [0.0])
    gap_count = array("i", [0])
    for index, slot in enumerate(slots):
        start = _utc(slot.start, "slot.start")
        end = _utc(slot.end, "slot.end")
        elapsed_float = (end - start).total_seconds()
        if not elapsed_float.is_integer():
            raise ValueError("slot duration must be an integer number of seconds")
        elapsed = int(elapsed_float)
        duration_s.append(duration_s[-1] + elapsed)
        next_cost = energy_cost[-1] + float(slot.price_per_kwh) * elapsed / 3600.0
        if not math.isfinite(next_cost):
            raise ValueError("integrated price cost exceeds numeric limits")
        energy_cost.append(next_cost)
        if index + 1 < count:
            gap_count.append(gap_count[-1] + int(end != _utc(slots[index + 1].start, "slot.start")))

    # Candidates are stored as compact slot-boundary indices plus float64
    # weights. Full dataclasses and detail values are created only on output.
    starts = array("i")
    shutdowns = array("i")
    cutoff_ends = array("i")
    recoveries_ends = array("i")
    weights = array("d")
    candidates_scanned = 0
    candidates_accepted = 0
    reference_seconds = 3600

    min_seconds = config.min_cutoff_minutes * 60
    max_seconds = config.max_cutoff_minutes * 60
    step_seconds = CUTOFF_STEP_MINUTES * 60
    boundary_index = {duration_s[index]: index for index in range(count + 1)}

    for shutdown_idx, shutdown_slot in enumerate(slots):
        if _utc(shutdown_slot.start, "slot.start") < horizon:
            continue
        for cutoff_seconds in range(min_seconds, max_seconds + 1, step_seconds):
            candidates_scanned += 1
            cutoff_end_idx = boundary_index.get(duration_s[shutdown_idx] + cutoff_seconds)
            if cutoff_end_idx is None or cutoff_end_idx <= shutdown_idx:
                continue
            preheat_idx = boundary_index.get(duration_s[shutdown_idx] - cutoff_seconds)
            if preheat_idx is None or preheat_idx >= shutdown_idx:
                continue
            recovery_end_idx = boundary_index.get(duration_s[cutoff_end_idx] + cutoff_seconds)
            if recovery_end_idx is None or recovery_end_idx <= cutoff_end_idx:
                continue
            if _utc(slots[preheat_idx].start, "slot.start") < horizon:
                continue
            # gap_count[r]-gap_count[l] counts discontinuities between slots
            # with indices l..r inclusive; no gaps are permitted in the full
            # preheat-through-recovery schedule.
            if gap_count[recovery_end_idx - 1] - gap_count[preheat_idx] != 0:
                continue

            pre_cost = energy_cost[shutdown_idx] - energy_cost[preheat_idx]
            high_cost = energy_cost[cutoff_end_idx] - energy_cost[shutdown_idx]
            recovery_cost = energy_cost[recovery_end_idx] - energy_cost[cutoff_end_idx]
            phase_hours = cutoff_seconds / 3600.0
            baseline = pre_cost + high_cost + recovery_cost
            planned = (
                pre_cost * config.preheat_factor
                + high_cost * config.cutoff_factor
                + recovery_cost * config.recovery_factor
            )
            saving = baseline - planned
            if not all(math.isfinite(value) for value in (baseline, planned, saving)):
                raise ValueError("candidate cost exceeds numeric limits")
            weighted_rest_hours = phase_hours * (config.preheat_factor + config.recovery_factor)
            average_rest = (
                (pre_cost * config.preheat_factor + recovery_cost * config.recovery_factor)
                / weighted_rest_hours
                if weighted_rest_hours > 0
                else 0.0
            )
            average_cutoff = high_cost / phase_hours if phase_hours > 0 else 0.0
            price_diff = average_cutoff - average_rest
            required_diff = config.min_price_diff_per_kwh * reference_seconds / cutoff_seconds
            if not math.isfinite(price_diff):
                raise ValueError("candidate price difference exceeds numeric limits")
            if not math.isfinite(required_diff):
                continue
            if saving <= 0.0 or price_diff < required_diff:
                continue

            candidates_accepted += 1
            starts.append(preheat_idx)
            shutdowns.append(shutdown_idx)
            cutoff_ends.append(cutoff_end_idx)
            recoveries_ends.append(recovery_end_idx)
            weights.append(saving)

    if not weights:
        return OptimizationResult(
            (), 0.0, candidates_scanned, 0, count, config.currency.upper()
        )

    # Stable ordering makes equal-score outcomes deterministic.
    order = sorted(
        range(len(weights)),
        key=lambda ix: (
            recoveries_ends[ix], starts[ix], shutdowns[ix], cutoff_ends[ix]
        ),
    )
    sorted_starts = array("i", (starts[ix] for ix in order))
    sorted_shutdowns = array("i", (shutdowns[ix] for ix in order))
    sorted_cutoff_ends = array("i", (cutoff_ends[ix] for ix in order))
    sorted_recoveries_ends = array("i", (recoveries_ends[ix] for ix in order))
    sorted_weights = array("d", (weights[ix] for ix in order))
    candidate_count = len(order)

    predecessors = array("i")
    for index in range(candidate_count):
        # Prefix length of intervals whose recovery end is <= this start.
        predecessors.append(
            bisect.bisect_right(
                sorted_recoveries_ends, sorted_starts[index], 0, index
            )
        )

    max_k = min(config.max_periods, candidate_count)
    bytes_per_row = (candidate_count + 7) // 8
    decisions = bytearray(max_k * bytes_per_row)
    previous = array("d", [0.0]) * (candidate_count + 1)
    current = array("d", [0.0]) * (candidate_count + 1)

    for k in range(max_k):
        current[0] = 0.0
        decision_row = k
        for prefix in range(1, candidate_count + 1):
            skip = current[prefix - 1]
            candidate = prefix - 1
            take = sorted_weights[candidate] + previous[predecessors[candidate]]
            if not math.isfinite(take):
                raise ValueError("combined schedule saving exceeds numeric limits")
            # Strict comparison preserves the prior prefix on ties.
            if take > skip:
                current[prefix] = take
                _add_bit(decisions, decision_row, candidate, bytes_per_row)
            else:
                current[prefix] = skip
        previous, current = current, previous

    chosen_indices: list[int] = []
    prefix = candidate_count
    k = max_k - 1
    while prefix > 0 and k >= 0:
        candidate = prefix - 1
        if _has_bit(decisions, k, candidate, bytes_per_row):
            chosen_indices.append(candidate)
            prefix = predecessors[candidate]
            k -= 1
        else:
            prefix -= 1

    periods: list[CutoffPeriod] = []
    for candidate in chosen_indices:
        pre_idx = sorted_starts[candidate]
        shutdown_idx = sorted_shutdowns[candidate]
        cutoff_end_idx = sorted_cutoff_ends[candidate]
        recovery_end_idx = sorted_recoveries_ends[candidate]
        pre_cost = energy_cost[shutdown_idx] - energy_cost[pre_idx]
        high_cost = energy_cost[cutoff_end_idx] - energy_cost[shutdown_idx]
        recovery_cost = energy_cost[recovery_end_idx] - energy_cost[cutoff_end_idx]
        phase_seconds = duration_s[cutoff_end_idx] - duration_s[shutdown_idx]
        phase_hours = phase_seconds / 3600.0
        baseline = pre_cost + high_cost + recovery_cost
        planned = (
            pre_cost * config.preheat_factor
            + high_cost * config.cutoff_factor
            + recovery_cost * config.recovery_factor
        )
        saving = baseline - planned
        if not all(math.isfinite(value) for value in (baseline, planned, saving)):
            raise ValueError("selected schedule cost exceeds numeric limits")
        periods.append(
            CutoffPeriod(
                preheat_start=_utc(slots[pre_idx].start, "slot.start"),
                shutdown_start=_utc(slots[shutdown_idx].start, "slot.start"),
                recovery_start=_utc(slots[cutoff_end_idx].start, "slot.start"),
                recovery_end=_utc(slots[recovery_end_idx - 1].end, "slot.end"),
                savings_per_kw=saving,
                savings_percent=(saving / baseline * 100.0 if baseline > 0.0 else None),
                baseline_cost_per_kw=baseline,
                planned_cost_per_kw=planned,
                price_difference_per_kwh=(
                    (high_cost / phase_hours)
                    - ((pre_cost * config.preheat_factor + recovery_cost * config.recovery_factor)
                       / (phase_hours * (config.preheat_factor + config.recovery_factor)))
                ),
                min_price_diff_per_kwh=(
                    config.min_price_diff_per_kwh * reference_seconds / phase_seconds
                ),
                shutdown_duration_hours=phase_hours,
            )
        )
    periods.sort(key=lambda period: (period.preheat_start, period.shutdown_start))
    total_savings = sum(period.savings_per_kw for period in periods)
    return OptimizationResult(
        tuple(periods),
        total_savings,
        candidates_scanned,
        candidates_accepted,
        count,
        config.currency.upper(),
    )
