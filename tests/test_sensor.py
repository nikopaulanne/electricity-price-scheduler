"""Entity value, availability and timestamp serialization tests."""

from datetime import datetime, timezone
import unittest

from custom_components.electricity_price_scheduler.sensor import (
    ScheduleSensor,
    _iso_utc,
    _locked_settings_known,
    _period_attributes,
)
from custom_components.electricity_price_scheduler.coordinator import (
    DataStatus,
    _runtime_snapshot,
)
from custom_components.electricity_price_scheduler.core.models import (
    CutoffPeriod,
    OptimizationResult,
    PriceSnapshot,
)


class SensorValueTests(unittest.TestCase):
    def test_schedule_count_has_no_physical_unit(self):
        self.assertNotEqual(
            getattr(ScheduleSensor, "_attr_native_unit_of_measurement", None),
            "periods",
        )

    def test_timestamps_are_serialized_in_utc(self):
        value = datetime.fromisoformat("2026-11-05T14:00:00+02:00")
        self.assertEqual(_iso_utc(value), "2026-11-05T12:00:00+00:00")

    def test_period_attributes_describe_modeled_savings(self):
        start = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
        period = CutoffPeriod(
            start,
            start.replace(hour=12, minute=30),
            start.replace(hour=13),
            start.replace(hour=13, minute=30),
            0.1,
            10.0,
            1.0,
            0.9,
            0.1,
            0.03,
            0.5,
        )
        attrs = _period_attributes(period)
        self.assertEqual(attrs["shutdown_start"], "2026-11-05T12:30:00+00:00")
        self.assertEqual(attrs["cost_saving_per_kw"], 0.1)
        self.assertFalse(attrs["locked"])
        self.assertIsNone(_period_attributes(None))

    def test_period_attributes_can_mark_a_persisted_lock(self):
        start = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
        period = CutoffPeriod(
            start,
            start.replace(hour=12, minute=30),
            start.replace(hour=13),
            start.replace(hour=13, minute=30),
            0.1,
            10.0,
            1.0,
            0.9,
            0.1,
            0.03,
            0.5,
        )
        self.assertTrue(_period_attributes(period, locked=True)["locked"])

    def test_decision_reason_keeps_active_phase_visible(self):
        now = datetime(2026, 11, 5, 12, 40, tzinfo=timezone.utc)
        start = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
        period = CutoffPeriod(
            start,
            start.replace(hour=12, minute=30),
            start.replace(hour=13),
            start.replace(hour=13, minute=30),
            0.1,
            10.0,
            1.0,
            0.9,
            0.1,
            0.03,
            0.5,
        )
        runtime = _runtime_snapshot(
            PriceSnapshot((), "EUR", "test", tomorrow_available=True),
            OptimizationResult((period,), 0.1, 1, 1, 6, "EUR"),
            now,
            last_success=now,
            multipliers=None,
            locked_period=period,
        )
        self.assertTrue(runtime.locked_period)
        self.assertEqual(runtime.phase, "shutdown")
        self.assertEqual(runtime.decision_reason, "shutdown")
        self.assertFalse(_locked_settings_known(runtime))

    def test_locked_settings_known_is_empty_without_an_active_lock(self):
        now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
        runtime = _runtime_snapshot(
            PriceSnapshot((), "EUR", "test", tomorrow_available=True),
            OptimizationResult((), 0, 0, 0, 0, "EUR"),
            now,
            last_success=now,
            multipliers=None,
        )
        self.assertIsNone(_locked_settings_known(runtime))

    def test_decision_reason_reports_source_failure(self):
        from custom_components.electricity_price_scheduler.coordinator import _unavailable_snapshot

        runtime = _unavailable_snapshot(DataStatus.SOURCE_UNAVAILABLE)
        self.assertEqual(runtime.decision_reason, "source_unavailable")


if __name__ == "__main__":
    unittest.main()
