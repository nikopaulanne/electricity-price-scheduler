"""Contract tests for the HACS ENTSO-e sensor adapter."""

from __future__ import annotations

import unittest
from datetime import date
from zoneinfo import ZoneInfo

from custom_components.electricity_price_scheduler.providers import (
    EntsoeConfig,
    ProviderConfigError,
    ProviderDataError,
    ProviderUnavailableError,
)
from custom_components.electricity_price_scheduler.providers.entsoe import EntsoeProvider


TODAY = date(2026, 11, 5)
OSLO = ZoneInfo("Europe/Oslo")


class FakeState:
    state = "0.12"

    def __init__(self, attributes: dict[str, object]):
        self.attributes = attributes


class FakeStates:
    def __init__(self, state: FakeState | None):
        self._state = state

    def get(self, entity_id: str) -> FakeState | None:
        return self._state if entity_id == "sensor.average_electricity_price" else None


class FakeHass:
    def __init__(self, state: FakeState | None, timezone: str = "Europe/Oslo"):
        self.states = FakeStates(state)
        self.config = type("Config", (), {"time_zone": timezone})()


def _point(timestamp: str, price: object) -> dict[str, object]:
    return {"time": timestamp, "price": price}


class EntsoeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_upstream_timestamped_lists_and_converts_currency_per_mwh(self) -> None:
        state = FakeState(
            {
                "unit_of_measurement": "EUR/MWh",
                "prices_today": [
                    _point("2026-11-05T00:00:00+01:00", 100),
                    _point("2026-11-05T01:00:00+01:00", 250),
                    # Old data can remain in source attributes; only local today is used.
                    _point("2026-11-04T23:00:00+01:00", 900),
                ],
                "prices_tomorrow": [_point("2026-11-06T00:00:00+01:00", 300)],
            }
        )
        snapshot = await EntsoeProvider().async_fetch_snapshot(
            FakeHass(state), EntsoeConfig("sensor.average_electricity_price", "EUR"), today_local=TODAY
        )

        self.assertEqual(snapshot.provider, "entsoe")
        self.assertEqual(snapshot.currency, "EUR")
        self.assertTrue(snapshot.tomorrow_available)
        self.assertIn("entsoe_prices_may_include_vat_or_modifier", snapshot.warnings)
        self.assertEqual(len(snapshot.slots), 12)
        self.assertEqual(snapshot.slots[0].start.isoformat(), "2026-11-04T23:00:00+00:00")
        self.assertAlmostEqual(snapshot.slots[0].price_per_kwh, 0.1)
        self.assertAlmostEqual(snapshot.slots[-1].price_per_kwh, 0.3)

    async def test_preserves_15_minute_period_and_missing_interval_gap(self) -> None:
        state = FakeState(
            {
                "unit_of_measurement": "EUR/kWh",
                "prices_today": [
                    _point("2026-11-05T00:00:00+01:00", 0.1),
                    _point("2026-11-05T00:15:00+01:00", 0.2),
                    # Missing 00:30 and 00:45 stay a gap; no price is invented.
                    _point("2026-11-05T01:00:00+01:00", 0.3),
                ],
                "prices_tomorrow": [],
            }
        )
        snapshot = await EntsoeProvider().async_fetch_snapshot(
            FakeHass(state), EntsoeConfig("sensor.average_electricity_price", "EUR", 15), today_local=TODAY
        )
        self.assertEqual(len(snapshot.slots), 3)
        self.assertNotEqual(snapshot.slots[1].end, snapshot.slots[2].start)
        self.assertFalse(snapshot.tomorrow_available)
        self.assertIn("entsoe_tomorrow_prices_unavailable", snapshot.warnings)

    async def test_no_tomorrow_prices_is_partial_snapshot(self) -> None:
        state = FakeState(
            {
                "unit_of_measurement": "EUR/kWh",
                "prices_today": [
                    _point("2026-11-05T00:00:00+01:00", 0.1),
                    _point("2026-11-05T01:00:00+01:00", 0.2),
                ],
            }
        )
        snapshot = await EntsoeProvider().async_fetch_snapshot(
            FakeHass(state), EntsoeConfig("sensor.average_electricity_price", "EUR"), today_local=TODAY
        )
        self.assertFalse(snapshot.tomorrow_available)

    async def test_currency_conflict_is_rejected(self) -> None:
        state = FakeState(
            {
                "unit_of_measurement": "NOK/kWh",
                "prices_today": [
                    _point("2026-11-05T00:00:00+01:00", 1),
                    _point("2026-11-05T01:00:00+01:00", 2),
                ],
            }
        )
        with self.assertRaises(ProviderConfigError):
            await EntsoeProvider().async_fetch_snapshot(
                FakeHass(state), EntsoeConfig("sensor.average_electricity_price", "EUR"), today_local=TODAY
            )

    async def test_stale_day_list_is_not_accepted_as_today(self) -> None:
        state = FakeState(
            {
                "unit_of_measurement": "EUR/kWh",
                "prices_today": [
                    _point("2026-11-04T00:00:00+01:00", 0.1),
                    _point("2026-11-04T01:00:00+01:00", 0.2),
                ],
            }
        )
        with self.assertRaises(ProviderUnavailableError):
            await EntsoeProvider().async_fetch_snapshot(
                FakeHass(state), EntsoeConfig("sensor.average_electricity_price", "EUR"), today_local=TODAY
            )

    async def test_naive_timestamps_and_malformed_values_are_rejected(self) -> None:
        for rows in (
            [_point("2026-11-05T00:00:00", 0.1), _point("2026-11-05T01:00:00+01:00", 0.2)],
            [_point("2026-11-05T00:00:00+01:00", "bad"), _point("2026-11-05T01:00:00+01:00", 0.2)],
        ):
            state = FakeState({"unit_of_measurement": "EUR/kWh", "prices_today": rows})
            with self.assertRaises(ProviderDataError):
                await EntsoeProvider().async_fetch_snapshot(
                    FakeHass(state), EntsoeConfig("sensor.average_electricity_price", "EUR"), today_local=TODAY
                )

    async def test_oversized_lists_are_rejected_before_expansion(self) -> None:
        rows = [_point("2026-11-05T00:00:00+01:00", 0.1)] * 401
        state = FakeState({"unit_of_measurement": "EUR/kWh", "prices_today": rows})
        with self.assertRaises(ProviderDataError):
            await EntsoeProvider().async_fetch_snapshot(
                FakeHass(state), EntsoeConfig("sensor.average_electricity_price", "EUR"), today_local=TODAY
            )
