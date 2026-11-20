"""Price scenario runtime configuration and occurrence-lock tests."""

from datetime import datetime, time, timedelta, timezone
import unittest

from custom_components.electricity_price_scheduler.core.models import PriceSlot
from custom_components.electricity_price_scheduler.core.price_schedule import (
    PriceScheduleConfig,
    PriceScheduleResult,
)
from custom_components.electricity_price_scheduler.price_runtime import (
    _group_runs,
    _locked_result,
    _occurrence_end,
    _supports_latched_coverage,
    build_price_decision_attributes,
    parse_price_schedule_config,
)

UTC = timezone.utc


def _slot(start: datetime, minutes: int = 15, price: float = 0.1) -> PriceSlot:
    return PriceSlot(start, start + timedelta(minutes=minutes), price)


class PriceRuntimeContractTests(unittest.TestCase):
    def test_threshold_scenario_uses_direct_limit_mode(self):
        config = parse_price_schedule_config(
            "threshold", {"threshold_price_per_kwh": 0.12, "window_start": "06:00:00", "window_end": "22:00:00"}
        )
        self.assertEqual(config.mode, "threshold")
        self.assertEqual(config.threshold_price_per_kwh, 0.12)

    def test_schedule_scenario_keeps_contiguous_or_split_setting(self):
        config = parse_price_schedule_config(
            "schedule",
            {
                "mode": "cheapest_split",
                "required_duration_minutes": 90,
                "min_run_minutes": 30,
                "weekdays": [0, 4],
            },
        )
        self.assertEqual(config.mode, "cheapest_split")
        self.assertEqual(config.required_duration_minutes, 90)
        self.assertEqual(config.weekdays, (0, 4))

    def test_split_blocks_remain_one_occurrence_during_gaps(self):
        start = datetime(2026, 11, 19, 8, tzinfo=UTC)
        selected = (_slot(start), _slot(start + timedelta(hours=2)))
        result = _locked_result(selected, start + timedelta(minutes=15))
        self.assertFalse(result.active)
        self.assertEqual(result.status, "scheduled")
        self.assertEqual(result.selected_slots, selected)
        self.assertTrue(_supports_latched_coverage(
            selected, selected, start + timedelta(minutes=15), start + timedelta(hours=4)
        ))

    def test_occurrence_lock_runs_until_window_end_after_last_selected_slot(self):
        # Helsinki's 22:00–07:00 window crosses midnight; the selected slot is
        # after midnight but belongs to the previous local day.
        selected = (_slot(datetime(2026, 11, 20, 4, tzinfo=UTC)),)
        config = PriceScheduleConfig(
            mode="cheapest_contiguous",
            required_duration_minutes=15,
            window_start=time(22),
            window_end=time(7),
        )
        end = _occurrence_end(selected, config, "Europe/Helsinki")
        self.assertEqual(end, datetime(2026, 11, 20, 5, tzinfo=UTC))
        self.assertTrue(_supports_latched_coverage((), selected, selected[0].end, end))
        self.assertFalse(_supports_latched_coverage((), selected, end, end))

    def test_contiguous_selected_slots_form_one_run(self):
        start = datetime(2026, 11, 19, 10, tzinfo=UTC)
        groups = _group_runs((_slot(start), _slot(start + timedelta(minutes=15)), _slot(start + timedelta(hours=1))))
        self.assertEqual(tuple(map(len, groups)), (2, 1))

    def test_decision_attributes_explain_threshold_price_and_rules(self):
        config = parse_price_schedule_config(
            "threshold",
            {"threshold_price_per_kwh": 0.15, "window_start": "06:00", "window_end": "22:00", "weekdays": [0, 2]},
        )
        now = datetime(2026, 11, 19, 10, tzinfo=UTC)
        attrs = build_price_decision_attributes(
            config, None, status="above_threshold", active=False,
            reason_code="above_threshold",
            current_price=0.2, currency="EUR", now=now,
        )
        self.assertEqual(attrs["threshold_comparison"], "above")
        self.assertEqual(attrs["threshold_price_per_kwh"], 0.15)
        self.assertEqual(attrs["current_price"], 0.2)
        self.assertEqual(attrs["window_start"], "06:00")
        self.assertEqual(attrs["weekdays"], [0, 2])
        self.assertFalse(attrs["active"])

    def test_decision_attributes_group_split_slots_into_periods(self):
        start = datetime(2026, 11, 19, 8, tzinfo=UTC)
        config = parse_price_schedule_config(
            "schedule", {"mode": "cheapest_split", "required_duration_minutes": 30, "min_run_minutes": 15}
        )
        slots = (_slot(start), _slot(start + timedelta(minutes=15)), _slot(start + timedelta(hours=1)))
        result = PriceScheduleResult(False, "scheduled", "cheapest_intervals_selected", slots)
        attrs = build_price_decision_attributes(
            config, result, status=result.status, active=result.active,
            now=start - timedelta(minutes=5), currency="EUR",
        )
        self.assertEqual(attrs["selected_run_count"], 2)
        self.assertEqual([item["duration_minutes"] for item in attrs["selected_periods"]], [30, 15])
        self.assertEqual(attrs["next_period_start"], start.isoformat())

    def test_decision_attributes_keep_fault_distinct_from_off_decision(self):
        attrs = build_price_decision_attributes(
            None, None, status="source_unavailable", active=None,
            reason_code="source_unavailable",
            error_code="source_timeout", now=datetime(2026, 11, 19, 8, tzinfo=UTC),
        )
        self.assertIsNone(attrs["active"])
        self.assertEqual(attrs["status"], "source_unavailable")
        self.assertEqual(attrs["error_code"], "source_timeout")
        self.assertEqual(attrs["reason_code"], "source_unavailable")


if __name__ == "__main__":
    unittest.main()
