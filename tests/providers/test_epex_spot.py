"""Check the EPEX Spot HA sensor contract shared by its upstream price sources."""

from datetime import date
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from custom_components.electricity_price_scheduler.providers import (
    EpexSpotConfig,
    ProviderConfigError,
    async_fetch_snapshot,
)

TODAY = date(2026, 11, 20)


def source(unit):
    return SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Berlin"),
        states={"sensor.epex_price": SimpleNamespace(state="-0.048", attributes={
            "unit_of_measurement": unit,
            "data": [
                {
                    "start_time": "2026-11-20T01:00:00+01:00",
                    "end_time": "2026-11-20T02:00:00+01:00",
                    "price_per_kwh": -0.048,
                },
                {
                    "start_time": "2026-11-21T01:00:00+01:00",
                    "end_time": "2026-11-21T01:15:00+01:00",
                    "price_per_kwh": 0.1,
                },
            ],
        })},
    )


class EpexSpotTests(IsolatedAsyncioTestCase):
    async def test_upstream_formats_for_eur_and_gbp(self):
        for unit, currency in (("€/kWh", "EUR"), ("£/kWh", "GBP")):
            with self.subTest(unit=unit):
                snapshot = await async_fetch_snapshot(
                    source(unit), EpexSpotConfig("sensor.epex_price", currency),
                    today_local=TODAY,
                )
                self.assertEqual(snapshot.currency, currency)
                self.assertEqual(len(snapshot.slots), 5)
                self.assertAlmostEqual(snapshot.slots[0].price_per_kwh, -0.048)
                self.assertTrue(snapshot.tomorrow_available)
                self.assertLess(snapshot.slots[-2].end, snapshot.slots[-1].start)

    async def test_rejects_source_currency_change(self):
        with self.assertRaises(ProviderConfigError):
            await async_fetch_snapshot(
                source("£/kWh"), EpexSpotConfig("sensor.epex_price", "EUR"),
                today_local=TODAY,
            )
