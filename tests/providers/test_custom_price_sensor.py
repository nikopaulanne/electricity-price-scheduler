"""Price-sensor contract tests for Nordpool-compatible proxy entities."""

from __future__ import annotations

import unittest
from datetime import date
from custom_components.electricity_price_scheduler.providers import (
    CommunityNordpoolConfig,
    ProviderDataError,
    ProviderUnavailableError,
)
from custom_components.electricity_price_scheduler.providers.community_nordpool import (
    CommunityNordpoolProvider,
)


TODAY = date(2026, 11, 20)
class FakeState:
    def __init__(self, attributes: dict[str, object], state: str = "5.0"):
        self.attributes = attributes
        self.state = state


class FakeStates:
    def __init__(self, states: dict[str, FakeState]):
        self._states = states

    def get(self, entity_id: str):
        return self._states.get(entity_id)


class FakeHass:
    def __init__(self, states: dict[str, FakeState]):
        self.states = FakeStates(states)
        self.config = type("Config", (), {"time_zone": "Europe/Helsinki"})()
        self.data = {}


def _row(start: str, end: str, value: float) -> dict[str, object]:
    return {"start": start, "end": end, "value": value}


class CustomPriceSensorTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_and_direct_sensor_normalize_to_the_same_snapshot(self):
        direct = {
            "currency": "EUR",
            "unit": "kWh",
            "price_in_cents": False,
            "raw_today": [
                _row("2026-11-20T00:00:00+02:00", "2026-11-20T00:15:00+02:00", 0.05),
                _row("2026-11-20T00:15:00+02:00", "2026-11-20T00:30:00+02:00", 0.07),
            ],
            "raw_tomorrow": [
                _row("2026-11-21T00:00:00+02:00", "2026-11-21T00:15:00+02:00", 0.09),
            ],
        }
        proxy = {
            **direct,
            "unit": "c/kWh",
            "price_in_cents": True,
            "raw_today": [
                _row("2026-11-20T00:00:00+02:00", "2026-11-20T00:15:00+02:00", 5),
                _row("2026-11-20T00:15:00+02:00", "2026-11-20T00:30:00+02:00", 7),
            ],
            "raw_tomorrow": [
                _row("2026-11-21T00:00:00+02:00", "2026-11-21T00:15:00+02:00", 9),
            ],
        }
        hass = FakeHass({
            "sensor.direct_nordpool": FakeState(direct, "0.05"),
            "sensor.nordpool_fi": FakeState(proxy, "5"),
        })
        provider = CommunityNordpoolProvider()
        direct_result = await provider.async_fetch_snapshot(
            hass, CommunityNordpoolConfig("sensor.direct_nordpool", "EUR"), today_local=TODAY
        )
        proxy_result = await provider.async_fetch_snapshot(
            hass,
            CommunityNordpoolConfig("sensor.nordpool_fi", "EUR", "snt/kWh"),
            today_local=TODAY,
        )

        self.assertEqual(direct_result.currency, "EUR")
        self.assertEqual(proxy_result.currency, "EUR")
        self.assertTrue(direct_result.tomorrow_available)
        self.assertTrue(proxy_result.tomorrow_available)
        self.assertEqual(len(proxy_result.slots), 3)
        self.assertEqual(
            [(slot.start, slot.end) for slot in direct_result.slots],
            [(slot.start, slot.end) for slot in proxy_result.slots],
        )
        for direct_slot, proxy_slot in zip(direct_result.slots, proxy_result.slots, strict=True):
            self.assertAlmostEqual(direct_slot.price_per_kwh, proxy_slot.price_per_kwh)

    async def test_proxy_can_supply_current_prices_from_retained_tomorrow_attribute(self):
        state = FakeState({
            "currency": "EUR",
            "unit": "c/kWh",
            "price_in_cents": True,
            "raw_today": [
                _row("2026-11-19T23:45:00+02:00", "2026-11-20T00:00:00+02:00", 4),
            ],
            "raw_tomorrow": [
                _row("2026-11-20T00:00:00+02:00", "2026-11-20T00:15:00+02:00", 5),
                _row("2026-11-21T00:00:00+02:00", "2026-11-21T00:15:00+02:00", 7),
            ],
        })
        snapshot = await CommunityNordpoolProvider().async_fetch_snapshot(
            FakeHass({"sensor.nordpool_fi": state}),
            CommunityNordpoolConfig(
                "sensor.nordpool_fi", "EUR", "snt/kWh", custom_price_sensor=True
            ),
            today_local=TODAY,
        )

        self.assertEqual(snapshot.provider, "custom_price_sensor")
        self.assertEqual(len(snapshot.slots), 2)
        self.assertEqual(
            [slot.start.isoformat() for slot in snapshot.slots],
            ["2026-11-19T22:00:00+00:00", "2026-11-20T22:00:00+00:00"],
        )
        self.assertEqual([slot.price_per_kwh for slot in snapshot.slots], [0.05, 0.07])
        self.assertTrue(snapshot.tomorrow_available)

    async def test_custom_sensor_rejects_expired_dates_and_unavailable_state(self):
        config = CommunityNordpoolConfig(
            "sensor.nordpool_fi", "EUR", "snt/kWh", custom_price_sensor=True
        )
        expired = FakeState({
            "currency": "EUR",
            "unit": "c/kWh",
            "price_in_cents": True,
            "raw_today": [
                _row("2026-11-18T23:45:00+02:00", "2026-11-19T00:00:00+02:00", 4),
            ],
            "raw_tomorrow": [
                _row("2026-11-19T00:00:00+02:00", "2026-11-19T00:15:00+02:00", 5),
            ],
        })
        provider = CommunityNordpoolProvider()
        with self.assertRaises(ProviderUnavailableError):
            await provider.async_fetch_snapshot(
                FakeHass({"sensor.nordpool_fi": expired}), config, today_local=TODAY
            )
        unavailable = FakeState({}, "unavailable")
        with self.assertRaises(ProviderUnavailableError):
            await provider.async_fetch_snapshot(
                FakeHass({"sensor.nordpool_fi": unavailable}), config, today_local=TODAY
            )

    async def test_missing_proxy_unit_metadata_requires_explicit_unit(self):
        state = FakeState({
            "currency": "EUR",
            "raw_today": [
                _row("2026-11-20T00:00:00+02:00", "2026-11-20T00:15:00+02:00", 5),
            ],
            "raw_tomorrow": [],
        })
        hass = FakeHass({"sensor.nordpool_fi": state})
        provider = CommunityNordpoolProvider()
        with self.assertRaises(ProviderDataError) as raised:
            await provider.async_fetch_snapshot(
                hass, CommunityNordpoolConfig("sensor.nordpool_fi", "EUR"), today_local=TODAY
            )
        self.assertIn("unit", str(raised.exception).lower())

        result = await provider.async_fetch_snapshot(
            hass,
            CommunityNordpoolConfig("sensor.nordpool_fi", "EUR", "snt/kWh"),
            today_local=TODAY,
        )
        self.assertAlmostEqual(result.slots[0].price_per_kwh, 0.05)

    async def test_proxy_supports_non_eur_currency_with_explicit_unit_metadata(self):
        state = FakeState({
            "currency": "USD",
            "unit": "kWh",
            "price_in_cents": False,
            "raw_today": [
                _row("2026-11-20T00:00:00+02:00", "2026-11-20T00:15:00+02:00", 5),
            ],
            "raw_tomorrow": [],
        })
        snapshot = await CommunityNordpoolProvider().async_fetch_snapshot(
            FakeHass({"sensor.proxy": state}),
            CommunityNordpoolConfig(
                "sensor.proxy", "USD", custom_price_sensor=True
            ),
            today_local=TODAY,
        )

        self.assertEqual(snapshot.currency, "USD")
        self.assertAlmostEqual(snapshot.slots[0].price_per_kwh, 5.0)
