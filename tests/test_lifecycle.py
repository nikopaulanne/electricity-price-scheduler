"""Entry setup/unload tests using isolated Home Assistant instances."""

import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from ha_test_helpers import async_prepare_hass

from custom_components.electricity_price_scheduler import (
    DATA_ENTRIES,
    DATA_OPTIMIZER_SEMAPHORE,
    async_remove_entry,
)
from custom_components.electricity_price_scheduler.const import DOMAIN
from custom_components.electricity_price_scheduler.core.models import (
    CutoffPeriod,
    MultiplierResult,
    OptimizationResult,
    OptimizerConfig,
    PriceSlot,
    PriceSnapshot,
)
from custom_components.electricity_price_scheduler.coordinator import _runtime_snapshot


class EntryLifecycleTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config_dir = tempfile.TemporaryDirectory()
        components = Path(__file__).resolve().parents[1] / "custom_components"
        os.symlink(components, Path(self.config_dir.name) / "custom_components")
        self.hass = HomeAssistant(self.config_dir.name)
        await async_prepare_hass(self.hass)
        self.price_entity = er.async_get(self.hass).async_get_or_create(
            "sensor", "nordpool", "test_nordpool", suggested_object_id="test_nordpool",
        )
        self.hass.states.async_set(
            self.price_entity.entity_id,
            "1.2",
            {
                "raw_today": [],
                "raw_tomorrow": [],
                "currency": "EUR",
                "unit": "kWh",
                "price_in_cents": True,
            },
        )

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.config_dir.cleanup()

    async def _entry(self):
        prices = PriceSnapshot((), "EUR", "community_nordpool", tomorrow_available=True)
        with patch(
            "custom_components.electricity_price_scheduler.config_flow.async_fetch_snapshot",
            new=AsyncMock(return_value=prices),
        ), patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(return_value=prices),
        ):
            result = await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"], user_input={
                    "scenario": "heating",
                    "price_source": f"community_nordpool:{self.price_entity.entity_id}",
                }
            )
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"],
                user_input={
                    "profile_name": "Heating",
                    "max_cutoff_minutes": 300,
                    "min_price_diff_per_kwh": 0.03,
                },
            )
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
        await self.hass.async_block_till_done()
        return result["result"]

    async def test_real_entry_unload_stops_listeners_and_preserves_domain_gate(self):
        entry = await self._entry()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        first_gate = self.hass.data[DOMAIN][DATA_OPTIMIZER_SEMAPHORE]
        self.assertIsInstance(first_gate, asyncio.Semaphore)
        self.assertIsNotNone(coordinator._remove_state_listener)
        timer_cleanup = MagicMock()
        coordinator._remove_transition = timer_cleanup

        self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))
        self.assertFalse(coordinator._active)
        self.assertIsNone(coordinator._remove_state_listener)
        self.assertIsNone(coordinator._remove_transition)
        timer_cleanup.assert_called_once_with()
        self.assertEqual(self.hass.data[DOMAIN][DATA_ENTRIES], {})
        self.assertIs(self.hass.data[DOMAIN][DATA_OPTIMIZER_SEMAPHORE], first_gate)

        prices = PriceSnapshot((), "EUR", "community_nordpool", tomorrow_available=True)
        with patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(return_value=prices),
        ):
            # The integration domain shares its single executor gate across reloads.
            await self.hass.config_entries.async_setup(entry.entry_id)
        await self.hass.async_block_till_done()
        self.assertIs(self.hass.data[DOMAIN][DATA_OPTIMIZER_SEMAPHORE], first_gate)
        self.assertIn(entry.entry_id, self.hass.data[DOMAIN][DATA_ENTRIES])

    async def test_custom_source_expires_at_last_price_boundary(self):
        entry = await self._entry()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        coverage_end = now + timedelta(minutes=15)
        prices = PriceSnapshot(
            (PriceSlot(now, coverage_end, 0.1),),
            "EUR",
            "custom_price_sensor",
            coverage_start=now,
            coverage_end=coverage_end,
        )
        with (
            patch(
                "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
                new=AsyncMock(return_value=prices),
            ),
            patch(
                "custom_components.electricity_price_scheduler.coordinator.async_track_point_in_time",
            ) as schedule,
            patch("homeassistant.util.dt.utcnow", return_value=now) as clock,
        ):
            await coordinator.async_refresh()
            self.assertTrue(coordinator.data.available)
            self.assertEqual(schedule.call_args.args[2], coverage_end)

            clock.return_value = coverage_end
            await coordinator._async_handle_transition(coverage_end)

        self.assertFalse(coordinator.data.available)
        self.assertEqual(coordinator.data.status.value, "source_unavailable")
        self.assertIsNone(coordinator.data.phase)

    async def test_active_window_survives_reload_only_with_remaining_source_coverage(self):
        entry = await self._entry()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        period = CutoffPeriod(
            preheat_start=now - timedelta(minutes=30),
            shutdown_start=now + timedelta(minutes=15),
            recovery_start=now + timedelta(minutes=30),
            recovery_end=now + timedelta(minutes=60),
            savings_per_kw=0.1,
            savings_percent=10,
            baseline_cost_per_kw=1,
            planned_cost_per_kw=0.9,
            price_difference_per_kwh=0.1,
            min_price_diff_per_kwh=0.03,
            shutdown_duration_hours=0.25,
        )
        coordinator._locked_period = period
        await coordinator._async_persist_locked_period()
        self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))

        slots = tuple(
            PriceSlot(
                start=now - timedelta(minutes=15) + timedelta(minutes=15 * index),
                end=now + timedelta(minutes=15 * index),
                price_per_kwh=0.2,
            )
            for index in range(5)
        )
        covered = PriceSnapshot(slots, "EUR", "community_nordpool", tomorrow_available=True)
        with patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(return_value=covered),
        ):
            await self.hass.config_entries.async_setup(entry.entry_id)
        await self.hass.async_block_till_done()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        self.assertEqual(coordinator._locked_period, period)
        self.assertTrue(coordinator._locked_provenance_unknown)
        self.assertIsNone(coordinator._locked_effective_settings)

        # A subsequent source response without continuous future prices invalidates
        # the persisted lock instead of allowing its times to return on another load.
        empty = PriceSnapshot((), "EUR", "community_nordpool", tomorrow_available=True)
        self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))
        with patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(return_value=empty),
        ):
            await self.hass.config_entries.async_setup(entry.entry_id)
        await self.hass.async_block_till_done()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        self.assertIsNone(coordinator._locked_period)
        self.assertFalse(coordinator._locked_provenance_unknown)
        self.assertIsNone(await coordinator._active_window_store.async_load())

    async def test_profile_settings_mismatch_discards_persisted_lock(self):
        from custom_components.electricity_price_scheduler.coordinator import _period_to_store

        entry = await self._entry()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        period = CutoffPeriod(
            preheat_start=now - timedelta(minutes=30),
            shutdown_start=now + timedelta(minutes=15),
            recovery_start=now + timedelta(minutes=30),
            recovery_end=now + timedelta(minutes=60),
            savings_per_kw=0.1,
            savings_percent=10,
            baseline_cost_per_kw=1,
            planned_cost_per_kw=0.9,
            price_difference_per_kwh=0.1,
            min_price_diff_per_kwh=0.03,
            shutdown_duration_hours=0.25,
        )
        stored = _period_to_store(period)
        stored["settings_fingerprint"] = "previous-profile-settings"
        coordinator._restore_checked = False
        coordinator._active_window_store.async_load = AsyncMock(return_value=stored)
        coordinator._active_window_store.async_remove = AsyncMock()

        await coordinator._async_restore_locked_period(now)

        self.assertIsNone(coordinator._locked_period)
        coordinator._active_window_store.async_remove.assert_awaited_once()

    async def test_entry_removal_deletes_persisted_active_window(self):
        entry = await self._entry()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        coordinator._locked_period = CutoffPeriod(
            preheat_start=now - timedelta(minutes=30),
            shutdown_start=now + timedelta(minutes=15),
            recovery_start=now + timedelta(minutes=30),
            recovery_end=now + timedelta(minutes=60),
            savings_per_kw=0.1,
            savings_percent=10,
            baseline_cost_per_kw=1,
            planned_cost_per_kw=0.9,
            price_difference_per_kwh=0.1,
            min_price_diff_per_kwh=0.03,
            shutdown_duration_hours=0.25,
        )
        await coordinator._async_persist_locked_period()
        self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))

        await async_remove_entry(self.hass, entry)

        self.assertIsNone(await coordinator._active_window_store.async_load())

    async def test_shutdown_waits_for_update_lock_before_final_store_flush(self):
        from custom_components.electricity_price_scheduler.coordinator import _period_from_store

        entry = await self._entry()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        def period(offset: int) -> CutoffPeriod:
            return CutoffPeriod(
                preheat_start=now - timedelta(minutes=30),
                shutdown_start=now + timedelta(minutes=offset),
                recovery_start=now + timedelta(minutes=offset + 15),
                recovery_end=now + timedelta(minutes=offset + 45),
                savings_per_kw=0.1,
                savings_percent=10,
                baseline_cost_per_kw=1,
                planned_cost_per_kw=0.9,
                price_difference_per_kwh=0.1,
                min_price_diff_per_kwh=0.03,
                shutdown_duration_hours=0.25,
            )

        original, final = period(15), period(30)
        coordinator._locked_period = original
        await coordinator._async_persist_locked_period()
        async with coordinator._update_lock:
            shutdown = self.hass.async_create_task(coordinator.async_shutdown())
            await asyncio.sleep(0)
            self.assertFalse(shutdown.done())
            coordinator._locked_period = final
        await shutdown

        stored = await coordinator._active_window_store.async_load()
        self.assertEqual(_period_from_store(stored), final)

    async def test_cancelled_executor_keeps_gate_until_worker_finishes(self):
        entry = await self._entry()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        prices = PriceSnapshot((), "EUR", "community_nordpool", tomorrow_available=True)
        config = OptimizerConfig()
        multiplier = MultiplierResult(1.5, 1.2, "fixed", "configured")
        future = asyncio.get_running_loop().create_future()
        with patch.object(self.hass, "async_add_executor_job", return_value=future):
            task = self.hass.async_create_task(
                coordinator._async_calculate(
                    prices, config, multiplier, datetime.now(timezone.utc), None
                )
            )
            await asyncio.sleep(0)
            gate = coordinator._semaphore
            self.assertTrue(gate.locked())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertTrue(gate.locked())
            future.set_result((OptimizationResult((), 0, 0, 0, 0, "EUR"), None))
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(gate.locked())

    async def test_unload_invalidates_pending_calculation_result(self):
        entry = await self._entry()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        future = asyncio.get_running_loop().create_future()
        prices = PriceSnapshot((), "EUR", "community_nordpool", tomorrow_available=True)
        with patch.object(self.hass, "async_add_executor_job", return_value=future):
            task = self.hass.async_create_task(
                coordinator._async_calculate(
                    prices,
                    OptimizerConfig(),
                    MultiplierResult(1.5, 1.2, "fixed", "configured"),
                    datetime.now(timezone.utc),
                    None,
                )
            )
            await asyncio.sleep(0)
            shutdown = self.hass.async_create_task(coordinator.async_shutdown())
            future.set_result((OptimizationResult((), 0, 0, 0, 0, "EUR"), None))
            await shutdown
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(coordinator._semaphore.locked())

    async def test_unchanged_inputs_do_not_repeat_dp(self):
        entry = await self._entry()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        prices = PriceSnapshot((), "EUR", "community_nordpool", tomorrow_available=True)
        empty = OptimizationResult((), 0, 0, 0, 0, "EUR")
        coordinator._calculation_key = None
        with patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(return_value=prices),
        ) as fetch, patch(
            "custom_components.electricity_price_scheduler.coordinator.optimize",
            return_value=empty,
        ) as optimizer:
            await coordinator._async_update_data()
            await coordinator._async_update_data()
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(optimizer.call_count, 1)

    async def test_repeated_source_error_logs_only_one_warning(self):
        entry = await self._entry()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        with self.assertLogs(
            "custom_components.electricity_price_scheduler.coordinator", level="WARNING"
        ) as captured:
            coordinator._log_error_transition("source_unavailable", "Source failed")
            coordinator._log_error_transition("source_unavailable", "Source failed")
        self.assertEqual(len(captured.output), 1)

    async def test_phase_boundary_updates_state_without_fetch_or_dp(self):
        entry = await self._entry()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = now - timedelta(minutes=30)
        period = CutoffPeriod(
            preheat_start=start,
            shutdown_start=now,
            recovery_start=now + timedelta(minutes=30),
            recovery_end=now + timedelta(hours=1),
            savings_per_kw=0.1,
            savings_percent=10,
            baseline_cost_per_kw=1,
            planned_cost_per_kw=0.9,
            price_difference_per_kwh=0.1,
            min_price_diff_per_kwh=0.03,
            shutdown_duration_hours=0.5,
        )
        prices = PriceSnapshot((), "EUR", "community_nordpool", tomorrow_available=True)
        result = OptimizationResult((period,), 0.1, 1, 1, 0, "EUR")
        coordinator._locked_period = period
        coordinator._last_price_snapshot = prices
        coordinator._last_result = result
        coordinator.async_set_updated_data(
            _runtime_snapshot(
                prices,
                result,
                now - timedelta(seconds=1),
                last_success=now - timedelta(minutes=1),
                multipliers=None,
            )
        )
        with patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(),
        ) as fetch, patch(
            "custom_components.electricity_price_scheduler.coordinator.optimize"
        ) as optimizer, patch.object(coordinator, "_schedule_next_transition"):
            await coordinator._async_handle_transition(now)
        self.assertEqual(coordinator.data.phase, "shutdown")
        fetch.assert_not_awaited()
        optimizer.assert_not_called()

    async def test_lock_expiry_replans_cached_data_with_full_k(self):
        entry = await self._entry()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        period = CutoffPeriod(
            preheat_start=now - timedelta(hours=1),
            shutdown_start=now - timedelta(minutes=30),
            recovery_start=now - timedelta(minutes=15),
            recovery_end=now,
            savings_per_kw=0.1,
            savings_percent=10,
            baseline_cost_per_kw=1,
            planned_cost_per_kw=0.9,
            price_difference_per_kwh=0.1,
            min_price_diff_per_kwh=0.03,
            shutdown_duration_hours=0.25,
        )
        prices = PriceSnapshot((), "EUR", "community_nordpool", tomorrow_available=True)
        result = OptimizationResult((period,), 0.1, 1, 1, 0, "EUR")
        coordinator._locked_period = period
        coordinator._last_price_snapshot = prices
        coordinator._last_optimizer_config = OptimizerConfig(max_periods=4)
        coordinator._last_multiplier = MultiplierResult(1.5, 1.2, "fixed", "configured")
        coordinator._last_result = result
        coordinator.async_set_updated_data(
            _runtime_snapshot(
                prices,
                result,
                now - timedelta(minutes=1),
                last_success=now - timedelta(minutes=1),
                multipliers=coordinator._last_multiplier,
            )
        )
        empty = OptimizationResult((), 0, 0, 0, 0, "EUR")
        with patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(),
        ) as fetch, patch(
            "custom_components.electricity_price_scheduler.coordinator.optimize",
            return_value=empty,
        ) as optimizer, patch.object(coordinator, "_schedule_next_transition"):
            await coordinator._async_handle_transition(now)
            await self.hass.async_block_till_done()
        self.assertIsNone(coordinator._locked_period)
        self.assertEqual(optimizer.call_count, 1)
        self.assertEqual(optimizer.call_args.args[1].max_periods, 4)
        fetch.assert_not_awaited()

    async def test_delayed_timer_uses_actual_now_and_expires_old_lock(self):
        entry = await self._entry()
        coordinator = self.hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        period = CutoffPeriod(
            preheat_start=now - timedelta(minutes=5),
            shutdown_start=now + timedelta(minutes=25),
            recovery_start=now + timedelta(minutes=55),
            recovery_end=now + timedelta(minutes=85),
            savings_per_kw=0.1,
            savings_percent=10,
            baseline_cost_per_kw=1,
            planned_cost_per_kw=0.9,
            price_difference_per_kwh=0.1,
            min_price_diff_per_kwh=0.03,
            shutdown_duration_hours=0.5,
        )
        prices = PriceSnapshot((), "EUR", "community_nordpool", tomorrow_available=True)
        result = OptimizationResult((period,), 0.1, 1, 1, 0, "EUR")
        coordinator._locked_period = period
        coordinator._last_price_snapshot = prices
        coordinator._last_optimizer_config = OptimizerConfig(max_periods=4)
        coordinator._last_multiplier = MultiplierResult(1.5, 1.2, "fixed", "configured")
        coordinator._last_result = result
        runtime = _runtime_snapshot(
            prices,
            result,
            now,
            last_success=now,
            multipliers=coordinator._last_multiplier,
        )
        coordinator.async_set_updated_data(runtime)

        with patch(
            "custom_components.electricity_price_scheduler.coordinator.async_track_point_in_time",
            return_value=MagicMock(),
        ) as track_timer:
            coordinator._schedule_next_transition(runtime)
        timer_callback = track_timer.call_args.args[1]
        late_now = period.recovery_end + timedelta(hours=1)
        empty = OptimizationResult((), 0, 0, 0, 0, "EUR")
        from homeassistant.util import dt as dt_util

        with patch.object(dt_util, "utcnow", return_value=late_now), patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(),
        ) as fetch, patch(
            "custom_components.electricity_price_scheduler.coordinator.optimize",
            return_value=empty,
        ) as optimizer, patch.object(coordinator, "_schedule_next_transition"):
            # The callback's scheduled timestamp is old; it must re-read wall clock.
            timer_callback(period.shutdown_start)
            await self.hass.async_block_till_done()
        self.assertIsNone(coordinator._locked_period)
        self.assertEqual(coordinator.data.phase, "normal")
        self.assertEqual(optimizer.call_count, 1)
        self.assertEqual(optimizer.call_args.args[1].max_periods, 4)
        fetch.assert_not_awaited()


if __name__ == "__main__":
    import unittest

    unittest.main()
