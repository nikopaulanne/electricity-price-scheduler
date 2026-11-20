"""Public-action provider tests using fake HA services, not live Nord Pool."""

from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime, timezone

from custom_components.electricity_price_scheduler.providers import (
    CoreNordpoolConfig,
    ProviderDataError,
    ProviderUnavailableError,
    async_fetch_snapshot,
)
from custom_components.electricity_price_scheduler.providers.core_nordpool import (
    ACTION_DOMAIN,
    ACTION_NAME,
    CoreNordpoolProvider,
)

TODAY = date(2026, 11, 5)
UTC = timezone.utc


class FakeServices:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object], dict[str, object]]] = []

    async def async_call(
        self,
        domain: str,
        service: str,
        service_data: dict[str, object],
        **kwargs: object,
    ) -> object:
        self.calls.append((domain, service, service_data, kwargs))
        key = str(service_data["date"])
        result = self.responses[key]
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return await result()
        return result


class FakeHass:
    def __init__(self, services: FakeServices):
        self.services = services
        self.config = type("Config", (), {"time_zone": "Europe/Helsinki"})()


def _response(start: datetime, *, price: float = 1000.0) -> dict[str, list[dict[str, object]]]:
    return {
        "NO4": [
            {"start": start, "end": start.replace(minute=start.minute + 15), "price": price}
        ]
    }


def _config() -> CoreNordpoolConfig:
    return CoreNordpoolConfig("config-entry-123", "NO4", "NOK")


class CoreNordpoolProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_public_action_and_normalizes_currency_per_mwh(self) -> None:
        services = FakeServices(
            {
                "2026-11-05": _response(datetime(2026, 11, 4, 23, 0, tzinfo=UTC), price=1250),
                "2026-11-06": _response(datetime(2026, 11, 5, 23, 0, tzinfo=UTC), price=2500),
            }
        )
        snapshot = await CoreNordpoolProvider().async_fetch_snapshot(
            FakeHass(services), _config(), today_local=TODAY
        )

        self.assertEqual(snapshot.provider, "core_nordpool")
        self.assertEqual(snapshot.currency, "NOK")
        self.assertTrue(snapshot.tomorrow_available)
        self.assertEqual(len(services.calls), 3)
        for domain, action, data, kwargs in services.calls:
            self.assertEqual((domain, action), (ACTION_DOMAIN, ACTION_NAME))
            self.assertEqual(data["config_entry"], "config-entry-123")
            self.assertEqual(data["areas"], ["NO4"])
            self.assertEqual(data["currency"], "NOK")
            self.assertEqual(kwargs, {"blocking": True, "return_response": True})
            self.assertNotIn("resolution", data)
        self.assertAlmostEqual(snapshot.slots[0].price_per_kwh, 1.25)
        self.assertAlmostEqual(snapshot.slots[1].price_per_kwh, 2.5)
        self.assertEqual(snapshot.slots[0].start.isoformat(), "2026-11-04T23:00:00+00:00")

    async def test_previous_market_day_supplies_the_finnish_midnight_hour(self) -> None:
        services = FakeServices({
            "2026-11-04": _response(datetime(2026, 11, 4, 22, 0, tzinfo=UTC), price=200),
            "2026-11-05": _response(datetime(2026, 11, 4, 23, 0, tzinfo=UTC), price=500),
            "2026-11-06": RuntimeError("not published"),
        })
        snapshot = await CoreNordpoolProvider().async_fetch_snapshot(
            FakeHass(services), _config(), today_local=TODAY,
        )
        self.assertEqual(snapshot.slots[0].start, datetime(2026, 11, 4, 22, tzinfo=UTC))
        self.assertEqual(snapshot.slots[0].price_per_kwh, 0.2)
        self.assertFalse(snapshot.tomorrow_available)

    async def test_unpublished_tomorrow_is_partial_success(self) -> None:
        services = FakeServices(
            {
                "2026-11-05": _response(datetime(2026, 11, 4, 23, 0, tzinfo=UTC)),
                "2026-11-06": RuntimeError("tomorrow not published"),
            }
        )
        snapshot = await CoreNordpoolProvider().async_fetch_snapshot(
            FakeHass(services), _config(), today_local=TODAY
        )
        self.assertFalse(snapshot.tomorrow_available)
        self.assertIn("core_nordpool_tomorrow_prices_unavailable", snapshot.warnings)
        self.assertEqual(len(snapshot.slots), 1)

    async def test_today_action_failure_is_source_unavailable(self) -> None:
        services = FakeServices({"2026-11-05": RuntimeError("offline")})
        with self.assertRaises(ProviderUnavailableError):
            await CoreNordpoolProvider().async_fetch_snapshot(
                FakeHass(services), _config(), today_local=TODAY
            )
        self.assertEqual(len(services.calls), 1)

    async def test_malformed_tomorrow_response_fails_closed(self) -> None:
        services = FakeServices(
            {
                "2026-11-05": _response(datetime(2026, 11, 4, 23, 0, tzinfo=UTC)),
                "2026-11-06": {"NO4": [{"start": "bad", "end": "bad", "price": 1}]},
            }
        )
        with self.assertRaises(ProviderDataError):
            await CoreNordpoolProvider().async_fetch_snapshot(
                FakeHass(services), _config(), today_local=TODAY
            )

    async def test_oversized_raw_response_is_rejected(self) -> None:
        rows = [
            {
                "start": datetime(2026, 11, 4, 23, 0, tzinfo=UTC),
                "end": datetime(2026, 11, 4, 23, 15, tzinfo=UTC),
                "price": 1.0,
            }
            for _ in range(401)
        ]
        services = FakeServices({"2026-11-05": {"NO4": rows}})
        with self.assertRaises(ProviderDataError):
            await CoreNordpoolProvider().async_fetch_snapshot(
                FakeHass(services), _config(), today_local=TODAY
            )

    async def test_service_timeout_is_unavailable(self) -> None:
        async def never_returns() -> object:
            await asyncio.sleep(0.02)
            return _response(datetime(2026, 11, 4, 23, 0, tzinfo=UTC))

        services = FakeServices({"2026-11-05": never_returns})
        provider = CoreNordpoolProvider()
        old_timeout = provider.ACTION_TIMEOUT_SECONDS if hasattr(provider, "ACTION_TIMEOUT_SECONDS") else None
        # Patch the module constant without waiting for the production timeout.
        from custom_components.electricity_price_scheduler.providers import core_nordpool

        original = core_nordpool.ACTION_TIMEOUT_SECONDS
        core_nordpool.ACTION_TIMEOUT_SECONDS = 0.001
        try:
            with self.assertRaises(ProviderUnavailableError):
                await provider.async_fetch_snapshot(FakeHass(services), _config(), today_local=TODAY)
        finally:
            core_nordpool.ACTION_TIMEOUT_SECONDS = original

    async def test_facade_dispatches_core_provider(self) -> None:
        services = FakeServices(
            {
                "2026-11-05": _response(datetime(2026, 11, 4, 23, 0, tzinfo=UTC)),
                "2026-11-06": RuntimeError("not published"),
            }
        )
        snapshot = await async_fetch_snapshot(
            FakeHass(services), _config(), today_local=TODAY
        )
        self.assertEqual(snapshot.provider, "core_nordpool")


if __name__ == "__main__":
    unittest.main()
