"""Contract tests for the community Nordpool sensor adapter (no HA install)."""

from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from custom_components.electricity_price_scheduler.providers import (
    CommunityNordpoolConfig,
    ProviderConfigError,
    ProviderDataError,
    ProviderUnavailableError,
    async_fetch_snapshot,
)
from custom_components.electricity_price_scheduler.providers.community_nordpool import (
    CommunityNordpoolProvider,
)


TODAY = date(2026, 11, 5)
OSLO = ZoneInfo("Europe/Oslo")


class FakeState:
    state = "0.01"

    def __init__(self, attributes: dict[str, object]):
        self.attributes = attributes


class FakeStates:
    def __init__(self, state: FakeState | None):
        self._state = state

    def get(self, entity_id: str) -> FakeState | None:
        return self._state if entity_id == "sensor.nordpool_no4" else None


class FakeHass:
    def __init__(self, state: FakeState | None, time_zone: str = "Europe/Oslo"):
        self.states = FakeStates(state)
        self.config = type("Config", (), {"time_zone": time_zone})()


def _row(start: datetime | str, end: datetime | str, value: object = 1000) -> dict[str, object]:
    return {"start": start, "end": end, "value": value}


class CommunityNordpoolProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalizes_hacs_cents_per_mwh_and_keeps_utc_instants(self) -> None:
        attrs = {
            "currency": "NOK",
            "unit": "MWh",
            "price_in_cents": True,
            "raw_today": [
                _row(
                    datetime(2026, 11, 5, 0, 0, tzinfo=OSLO),
                    datetime(2026, 11, 5, 0, 15, tzinfo=OSLO),
                    1000,
                ),
                # +01:00 is a valid Norwegian winter offset, not a suffix to strip.
                _row(
                    "2026-11-05T00:15:00+01:00",
                    "2026-11-05T00:30:00+01:00",
                    2000,
                ),
                # Stale row in raw_today must not enter today's snapshot.
                _row(
                    "2026-11-04T12:00:00+01:00",
                    "2026-11-04T12:15:00+01:00",
                    3000,
                ),
            ],
            "raw_tomorrow": [
                _row(
                    "2026-11-06T00:00:00+01:00",
                    "2026-11-06T00:15:00+01:00",
                    4000,
                )
            ],
        }
        snapshot = await CommunityNordpoolProvider().async_fetch_snapshot(
            FakeHass(FakeState(attrs)),
            CommunityNordpoolConfig("sensor.nordpool_no4", "NOK"),
            today_local=TODAY,
        )

        self.assertEqual(snapshot.currency, "NOK")
        self.assertEqual(snapshot.provider, "community_nordpool")
        self.assertTrue(snapshot.tomorrow_available)
        self.assertIn("community_prices_may_include_vat_or_additional_costs", snapshot.warnings)
        self.assertEqual(len(snapshot.slots), 3)
        self.assertEqual(snapshot.slots[0].start.isoformat(), "2026-11-04T23:00:00+00:00")
        self.assertAlmostEqual(snapshot.slots[0].price_per_kwh, 0.01)
        self.assertAlmostEqual(snapshot.slots[1].price_per_kwh, 0.02)
        self.assertEqual(snapshot.slots[2].start.isoformat(), "2026-11-05T23:00:00+00:00")
        self.assertAlmostEqual(snapshot.slots[2].price_per_kwh, 0.04)

    async def test_stale_raw_today_is_not_accepted_as_current_prices(self) -> None:
        state = FakeState(
            {
                "currency": "EUR",
                "unit": "kWh",
                "price_in_cents": False,
                "raw_today": [
                    _row("2026-11-04T10:00:00+01:00", "2026-11-04T10:15:00+01:00", 0.2)
                ],
                "raw_tomorrow": [],
            }
        )
        with self.assertRaises(ProviderUnavailableError):
            await CommunityNordpoolProvider().async_fetch_snapshot(
                FakeHass(state),
                CommunityNordpoolConfig("sensor.nordpool_no4", "EUR"),
                today_local=TODAY,
            )

    async def test_missing_tomorrow_is_partial_snapshot_with_warning(self) -> None:
        state = FakeState(
            {
                "currency": "EUR",
                "unit": "kWh",
                "price_in_cents": False,
                "raw_today": [
                    _row("2026-11-05T00:00:00+01:00", "2026-11-05T00:15:00+01:00", 0.2)
                ],
                "raw_tomorrow": None,
            }
        )
        snapshot = await CommunityNordpoolProvider().async_fetch_snapshot(
            FakeHass(state),
            CommunityNordpoolConfig("sensor.nordpool_no4", "EUR"),
            today_local=TODAY,
        )
        self.assertFalse(snapshot.tomorrow_available)
        self.assertIn("community_tomorrow_prices_unavailable", snapshot.warnings)
        self.assertEqual(len(snapshot.slots), 1)

    async def test_currency_mismatch_is_rejected_without_conversion(self) -> None:
        state = FakeState(
            {
                "currency": "NOK",
                "unit": "kWh",
                "price_in_cents": False,
                "raw_today": [_row("2026-11-05T00:00:00+01:00", "2026-11-05T00:15:00+01:00")],
                "raw_tomorrow": [],
            }
        )
        with self.assertRaises(ProviderConfigError):
            await CommunityNordpoolProvider().async_fetch_snapshot(
                FakeHass(state),
                CommunityNordpoolConfig("sensor.nordpool_no4", "EUR"),
                today_local=TODAY,
            )

    async def test_ambiguous_unit_requires_explicit_override(self) -> None:
        state = FakeState(
            {
                "currency": "NOK",
                "unit": "unknown",
                "price_in_cents": False,
                "raw_today": [_row("2026-11-05T00:00:00+01:00", "2026-11-05T00:15:00+01:00")],
                "raw_tomorrow": [],
            }
        )
        with self.assertRaises(ProviderDataError):
            await CommunityNordpoolProvider().async_fetch_snapshot(
                FakeHass(state),
                CommunityNordpoolConfig("sensor.nordpool_no4", "NOK"),
                today_local=TODAY,
            )

        snapshot = await CommunityNordpoolProvider().async_fetch_snapshot(
            FakeHass(state),
            CommunityNordpoolConfig("sensor.nordpool_no4", "NOK", "NOK/kWh"),
            today_local=TODAY,
        )
        self.assertEqual(snapshot.slots[0].price_per_kwh, 1000.0)

    async def test_invalid_current_timestamp_is_rejected(self) -> None:
        state = FakeState(
            {
                "currency": "EUR",
                "unit": "kWh",
                "price_in_cents": False,
                "raw_today": [_row("2026-11-05T00:00:00", "2026-11-05T00:15:00", 0.2)],
                "raw_tomorrow": [],
            }
        )
        with self.assertRaises(ProviderDataError):
            await CommunityNordpoolProvider().async_fetch_snapshot(
                FakeHass(state),
                CommunityNordpoolConfig("sensor.nordpool_no4", "EUR"),
                today_local=TODAY,
            )

    async def test_too_many_raw_intervals_are_rejected_before_normalization(self) -> None:
        rows = [
            _row("2026-11-05T00:00:00+01:00", "2026-11-05T00:15:00+01:00")
            for _ in range(401)
        ]
        state = FakeState(
            {"currency": "EUR", "unit": "kWh", "price_in_cents": False, "raw_today": rows}
        )
        with self.assertRaises(ProviderDataError):
            await CommunityNordpoolProvider().async_fetch_snapshot(
                FakeHass(state),
                CommunityNordpoolConfig("sensor.nordpool_no4", "EUR"),
                today_local=TODAY,
            )

    async def test_facade_dispatches_community_provider(self) -> None:
        state = FakeState(
            {
                "currency": "EUR",
                "unit": "kWh",
                "price_in_cents": False,
                "raw_today": [_row("2026-11-05T00:00:00+01:00", "2026-11-05T00:15:00+01:00", 0.2)],
                "raw_tomorrow": [],
            }
        )
        snapshot = await async_fetch_snapshot(
            FakeHass(state),
            CommunityNordpoolConfig("sensor.nordpool_no4", "EUR"),
            today_local=TODAY,
        )
        self.assertEqual(snapshot.provider, "community_nordpool")


if __name__ == "__main__":
    unittest.main()
