"""Reproducible microbenchmark for bounded 200-slot optimizer workloads.

This exercises only the pure optimizer. It is not an HA loop, provider, or
device-class performance test. Run from the repository root.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from custom_components.electricity_price_scheduler.core.models import (
    OptimizerConfig,
    PriceSlot,
)
from custom_components.electricity_price_scheduler.core.optimizer import optimize


def make_slots(*, stress: bool) -> tuple[PriceSlot, ...]:
    start = datetime(2026, 11, 5, tzinfo=timezone.utc)
    slots = []
    for index in range(200):
        phase = index % 32
        price = (
            -1.0
            if stress
            else 0.055 + ((index * 37) % 13) * 0.0015 + (0.09 if 12 <= phase < 18 else 0.0)
        )
        left = start + timedelta(minutes=15 * index)
        slots.append(PriceSlot(left, left + timedelta(minutes=15), price))
    return tuple(slots)


def make_config(*, max_periods: int, stress: bool) -> OptimizerConfig:
    if stress:
        return OptimizerConfig(
            min_cutoff_minutes=30,
            max_cutoff_minutes=360,
            min_price_diff_per_kwh=0.0,
            max_periods=max_periods,
            preheat_factor=3.0,
            recovery_factor=3.0,
        )
    return OptimizerConfig(max_periods=max_periods)


def measure(*, max_periods: int, stress: bool, trials: int) -> dict[str, int | float]:
    slots = make_slots(stress=stress)
    config = make_config(max_periods=max_periods, stress=stress)
    call = lambda: optimize(slots, config, not_before=slots[0].start)

    for _ in range(30):
        call()

    samples_ms = []
    for _ in range(trials):
        started = time.perf_counter_ns()
        call()
        samples_ms.append((time.perf_counter_ns() - started) / 1_000_000)
    samples_ms.sort()
    p95_index = min(len(samples_ms) - 1, int(0.95 * len(samples_ms)))

    tracemalloc.start()
    call()
    _, peak_bytes = tracemalloc.get_traced_memory()
    snapshot = tracemalloc.take_snapshot()
    current_traced_blocks = sum(stat.count for stat in snapshot.statistics("filename"))
    tracemalloc.stop()

    result = call()
    return {
        "k": max_periods,
        "warm_p95_ms": round(samples_ms[p95_index], 4),
        "tracemalloc_peak_bytes": peak_bytes,
        "traced_blocks_after_call": current_traced_blocks,
        "candidates_scanned": result.candidates_scanned,
        "candidates_accepted": result.candidates_accepted,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, help="Label recorded with the result")
    parser.add_argument("--scenario", choices=("normal", "stress", "both"), default="both")
    parser.add_argument("--trials", type=int, default=300)
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials must be positive")

    scenarios = ("normal", "stress") if args.scenario == "both" else (args.scenario,)
    output = {
        "runtime": args.runtime,
        "python": sys.version.split()[0],
        "slots": 200,
        "trials": args.trials,
        "warmup_calls_per_k": 30,
        "scenarios": {
            scenario: [
                measure(max_periods=k, stress=scenario == "stress", trials=args.trials)
                for k in (1, 6, 12, 24)
            ]
            for scenario in scenarios
        },
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
