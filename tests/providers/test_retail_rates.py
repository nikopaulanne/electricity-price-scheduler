"""Test retailer payloads with stand-ins for HA's public price actions."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from custom_components.electricity_price_scheduler.providers import (
    AmberConfig,
    ProviderUnavailableError,
    TibberConfig,
    async_fetch_snapshot,
)
from custom_components.electricity_price_scheduler.providers.tibber import TibberHomeRequired

UTC = timezone.utc
TODAY = datetime(2026, 11, 20, tzinfo=UTC)


class RetailRateTests(IsolatedAsyncioTestCase):
    async def test_tibber_gap_is_preserved_and_prices_are_not_taxed_twice(self):
        action = AsyncMock(return_value={"prices": {"Home": [
            {"start_time": (TODAY + timedelta(minutes=offset)).isoformat(), "price": 0.25}
            for offset in (0, 15, 45, 60)
        ]}})
        hass = SimpleNamespace(config=SimpleNamespace(time_zone="UTC"), services=SimpleNamespace(async_call=action))
        snapshot = await async_fetch_snapshot(hass, TibberConfig("EUR"), today_local=TODAY.date())
        self.assertEqual(len(snapshot.slots), 4)
        self.assertEqual(snapshot.slots[1].end + timedelta(minutes=15), snapshot.slots[2].start)
        self.assertEqual(snapshot.slots[0].price_per_kwh, 0.25)
        self.assertFalse(snapshot.tomorrow_available)
        self.assertEqual(action.await_args.args[0:2], ("tibber", "get_prices"))

    async def test_tibber_requires_home_when_ambiguous(self):
        action = AsyncMock(return_value={"prices": {"Home A": [], "Home B": []}})
        hass = SimpleNamespace(config=SimpleNamespace(time_zone="UTC"), services=SimpleNamespace(async_call=action))
        with self.assertRaises(TibberHomeRequired):
            await async_fetch_snapshot(hass, TibberConfig("EUR"), today_local=TODAY.date())

    async def test_amber_replays_public_evcc_payload_with_inclusive_seconds(self):
        # Public evcc-io/evcc discussion 21795: redacted price-only Amber excerpt.
        current = {
            "start_time": "2025-06-12T05:00:01Z", "end_time": "2025-06-12T05:30:00Z",
            "duration": 30, "per_kwh": 0.3518919, "channel_type": "general",
        }
        forecast = {
            "start_time": "2025-06-12T05:30:01Z", "end_time": "2025-06-12T06:00:00Z",
            "duration": 30, "per_kwh": 0.5434655,
        }
        hass = SimpleNamespace(
            config=SimpleNamespace(time_zone="Australia/Sydney"),
            states={"sensor.general_price": SimpleNamespace(state="0.35", attributes=current)},
            services=SimpleNamespace(async_call=AsyncMock(return_value={"forecasts": [forecast]})),
        )
        clock_path = "custom_components.electricity_price_scheduler.providers.amber.datetime"
        with patch(clock_path) as clock:
            clock.now.return_value = datetime(2025, 6, 12, 5, tzinfo=UTC)
            clock.combine.side_effect = datetime.combine
            clock.min = datetime.min
            snapshot = await async_fetch_snapshot(
                hass, AmberConfig("site-entry", "AUD", "general", "sensor.general_price"),
                today_local=clock.now.return_value.date(),
            )
        self.assertEqual(len(snapshot.slots), 4)
        self.assertEqual(snapshot.slots[0].price_per_kwh, 0.3518919)
        self.assertEqual(snapshot.slots[1].end, snapshot.slots[2].start)
        self.assertEqual(snapshot.slots[2].price_per_kwh, 0.5434655)

    async def test_amber_actual_price_precedes_five_minute_forecast(self):
        def interval(start, price):
            return {
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(minutes=5)).isoformat(),
                "per_kwh": price,
            }

        actual = SimpleNamespace(state="0.35", attributes=interval(TODAY, 0.35) | {"channel_type": "general"})
        forecast = [interval(TODAY + timedelta(minutes=5 * index), 0.05) for index in range(350)]
        action = AsyncMock(return_value={"forecasts": forecast})
        hass = SimpleNamespace(
            config=SimpleNamespace(time_zone="UTC"),
            states={"sensor.general_price": actual},
            services=SimpleNamespace(async_call=action),
        )
        settings = AmberConfig("site-entry", "AUD", "general", "sensor.general_price")
        with patch("custom_components.electricity_price_scheduler.providers.amber.datetime") as clock:
            clock.now.return_value = TODAY + timedelta(minutes=1)
            clock.combine.side_effect = datetime.combine
            clock.min = datetime.min
            snapshot = await async_fetch_snapshot(hass, settings, today_local=TODAY.date())
            self.assertEqual(snapshot.slots[0].price_per_kwh, 0.35)
            self.assertEqual(snapshot.slots[1].price_per_kwh, 0.05)
            self.assertEqual(snapshot.slots[1].end - snapshot.slots[1].start, timedelta(minutes=5))
            self.assertLessEqual(len(snapshot.slots), 200)
            clock.now.return_value = TODAY + timedelta(minutes=5)
            with self.assertRaises(ProviderUnavailableError):
                await async_fetch_snapshot(hass, settings, today_local=TODAY.date())
        action.assert_awaited_once_with(
            "amberelectric", "get_forecasts",
            {"config_entry_id": "site-entry", "channel_type": "general"},
            blocking=True, return_response=True,
        )
