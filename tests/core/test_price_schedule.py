from datetime import datetime, time, timedelta, timezone
import unittest

from custom_components.electricity_price_scheduler.core.models import PriceSlot
from custom_components.electricity_price_scheduler.core.price_schedule import (
    PriceScheduleConfig,
    PriceScheduleError,
    plan_price_schedule,
)

UTC = timezone.utc
BASE = datetime(2026, 1, 5, tzinfo=UTC)


def slots(prices, start=BASE, minutes=60):
    return tuple(
        PriceSlot(start + timedelta(minutes=minutes * i), start + timedelta(minutes=minutes * (i + 1)), price)
        for i, price in enumerate(prices)
    )


class PriceScheduleTests(unittest.TestCase):
    def test_threshold_respects_zero_price_and_reports_current_slot(self):
        data = slots([0, 0.1])
        result = plan_price_schedule(
            data,
            BASE + timedelta(minutes=10),
            PriceScheduleConfig(mode="threshold", threshold_price_per_kwh=0),
            "UTC",
        )
        self.assertTrue(result.active)
        self.assertEqual(result.status, "active")
        self.assertEqual(result.selected_slots, data[:1])
        self.assertEqual(result.next_transition, BASE + timedelta(hours=1))


    def test_cheapest_contiguous_selects_lowest_average_and_tie_breaks_earlier(self):
        data = slots([5, 1, 2, 1, 2])
        result = plan_price_schedule(
            data,
            BASE - timedelta(minutes=1),
            PriceScheduleConfig(mode="cheapest_contiguous", required_duration_minutes=120),
            "UTC",
        )
        self.assertEqual(result.selected_slots, data[1:3])


    def test_split_respects_minimum_run_and_price_ceiling(self):
        data = slots([1, 10, 1, 1, 8, 1])
        result = plan_price_schedule(
            data,
            BASE - timedelta(minutes=1),
            PriceScheduleConfig(
                mode="cheapest_split",
                required_duration_minutes=180,
                min_run_minutes=120,
                price_ceiling_per_kwh=2,
            ),
            "UTC",
        )
        # Every selected run is at least two hours, so this input has no solution.
        self.assertEqual(result.status, "price_ceiling")
        self.assertFalse(result.selected_slots)

    def test_split_picks_separate_minimum_length_runs(self):
        data = slots([1, 1, 10, 10, 1, 1, 9, 9])
        result = plan_price_schedule(
            data,
            BASE - timedelta(minutes=1),
            PriceScheduleConfig(
                mode="cheapest_split",
                required_duration_minutes=240,
                min_run_minutes=120,
            ),
            "UTC",
        )
        self.assertEqual(result.selected_slots, data[:2] + data[4:6])


    def test_deadline_priority_relaxes_ceiling_and_marks_warning(self):
        data = slots([10, 1, 1])
        result = plan_price_schedule(
            data,
            BASE - timedelta(minutes=1),
            PriceScheduleConfig(
                mode="cheapest_contiguous",
                required_duration_minutes=60,
                price_ceiling_per_kwh=2,
                deadline=BASE + timedelta(hours=1),
                fallback="deadline",
            ),
            "UTC",
        )
        self.assertEqual(result.selected_slots, data[:1])
        self.assertTrue(result.warnings)


    def test_local_overnight_window_includes_correct_start_day(self):
        # Helsinki is UTC+2 on this January date. Monday's 22:00–02:00 window is
        # Monday 20:00 through Tuesday 00:00 UTC.
        data = slots([4, 1, 1, 2, 3], start=datetime(2026, 1, 5, 20, tzinfo=UTC))
        result = plan_price_schedule(
            data,
            datetime(2026, 1, 5, 20, tzinfo=UTC),
            PriceScheduleConfig(
                mode="cheapest_contiguous",
                required_duration_minutes=120,
                window_start=time(22),
                window_end=time(2),
                weekdays=(0,),
            ),
            "Europe/Helsinki",
        )
        self.assertEqual(result.selected_slots, data[1:3])

    def test_threshold_window_can_begin_inside_a_price_slot(self):
        start = datetime(2026, 1, 5, 6, tzinfo=UTC)
        data = slots([0], start=start, minutes=15)
        before = plan_price_schedule(
            data,
            start + timedelta(minutes=4),
            PriceScheduleConfig(
                mode="threshold",
                threshold_price_per_kwh=0,
                window_start="06:05",
                window_end="07:00",
            ),
            "UTC",
        )
        at_start = plan_price_schedule(
            data,
            start + timedelta(minutes=5),
            PriceScheduleConfig(
                mode="threshold",
                threshold_price_per_kwh=0,
                window_start="06:05",
                window_end="07:00",
            ),
            "UTC",
        )
        self.assertFalse(before.active)
        self.assertTrue(at_start.active)

    def test_waits_for_complete_daily_window_price_coverage(self):
        start = datetime(2026, 1, 5, 20, tzinfo=UTC)
        data = slots([1, 2], start=start)
        result = plan_price_schedule(
            data,
            start - timedelta(minutes=1),
            PriceScheduleConfig(
                mode="cheapest_contiguous",
                required_duration_minutes=60,
                window_start="22:00",
                window_end="02:00",
            ),
            "Europe/Helsinki",
        )
        self.assertEqual(result.status, "waiting_for_prices")
        self.assertEqual(result.occurrence_end, datetime(2026, 1, 6, 0, tzinfo=UTC))

    def test_weekday_filter_applies_without_daily_window(self):
        data = slots([0, 0], start=datetime(2026, 1, 5, 23, tzinfo=UTC))
        result = plan_price_schedule(
            data,
            data[0].start,
            PriceScheduleConfig(mode="threshold", threshold_price_per_kwh=0, weekdays=(1,)),
            "UTC",
        )
        self.assertFalse(result.active)
        self.assertEqual(result.selected_slots, data[1:])


    def test_dst_window_uses_local_midnight_boundaries(self):
        # On the spring transition date, Helsinki's 01:00–05:00 local window is
        # three elapsed UTC hours and contains the three UTC slots below.
        start = datetime(2026, 3, 28, 23, tzinfo=UTC)
        data = slots([1, 2, 1, 3], start=start)
        result = plan_price_schedule(
            data,
            start - timedelta(minutes=1),
            PriceScheduleConfig(
                mode="cheapest_contiguous",
                required_duration_minutes=180,
                window_start="01:00",
                window_end="05:00",
            ),
            "Europe/Helsinki",
        )
        self.assertEqual(result.selected_slots, data[:3])


    def test_rejects_more_than_200_slots(self):
        data = slots([1] * 201, minutes=15)
        with self.assertRaisesRegex(PriceScheduleError, "200"):
            plan_price_schedule(
                data,
                BASE,
                PriceScheduleConfig(mode="threshold", threshold_price_per_kwh=2),
                "UTC",
            )
