"""Config-flow coverage for the opt-in custom price sensor path."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
import tempfile
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import voluptuous as vol
from ha_test_helpers import async_prepare_hass

from custom_components.electricity_price_scheduler.const import DOMAIN
from custom_components.electricity_price_scheduler.core.models import PriceSlot, PriceSnapshot


class CustomPriceSensorFlowTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config_dir = tempfile.TemporaryDirectory()
        components = Path(__file__).resolve().parents[1] / "custom_components"
        os.symlink(components, Path(self.config_dir.name) / "custom_components")
        self.hass = HomeAssistant(self.config_dir.name)
        await async_prepare_hass(self.hass)
        now = dt_util.utcnow()
        self.snapshot = PriceSnapshot(
            slots=(PriceSlot(now - timedelta(minutes=5), now + timedelta(minutes=10), 0.05),),
            currency="EUR",
            provider="custom_price_sensor",
            coverage_start=now - timedelta(minutes=5),
            coverage_end=now + timedelta(minutes=10),
            tomorrow_available=False,
        )
        self.hass.states.async_set("sensor.proxy_price", "5.0", {
            "friendly_name": "Proxy price",
            "currency": "EUR",
            "unit": "c/kWh",
            "price_in_cents": True,
            "raw_today": [],
            "raw_tomorrow": [],
        })

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.config_dir.cleanup()

    @staticmethod
    def _field(form, name):
        return next(key for key in form["data_schema"].schema if str(key) == name)

    async def _add_nordpool_source(self, object_id: str):
        entity = er.async_get(self.hass).async_get_or_create(
            "sensor", "nordpool", object_id, suggested_object_id=object_id,
        )
        self.hass.states.async_set(entity.entity_id, "5.0", {
            "raw_today": [], "raw_tomorrow": [], "currency": "EUR",
            "unit": "kWh", "price_in_cents": False,
        })
        return entity.entity_id

    async def test_market_picker_defaults_for_zero_one_and_multiple_sources(self):
        form = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER},
        )
        source = self._field(form, "price_source")
        self.assertEqual(source.default(), "custom_price_sensor")

        first = await self._add_nordpool_source("first_price")
        form = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER},
        )
        source = self._field(form, "price_source")
        self.assertEqual(source.default(), f"community_nordpool:{first}")
        selector = form["data_schema"].schema[source]
        options = {item["value"] for item in selector.config["options"]}
        self.assertIn("custom_price_sensor", options)

        await self._add_nordpool_source("second_price")
        form = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER},
        )
        source = self._field(form, "price_source")
        self.assertIs(source.default, vol.UNDEFINED)
        selector = form["data_schema"].schema[source]
        self.assertIn("custom_price_sensor", {item["value"] for item in selector.config["options"]})

    async def _create_custom_entry(self):
        with patch(
            "custom_components.electricity_price_scheduler.config_flow.async_fetch_snapshot",
            new=AsyncMock(return_value=self.snapshot),
        ), patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(return_value=self.snapshot),
        ):
            form = await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER},
            )
            result = await self.hass.config_entries.flow.async_configure(form["flow_id"], {
                "scenario": "heating",
                "price_source": "custom_price_sensor",
                "price_entity_id": "sensor.proxy_price",
            })
            self.assertEqual(result["step_id"], "market_source")
            self.assertEqual(result["errors"]["unit_override"], "unit_override_required")
            self.assertIn("unit_override", {str(key) for key in result["data_schema"].schema})
            result = await self.hass.config_entries.flow.async_configure(result["flow_id"], {
                "scenario": "heating",
                "price_source": "custom_price_sensor",
                "price_entity_id": "sensor.proxy_price",
                "unit_override": "snt/kWh",
            })
            self.assertEqual(result["step_id"], "custom_price_preview")
            self.assertIn("current_price", result["description_placeholders"])
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"], {"continue": True},
            )
            self.assertEqual(result["step_id"], "settings")
            result = await self.hass.config_entries.flow.async_configure(result["flow_id"], {
                "profile_name": "Proxy test",
                "max_cutoff_minutes": 300,
                "min_price_diff_per_kwh": 0.03,
            })
        await self.hass.async_block_till_done()
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
        return result["result"]

    async def test_setup_previews_custom_source_and_persists_explicit_opt_in(self):
        entry = await self._create_custom_entry()
        self.assertEqual(entry.data["provider_type"], "community_nordpool")
        self.assertIs(entry.data["custom_price_sensor"], True)
        self.assertEqual(entry.data["price_entity_id"], "sensor.proxy_price")
        self.assertEqual(entry.data["unit_override"], "snt/kWh")
        self.assertEqual(entry.data["currency"], "EUR")

    async def test_reconfigure_switches_custom_sensor_without_changing_profile_entities(self):
        entry = await self._create_custom_entry()
        registry = er.async_get(self.hass)
        original_entities = {
            entity.entity_id for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        self.assertTrue(original_entities)
        first_source = self.hass.states.get("sensor.proxy_price")
        self.hass.states.async_set("sensor.other_proxy_price", "5.0", first_source.attributes)
        with patch(
            "custom_components.electricity_price_scheduler.config_flow.async_fetch_snapshot",
            new=AsyncMock(return_value=self.snapshot),
        ), patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(return_value=self.snapshot),
        ):
            form = await self.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
            )
            self.assertEqual(form["step_id"], "market_reconfigure")
            source = self._field(form, "price_source")
            self.assertEqual(source.default(), "custom_price_sensor")
            self.assertEqual(self._field(form, "price_entity_id").default(), "sensor.proxy_price")
            result = await self.hass.config_entries.flow.async_configure(form["flow_id"], {
                "price_source": "custom_price_sensor",
                "price_entity_id": "sensor.other_proxy_price",
                "unit_override": "snt/kWh",
            })
            self.assertEqual(result["step_id"], "custom_price_preview")
            self.assertEqual(entry.data["price_entity_id"], "sensor.proxy_price")
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"], {"continue": True},
            )
            await self.hass.async_block_till_done()
        self.assertEqual(result["type"], FlowResultType.ABORT, result)
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertIs(entry.data["custom_price_sensor"], True)
        self.assertEqual(entry.data["price_entity_id"], "sensor.other_proxy_price")
        self.assertEqual(original_entities, {
            entity.entity_id for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        })
