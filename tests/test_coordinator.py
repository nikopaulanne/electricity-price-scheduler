"""Runtime coordinator contract and active-window boundary tests."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from custom_components.electricity_price_scheduler.coordinator import (
    DataStatus,
    _runtime_snapshot,
    _supports_locked_window,
    _calculate_result,
    _temperature_to_celsius,
    _resolve_helper_options,
    _resolve_helper_options_with_provenance,
)
from custom_components.electricity_price_scheduler.core.models import (
    CutoffPeriod,
    MultiplierResult,
    OptimizationResult,
    OptimizerConfig,
    PriceSlot,
    PriceSnapshot,
)

UTC = timezone.utc


def _period(start: datetime) -> CutoffPeriod:
    return CutoffPeriod(
        preheat_start=start,
        shutdown_start=start + timedelta(minutes=30),
        recovery_start=start + timedelta(hours=1),
        recovery_end=start + timedelta(hours=1, minutes=30),
        savings_per_kw=0.1,
        savings_percent=10.0,
        baseline_cost_per_kw=1.0,
        planned_cost_per_kw=0.9,
        price_difference_per_kwh=0.1,
        min_price_diff_per_kwh=0.03,
        shutdown_duration_hours=0.5,
    )


class CoordinatorContractTests(unittest.TestCase):
    def test_missing_tomorrow_is_waiting_even_with_no_periods(self):
        now = datetime(2026, 11, 5, 12, tzinfo=UTC)
        prices = PriceSnapshot((), "EUR", "test", tomorrow_available=False)
        result = OptimizationResult((), 0, 0, 0, 0, "EUR")
        runtime = _runtime_snapshot(
            prices, result, now, last_success=now, multipliers=None
        )
        self.assertEqual(runtime.status, DataStatus.WAITING_FOR_PRICES)
        self.assertTrue(runtime.available)
        self.assertEqual(runtime.phase, "normal")

    def test_no_profitable_periods_is_normal_and_available(self):
        now = datetime(2026, 11, 5, 12, tzinfo=UTC)
        prices = PriceSnapshot((), "EUR", "test", tomorrow_available=True)
        result = OptimizationResult((), 0, 0, 0, 0, "EUR")
        runtime = _runtime_snapshot(
            prices, result, now, last_success=now, multipliers=None
        )
        self.assertEqual(runtime.status, DataStatus.NO_PROFITABLE_PERIODS)
        self.assertTrue(runtime.available)
        self.assertEqual(runtime.phase, "normal")

    def test_current_cutoff_has_no_past_next_start(self):
        now = datetime(2026, 11, 5, 1, 1, tzinfo=UTC)
        period = _period(datetime(2026, 11, 5, 0, 30, tzinfo=UTC))
        prices = PriceSnapshot((), "EUR", "test", tomorrow_available=True)
        result = OptimizationResult((period,), 0.1, 1, 1, 6, "EUR")
        runtime = _runtime_snapshot(
            prices, result, now, last_success=now, multipliers=None
        )
        self.assertEqual(runtime.phase, "shutdown")
        self.assertIsNone(runtime.next_period)

    def test_lock_survives_local_midnight_when_remaining_coverage_exists(self):
        start = datetime(2026, 11, 5, 21, 30, tzinfo=UTC)
        locked = _period(start)
        now = datetime(2026, 11, 5, 22, 10, tzinfo=UTC)
        slots = tuple(
            PriceSlot(
                start=begin,
                end=begin + timedelta(minutes=15),
                price_per_kwh=0.2,
            )
            for begin in (
                datetime(2026, 11, 5, 22, tzinfo=UTC),
                datetime(2026, 11, 5, 22, 15, tzinfo=UTC),
                datetime(2026, 11, 5, 22, 30, tzinfo=UTC),
                datetime(2026, 11, 5, 22, 45, tzinfo=UTC),
                datetime(2026, 11, 5, 23, tzinfo=UTC),
            )
        )
        snapshot = PriceSnapshot(slots, "EUR", "test", tomorrow_available=True)
        self.assertTrue(_supports_locked_window(snapshot, locked, now))

    def test_lock_expires_and_gaps_in_remaining_coverage_reject_it(self):
        start = datetime(2026, 11, 5, 12, tzinfo=UTC)
        locked = _period(start)
        expired = PriceSnapshot((), "EUR", "test", tomorrow_available=True)
        self.assertFalse(
            _supports_locked_window(expired, locked, locked.recovery_end)
        )
        gap = PriceSnapshot(
            (PriceSlot(locked.shutdown_start + timedelta(minutes=15), locked.recovery_end, 0.2),),
            "EUR",
            "test",
            tomorrow_available=True,
        )
        self.assertFalse(
            _supports_locked_window(gap, locked, locked.shutdown_start)
        )

    def test_temperature_units_are_normalized_and_invalid_values_fail_closed(self):
        self.assertAlmostEqual(_temperature_to_celsius("68", "°F"), 20)
        self.assertAlmostEqual(_temperature_to_celsius("293.15", "K"), 20)
        self.assertIsNone(_temperature_to_celsius("unknown", "°C"))
        self.assertIsNone(_temperature_to_celsius("20", "percent"))

    def test_dynamic_helpers_convert_to_optimizer_units(self):
        states = SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(
                state={"input_number.cutoff": "3", "input_number.diff": "3.5"}[entity_id]
            )
        )
        hass = SimpleNamespace(states=states)
        options, warnings = _resolve_helper_options(
            hass,
            {
                "max_cutoff_minutes": 300,
                "min_price_diff_per_kwh": 0.03,
                "max_cutoff_entity_id": "input_number.cutoff",
                "max_cutoff_entity_unit": "h",
                "min_price_diff_entity_id": "input_number.diff",
                "min_price_diff_entity_unit": "subunit_per_kwh",
            },
        )
        self.assertEqual(options["max_cutoff_minutes"], 180)
        self.assertEqual(options["min_price_diff_per_kwh"], 0.035)
        self.assertEqual(warnings, ())

    def test_invalid_or_unavailable_helpers_keep_static_values_and_warn(self):
        states = SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(
                state="unavailable" if entity_id.endswith("cutoff") else "not-a-number"
            )
        )
        options, warnings = _resolve_helper_options(
            SimpleNamespace(states=states),
            {
                "max_cutoff_minutes": 300,
                "min_price_diff_per_kwh": 0.03,
                "max_cutoff_entity_id": "sensor.cutoff",
                "max_cutoff_entity_unit": "min",
                "min_price_diff_entity_id": "sensor.diff",
                "min_price_diff_entity_unit": "currency_per_kwh",
            },
        )
        self.assertEqual(options["max_cutoff_minutes"], 300)
        self.assertEqual(options["min_price_diff_per_kwh"], 0.03)
        self.assertEqual(
            warnings,
            ("max_cutoff_helper_unavailable", "min_price_diff_helper_invalid"),
        )

    def test_effective_setting_provenance_distinguishes_helper_and_fixed_values(self):
        states = SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(
                state={"input_number.cutoff": "3", "input_number.diff": "3.5"}[entity_id]
            )
        )
        _, warnings, effective = _resolve_helper_options_with_provenance(
            SimpleNamespace(states=states),
            {
                "max_cutoff_minutes": 300,
                "min_price_diff_per_kwh": 0.03,
                "max_cutoff_entity_id": "input_number.cutoff",
                "max_cutoff_entity_unit": "h",
                "min_price_diff_entity_id": "input_number.diff",
                "min_price_diff_entity_unit": "subunit_per_kwh",
            },
        )
        self.assertEqual(warnings, ())
        self.assertEqual(effective["max_cutoff_minutes"], 180)
        self.assertEqual(effective["max_cutoff_source"], "helper")
        self.assertEqual(effective["min_price_difference_per_kwh"], 0.035)
        self.assertEqual(effective["min_price_difference_source"], "helper")

        _, _, fixed = _resolve_helper_options_with_provenance(
            SimpleNamespace(states=states),
            {"max_cutoff_minutes": 300, "min_price_diff_per_kwh": 0.03},
        )
        self.assertEqual(fixed["max_cutoff_source"], "fixed")
        self.assertEqual(fixed["min_price_difference_source"], "fixed")

    def test_helper_fallback_provenance_explains_static_values_used(self):
        states = SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(state="unavailable")
        )
        _, warnings, effective = _resolve_helper_options_with_provenance(
            SimpleNamespace(states=states),
            {
                "max_cutoff_minutes": 180,
                "min_price_diff_per_kwh": 0.035,
                "max_cutoff_entity_id": "input_number.cutoff",
                "max_cutoff_entity_unit": "h",
                "min_price_diff_entity_id": "input_number.diff",
                "min_price_diff_entity_unit": "subunit_per_kwh",
            },
        )
        self.assertEqual(len(warnings), 2)
        self.assertEqual(effective["max_cutoff_minutes"], 180)
        self.assertEqual(effective["max_cutoff_source"], "fallback")
        self.assertEqual(
            effective["max_cutoff_fallback_reason"], "max_cutoff_helper_unavailable"
        )
        self.assertEqual(effective["min_price_difference_per_kwh"], 0.035)
        self.assertEqual(effective["min_price_difference_source"], "fallback")
        self.assertEqual(
            effective["min_price_difference_fallback_reason"],
            "min_price_diff_helper_unavailable",
        )

    def test_runtime_reports_next_phase_change_and_active_lock(self):
        now = datetime(2026, 11, 5, 12, 40, tzinfo=UTC)
        period = _period(datetime(2026, 11, 5, 12, tzinfo=UTC))
        prices = PriceSnapshot((), "EUR", "test", tomorrow_available=True)
        result = OptimizationResult((period,), 0.1, 1, 1, 6, "EUR")
        runtime = _runtime_snapshot(
            prices,
            result,
            now,
            last_success=now,
            multipliers=None,
            effective_settings={"max_cutoff_minutes": 180},
            locked_period=period,
            locked_effective_settings={"max_cutoff_minutes": 180},
        )
        self.assertEqual(runtime.phase, "shutdown")
        self.assertEqual(runtime.next_change, period.recovery_start)
        self.assertIs(runtime.locked_period, period)
        self.assertEqual(runtime.effective_settings["max_cutoff_minutes"], 180)
        self.assertEqual(runtime.locked_effective_settings["max_cutoff_minutes"], 180)

    def test_locked_period_is_preserved_and_reserves_one_of_k_periods(self):
        start = datetime(2026, 11, 5, 12, tzinfo=UTC)
        locked = _period(start)
        snapshot = PriceSnapshot((), "EUR", "test", tomorrow_available=True)
        config = OptimizerConfig(max_periods=4)
        multiplier = MultiplierResult(1.5, 1.2, "fixed", "configured")
        future_period = _period(locked.recovery_end + timedelta(hours=1))
        future_result = OptimizationResult((future_period,), 0.2, 2, 1, 0, "EUR")
        now = start + timedelta(minutes=40)
        with patch(
            "custom_components.electricity_price_scheduler.coordinator.optimize",
            return_value=future_result,
        ) as optimizer:
            result, kept_lock = _calculate_result(snapshot, config, multiplier, now, locked)
        self.assertIs(kept_lock, locked)
        self.assertEqual(result.periods, (locked, future_period))
        self.assertEqual(optimizer.call_count, 1)
        args, kwargs = optimizer.call_args
        self.assertEqual(args[1].max_periods, 3)
        self.assertEqual(kwargs["not_before"], locked.recovery_end)

    def test_released_period_restores_full_k(self):
        start = datetime(2026, 11, 5, 12, tzinfo=UTC)
        snapshot = PriceSnapshot((), "EUR", "test", tomorrow_available=True)
        config = OptimizerConfig(max_periods=4)
        multiplier = MultiplierResult(1.5, 1.2, "fixed", "configured")
        empty_result = OptimizationResult((), 0, 0, 0, 0, "EUR")
        with patch(
            "custom_components.electricity_price_scheduler.coordinator.optimize",
            return_value=empty_result,
        ) as optimizer:
            result, kept_lock = _calculate_result(
                snapshot, config, multiplier, start, locked=None
            )
        self.assertEqual(result.periods, ())
        self.assertIsNone(kept_lock)
        self.assertEqual(optimizer.call_args.args[1].max_periods, 4)


if __name__ == "__main__":
    unittest.main()
