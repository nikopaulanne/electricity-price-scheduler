"""Manual, dependency-free core benchmark; run with ``PYTHONPATH=. python3 tests/core/benchmark_optimizer.py``.

This is a local measurement aid, not a CI threshold and not proof of Raspberry
Pi performance. tracemalloc reports Python allocations, not total HA RSS.
"""

from datetime import datetime, timedelta, timezone
import time
import tracemalloc

from custom_components.electricity_price_scheduler.core import OptimizerConfig, PriceSlot, optimize


def main() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    slots = []
    for index in range(200):
        # Repeating high-price blocks exercise a dense, realistic candidate set.
        block_position = index % 32
        price = 0.35 if 12 <= block_position < 20 else 0.04 + (index % 5) * 0.002
        slots.append(
            PriceSlot(
                start + timedelta(minutes=15 * index),
                start + timedelta(minutes=15 * (index + 1)),
                price,
            )
        )
    config = OptimizerConfig(
        min_cutoff_minutes=30,
        max_cutoff_minutes=360,
        min_price_diff_per_kwh=0.01,
        max_periods=6,
    )

    tracemalloc.start()
    tracemalloc.reset_peak()
    before_current, _ = tracemalloc.get_traced_memory()
    began = time.perf_counter()
    result = optimize(slots, config, not_before=start)
    elapsed = time.perf_counter() - began
    after_current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"elapsed_seconds={elapsed:.6f}")
    print(f"python_allocated_current_delta_bytes={max(0, after_current - before_current)}")
    print(f"python_allocated_peak_bytes={max(0, peak - before_current)}")
    print(f"slots={result.slot_count}")
    print(f"candidates_scanned={result.candidates_scanned}")
    print(f"candidates_accepted={result.candidates_accepted}")
    print(f"selected_periods={len(result.periods)}")


if __name__ == "__main__":
    main()
