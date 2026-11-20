"""Contract tests for BottlecapDave Octopus Energy rates event adapter."""

from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from custom_components.electricity_price_scheduler.providers import (
    OctopusEnergyConfig,
    ProviderConfigError,
    ProviderDataError,
    ProviderUnavailableError,
)
from custom_components.electricity_price_scheduler.providers.octopus import (
    OctopusEnergyProvider,
    next_day_entity_id,
)


TODAY = date(2026, 11, 5)
OSLO = ZoneInfo("Europe/Oslo")
CURRENT = "event.octopus_energy_electricity_123456_987654_current_day_rates"
NEXT = "event.octopus_energy_electricity_123456_987654_next_day_rates"


class FakeState:
    state = "2026-11-05T15:00:00+00:00"

    def __init__(self, rates: object):
        self.attributes = {"rates": rates}


class FakeStates:
    def __init__(self, entities: dict[str, FakeState]):
        self.entities = entities

    def get(self, entity_id: str) -> FakeState | None:
        return self.entities.get(entity_id)


class FakeHass:
    def __init__(self, entities: dict[str, FakeState], time_zone: str = "Europe/Oslo"):
        self.states = FakeStates(entities)
        self.config = type("Config", (), {"time_zone": time_zone})()


def _rate(start: str, end: str, value: object = 0.2) -> dict[str, object]:
    return {"start": start, "end": end, "value_inc_vat": value}


class OctopusEnergyProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_today_and_sibling_tomorrow_in_gbp_per_kwh(self) -> None:
        entities = {
            CURRENT: FakeState(
                [
                    _rate("2026-11-05T00:00:00+01:00", "2026-11-05T00:30:00+01:00", 0.31),
                    # An older interval on the current-day event is ignored.
                    _rate("2026-11-04T23:30:00+01:00", "2026-11-05T00:00:00+01:00", 0.9),
                ]
            ),
            NEXT: FakeState(
                [_rate("2026-11-06T00:00:00+01:00", "2026-11-06T00:30:00+01:00", 0.12)]
            ),
        }
        snapshot = await OctopusEnergyProvider().async_fetch_snapshot(
            FakeHass(entities), OctopusEnergyConfig(CURRENT), today_local=TODAY
        )

        self.assertEqual(snapshot.provider, "octopus_energy")
        self.assertEqual(snapshot.currency, "GBP")
        self.assertTrue(snapshot.tomorrow_available)
        self.assertEqual(len(snapshot.slots), 4)  # 30 minute rates normalize to 15 minute slots.
        self.assertEqual(snapshot.slots[0].start.isoformat(), "2026-11-04T23:00:00+00:00")
        self.assertAlmostEqual(snapshot.slots[0].price_per_kwh, 0.31)
        self.assertAlmostEqual(snapshot.slots[-1].price_per_kwh, 0.12)

    async def test_missing_or_unpublished_tomorrow_is_a_partial_snapshot(self) -> None:
        entities = {
            CURRENT: FakeState(
                [_rate("2026-11-05T00:00:00+01:00", "2026-11-05T00:30:00+01:00")]
            )
        }
        snapshot = await OctopusEnergyProvider().async_fetch_snapshot(
            FakeHass(entities), OctopusEnergyConfig(CURRENT), today_local=TODAY
        )
        self.assertFalse(snapshot.tomorrow_available)
        self.assertIn("octopus_energy_tomorrow_prices_unavailable", snapshot.warnings)
        self.assertEqual(len(snapshot.slots), 2)

    async def test_stale_current_event_is_not_mistaken_for_today(self) -> None:
        entities = {
            CURRENT: FakeState(
                [_rate("2026-11-04T12:00:00+01:00", "2026-11-04T12:30:00+01:00")]
            )
        }
        with self.assertRaises(ProviderUnavailableError):
            await OctopusEnergyProvider().async_fetch_snapshot(
                FakeHass(entities), OctopusEnergyConfig(CURRENT), today_local=TODAY
            )

    async def test_invalid_rate_value_is_rejected(self) -> None:
        entities = {
            CURRENT: FakeState(
                [_rate("2026-11-05T00:00:00+01:00", "2026-11-05T00:30:00+01:00", "not-a-price")]
            )
        }
        with self.assertRaises(ProviderDataError):
            await OctopusEnergyProvider().async_fetch_snapshot(
                FakeHass(entities), OctopusEnergyConfig(CURRENT), today_local=TODAY
            )

    async def test_conflicting_duplicate_intervals_are_rejected(self) -> None:
        interval = _rate("2026-11-05T00:00:00+01:00", "2026-11-05T00:30:00+01:00", 0.2)
        entities = {CURRENT: FakeState([interval, {**interval, "value_inc_vat": 0.3}])}
        with self.assertRaises(ProviderDataError):
            await OctopusEnergyProvider().async_fetch_snapshot(
                FakeHass(entities), OctopusEnergyConfig(CURRENT), today_local=TODAY
            )

    async def test_naive_timestamps_and_oversized_data_are_rejected(self) -> None:
        entities = {
            CURRENT: FakeState([_rate("2026-11-05T00:00:00", "2026-11-05T00:30:00")])
        }
        with self.assertRaises(ProviderDataError):
            await OctopusEnergyProvider().async_fetch_snapshot(
                FakeHass(entities), OctopusEnergyConfig(CURRENT), today_local=TODAY
            )

        oversized = [
            _rate("2026-11-05T00:00:00+01:00", "2026-11-05T00:30:00+01:00")
            for _ in range(801)
        ]
        with self.assertRaises(ProviderDataError):
            await OctopusEnergyProvider().async_fetch_snapshot(
                FakeHass({CURRENT: FakeState(oversized)}),
                OctopusEnergyConfig(CURRENT),
                today_local=TODAY,
            )

    async def test_next_entity_id_rejects_noncanonical_or_export_events(self) -> None:
        self.assertEqual(next_day_entity_id(CURRENT), NEXT)
        for invalid in (
            "sensor.octopus_energy_electricity_123_456_current_day_rates",
            "event.octopus_energy_electricity_123_456_export_current_day_rates",
            "event.octopus_energy_electricity_123_456_next_day_rates",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ProviderConfigError):
                next_day_entity_id(invalid)

    async def test_only_gbp_is_accepted(self) -> None:
        with self.assertRaises(ProviderConfigError):
            await OctopusEnergyProvider().async_fetch_snapshot(
                FakeHass({}), OctopusEnergyConfig(CURRENT, "EUR"), today_local=TODAY
            )


if __name__ == "__main__":
    unittest.main()
