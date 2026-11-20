from datetime import datetime, timedelta, timezone
import unittest

from custom_components.electricity_price_scheduler.core.normalization import (
    PriceDataError,
    normalize_slots,
)


class NormalizeSlotsTests(unittest.TestCase):
    def raw(self, start, end, value):
        return {"start": start, "end": end, "value": value}

    def test_expands_hourly_utc_and_converts_nok_per_mwh(self):
        slots = normalize_slots(
            [self.raw("2026-01-01T00:00:00+01:00", "2026-01-01T01:00:00+01:00", 100)],
            source_unit="NOK/MWh",
            currency="nok",
        )
        self.assertEqual(len(slots), 4)
        self.assertEqual(slots[0].start.isoformat(), "2025-12-31T23:00:00+00:00")
        self.assertEqual(slots[-1].end.isoformat(), "2026-01-01T00:00:00+00:00")
        self.assertEqual([s.price_per_kwh for s in slots], [0.1] * 4)

    def test_supports_datetime_objects_and_subunit_aliases(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        cases = (
            ("EUR/Wh", "EUR", 0.001, 1.0),
            ("cent/Wh", "EUR", 0.1, 1.0),
            ("øre/MWh", "NOK", 100.0, 0.001),
            ("snt/kWh", "EUR", 5.0, 0.05),
        )
        for unit, currency, price, expected in cases:
            with self.subTest(unit=unit):
                slots = normalize_slots(
                    [self.raw(start, start + timedelta(minutes=15), price)],
                    source_unit=unit,
                    currency=currency,
                )
                self.assertAlmostEqual(slots[0].price_per_kwh, expected)

    def test_keeps_gaps_and_non_quarter_durations_without_inventing_data(self):
        slots = normalize_slots(
            [
                self.raw("2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z", 1),
                self.raw("2026-01-01T00:20:00Z", "2026-01-01T00:35:00Z", 2),
            ],
            source_unit="currency/kWh",
            currency="EUR",
        )
        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0].end, datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc))
        self.assertEqual(slots[1].start, datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc))

    def test_identical_duplicates_are_removed_but_conflicts_rejected(self):
        raw = self.raw("2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z", 10)
        self.assertEqual(len(normalize_slots([raw, raw], source_unit="snt/kWh", currency="EUR")), 1)
        with self.assertRaisesRegex(PriceDataError, "conflicting duplicate"):
            normalize_slots(
                [raw, dict(raw, value=11)], source_unit="snt/kWh", currency="EUR"
            )

    def test_rejects_naive_timestamps_bad_values_and_currency_conflicts(self):
        with self.assertRaisesRegex(PriceDataError, "UTC offset"):
            normalize_slots(
                [self.raw("2026-01-01T00:00:00", "2026-01-01T00:15:00Z", 1)],
                source_unit="EUR/kWh",
                currency="EUR",
            )
        with self.assertRaisesRegex(PriceDataError, "finite"):
            normalize_slots(
                [self.raw("2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z", float("nan"))],
                source_unit="EUR/kWh",
                currency="EUR",
            )
        with self.assertRaisesRegex(PriceDataError, "conflicts with currency"):
            normalize_slots(
                [self.raw("2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z", 1)],
                source_unit="NOK/MWh",
                currency="EUR",
            )

    def test_preallocation_guard_rejects_huge_interval(self):
        with self.assertRaisesRegex(PriceDataError, "normalized slot count"):
            normalize_slots(
                [self.raw("2000-01-01T00:00:00Z", "2030-01-01T00:00:00Z", 1)],
                source_unit="EUR/kWh",
                currency="EUR",
            )

    def test_rejects_overlaps_and_unknown_units(self):
        with self.assertRaisesRegex(PriceDataError, "overlapping"):
            normalize_slots(
                [
                    self.raw("2026-01-01T00:00:00Z", "2026-01-01T00:20:00Z", 1),
                    self.raw("2026-01-01T00:15:00Z", "2026-01-01T00:45:00Z", 2),
                ],
                source_unit="EUR/kWh",
                currency="EUR",
            )
        with self.assertRaisesRegex(PriceDataError, "unsupported source unit"):
            normalize_slots([], source_unit="magic", currency="EUR")


if __name__ == "__main__":
    unittest.main()
