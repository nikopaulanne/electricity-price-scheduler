"""Stable, entry-scoped IDs survive profile-title edits."""

import logging
import os
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from ha_test_helpers import async_prepare_hass

from custom_components.electricity_price_scheduler.const import DOMAIN
from custom_components.electricity_price_scheduler.entity import OptimizerEntity
from custom_components.electricity_price_scheduler.diagnostics import (
    async_get_config_entry_diagnostics,
)


class EntityIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_ids_follow_entry_id_not_profile_title(self):
        config_dir = tempfile.TemporaryDirectory()
        components = Path(__file__).resolve().parents[1] / "custom_components"
        os.symlink(components, Path(config_dir.name) / "custom_components")
        hass = HomeAssistant(config_dir.name)
        try:
            await async_prepare_hass(hass)
            coordinator = DataUpdateCoordinator(hass, logging.getLogger(__name__), name="test", config_entry=None)
            coordinator.entry = SimpleNamespace(entry_id="stable-entry-id", title="Heating")
            first = OptimizerEntity(coordinator, "phase")
            coordinator.entry = SimpleNamespace(entry_id="stable-entry-id", title="Renamed heating")
            renamed = OptimizerEntity(coordinator, "phase")

            self.assertEqual(first.unique_id, "stable-entry-id_phase")
            self.assertEqual(renamed.unique_id, first.unique_id)
            self.assertEqual(first.device_info["identifiers"], {(DOMAIN, "stable-entry-id")})
            self.assertEqual(renamed.device_info["identifiers"], first.device_info["identifiers"])
            self.assertEqual(renamed.device_info["name"], "Renamed heating")
        finally:
            await hass.async_stop()
            config_dir.cleanup()

    async def test_diagnostics_omit_source_and_temperature_entity_ids(self):
        hass = HomeAssistant("/tmp/ha-diagnostics-test")
        entry = SimpleNamespace(
            entry_id="entry-1",
            data={
                "provider_type": "community_nordpool",
                "price_entity_id": "sensor.private_prices",
                "source_config_entry_id": "secret-source-entry",
                "currency": "EUR",
            },
            options={
                "temperature_entity_id": "sensor.private_temperature",
                "max_cutoff_entity_id": "input_number.private_duration",
                "min_price_diff_entity_id": "input_number.private_threshold",
                "max_periods": 4,
            },
        )
        try:
            result = await async_get_config_entry_diagnostics(hass, entry)
            rendered = repr(result)
            self.assertNotIn("sensor.private_prices", rendered)
            self.assertNotIn("sensor.private_temperature", rendered)
            self.assertNotIn("input_number.private_duration", rendered)
            self.assertNotIn("input_number.private_threshold", rendered)
            self.assertNotIn("secret-source-entry", rendered)
            self.assertEqual(result["options"], {"max_periods": 4})
        finally:
            await hass.async_stop()


if __name__ == "__main__":
    unittest.main()
