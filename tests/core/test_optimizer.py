from datetime import datetime, timedelta, timezone
from itertools import combinations
import json
from pathlib import Path
import unittest

from custom_components.electricity_price_scheduler.core.models import OptimizerConfig, PriceSlot
from custom_components.electricity_price_scheduler.core.normalization import normalize_slots
from custom_components.electricity_price_scheduler.core.optimizer import optimize


FIXTURE_DIR = Path(__file__).parents[1] / "fixtures"


def regular_slots(prices, *, start=None, step_minutes=15):
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return tuple(
        PriceSlot(
            start + timedelta(minutes=step_minutes * index),
            start + timedelta(minutes=step_minutes * (index + 1)),
            price,
        )
        for index, price in enumerate(prices)
    )


def brute_force_oracle(slots, config, not_before):
    """Tiny independent subset oracle for uniform 15-minute fixtures."""
    candidates = []
    min_length = config.min_cutoff_minutes // 15
    max_length = config.max_cutoff_minutes // 15
    for shutdown in range(len(slots)):
        if slots[shutdown].start < not_before:
            continue
        for length in range(min_length, max_length + 1):
            pre = shutdown - length
            cutoff_end = shutdown + length
            recovery_end = shutdown + 2 * length
            if pre < 0 or recovery_end > len(slots):
                continue
            if any(slots[index].end != slots[index + 1].start for index in range(pre, recovery_end - 1)):
                continue
            hours = length * 0.25
            pre_cost = sum(slots[i].price_per_kwh * 0.25 for i in range(pre, shutdown))
            high_cost = sum(slots[i].price_per_kwh * 0.25 for i in range(shutdown, cutoff_end))
            rec_cost = sum(slots[i].price_per_kwh * 0.25 for i in range(cutoff_end, recovery_end))
            baseline = pre_cost + high_cost + rec_cost
            planned = pre_cost * config.preheat_factor + high_cost * config.cutoff_factor + rec_cost * config.recovery_factor
            saving = baseline - planned
            price_diff = high_cost / hours - (pre_cost * config.preheat_factor + rec_cost * config.recovery_factor) / (hours * (config.preheat_factor + config.recovery_factor))
            if saving > 0 and price_diff >= config.min_price_diff_per_kwh / (hours / 1.0):
                candidates.append((slots[pre].start, slots[recovery_end - 1].end, saving))

    best = 0.0
    for size in range(min(config.max_periods, len(candidates)) + 1):
        for subset in combinations(candidates, size):
            ordered = sorted(subset)
            if all(ordered[i][1] <= ordered[i + 1][0] for i in range(len(ordered) - 1)):
                best = max(best, sum(item[2] for item in ordered))
    return best


class OptimizerTests(unittest.TestCase):
    def test_legacy_healthy_fixture_matches_legacy_schedule_and_saving(self):
        fixture = json.loads((FIXTURE_DIR / "legacy_healthy_case.json").read_text())
        start = datetime.fromisoformat(fixture["start"])
        raw = [
            {
                "start": start + timedelta(minutes=fixture["interval_minutes"] * i),
                "end": start + timedelta(minutes=fixture["interval_minutes"] * (i + 1)),
                "value": price * 100,
            }
            for i, price in enumerate(fixture["prices_eur_per_kwh"])
        ]
        slots = normalize_slots(raw, source_unit="snt/kWh", currency="EUR")
        config = OptimizerConfig(
            min_cutoff_minutes=120,
            max_cutoff_minutes=120,
            min_price_diff_per_kwh=0.03,
            max_periods=6,
        )
        result = optimize(slots, config, not_before=start)
        self.assertEqual(len(result.periods), 1)
        period = result.periods[0]
        expected = fixture["legacy_expected"]
        self.assertEqual(period.preheat_start.isoformat(), expected["preheat_start"])
        self.assertEqual(period.shutdown_start.isoformat(), expected["shutdown_start"])
        self.assertEqual(period.recovery_start.isoformat(), expected["recovery_start"])
        self.assertEqual(period.recovery_end.isoformat(), expected["recovery_end"])
        self.assertAlmostEqual(period.savings_per_kw, expected["savings_per_kw"])

    def test_k_limited_dp_matches_exhaustive_subset_oracle(self):
        patterns = (
            [0.02, 0.02, 0.03, 0.3, 0.3, 0.02, 0.02, 0.25, 0.02, 0.02, 0.02, 0.02],
            [0.1, 0.02, 0.02, 0.2, 0.2, 0.2, 0.02, 0.02, 0.3, 0.02, 0.02, 0.02],
            [0.01, 0.3, 0.01, 0.01, 0.25, 0.25, 0.01, 0.01, 0.2, 0.2, 0.01, 0.01],
        )
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for max_periods in (1, 2, 6, 12, 24):
            for pattern in patterns:
                with self.subTest(k=max_periods, pattern=patterns.index(pattern)):
                    slots = regular_slots(pattern, start=start)
                    config = OptimizerConfig(
                        min_cutoff_minutes=30,
                        max_cutoff_minutes=30,
                        min_price_diff_per_kwh=0.01,
                        max_periods=max_periods,
                    )
                    result = optimize(slots, config, not_before=start)
                    expected = brute_force_oracle(slots, config, start)
                    self.assertAlmostEqual(result.total_savings_per_kw, expected, places=10)
                    self.assertLessEqual(len(result.periods), max_periods)
                    for left, right in zip(result.periods, result.periods[1:]):
                        self.assertLessEqual(left.recovery_end, right.preheat_start)

    def test_not_before_applies_to_preheat_start(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        prices = [0.05] * 12
        prices[6:8] = [0.5, 0.5]
        slots = regular_slots(prices, start=start)
        result = optimize(
            slots,
            OptimizerConfig(min_cutoff_minutes=30, max_cutoff_minutes=30),
            not_before=start + timedelta(minutes=15 * 5),
        )
        self.assertTrue(all(p.preheat_start >= start + timedelta(minutes=75) for p in result.periods))

    def test_gap_cannot_be_spanned_and_intervals_can_touch(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        slots = list(regular_slots([0.05] * 6 + [0.5] * 2 + [0.05] * 6, start=start))
        # Missing priced period between preheat and shutdown opportunities.
        slots[5] = PriceSlot(slots[5].start, slots[5].end - timedelta(minutes=5), slots[5].price_per_kwh)
        result = optimize(
            slots,
            OptimizerConfig(min_cutoff_minutes=30, max_cutoff_minutes=30),
            not_before=start,
        )
        self.assertEqual(result.periods, ())

    def test_negative_prices_can_yield_savings_with_undefined_percent(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # A 30-minute negative-price preheat and recovery makes the baseline
        # cost negative while zero-cost cutoff still improves the model.
        slots = regular_slots([-10.0] * 4 + [1.0] * 2 + [-10.0] * 4, start=start)
        result = optimize(
            slots,
            OptimizerConfig(min_cutoff_minutes=30, max_cutoff_minutes=30),
            not_before=start,
        )
        self.assertTrue(result.periods)
        self.assertTrue(all(period.savings_per_kw > 0 for period in result.periods))
        self.assertTrue(all(period.savings_percent is None for period in result.periods))

    def test_validation_rejects_bad_config_and_slots(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "max_periods"):
            optimize((), OptimizerConfig(max_periods=25), not_before=start)
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            optimize((), OptimizerConfig(preheat_factor=0), not_before=start)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            optimize((), OptimizerConfig(), not_before=datetime(2026, 1, 1))
        with self.assertRaisesRegex(ValueError, "sorted"):
            optimize(
                (PriceSlot(start + timedelta(minutes=15), start + timedelta(minutes=30), 1), PriceSlot(start, start + timedelta(minutes=15), 1)),
                OptimizerConfig(),
                not_before=start,
            )


if __name__ == "__main__":
    unittest.main()
