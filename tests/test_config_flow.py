"""Home Assistant 2026.11 config flow tests using the actual flow manager."""

import os
from datetime import timedelta
from pathlib import Path
import tempfile
from types import MappingProxyType
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntry, ConfigEntryState, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util
import voluptuous as vol
from ha_test_helpers import async_prepare_hass

from custom_components.electricity_price_scheduler.const import DOMAIN
from custom_components.electricity_price_scheduler.config_flow import _temperature_is_valid
from custom_components.electricity_price_scheduler.core.models import PriceSlot, PriceSnapshot
from custom_components.electricity_price_scheduler.providers.base import ProviderDataError


class ConfigFlowTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config_dir = tempfile.TemporaryDirectory()
        source_components = Path(__file__).resolve().parents[1] / "custom_components"
        os.symlink(source_components, Path(self.config_dir.name) / "custom_components")
        self.hass = HomeAssistant(self.config_dir.name)
        await async_prepare_hass(self.hass)
        self.nordpool_entity = er.async_get(self.hass).async_get_or_create(
            "sensor", "nordpool", "test-source", suggested_object_id="test_nordpool"
        ).entity_id
        self.hass.states.async_set(
            self.nordpool_entity,
            "1.2",
            {
                "raw_today": [],
                "raw_tomorrow": [],
                "currency": "EUR",
                "unit": "kWh",
                "price_in_cents": True,
            },
        )
        self.snapshot = PriceSnapshot(
            slots=(),
            currency="EUR",
            provider="community_nordpool",
            coverage_start=None,
            coverage_end=None,
            tomorrow_available=False,
        )

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.config_dir.cleanup()

    def _patch_fetch(self):
        return patch(
            "custom_components.electricity_price_scheduler.config_flow.async_fetch_snapshot",
            new=AsyncMock(return_value=self.snapshot),
        )

    async def _select_source(self, mock_fetch):
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        self.assertEqual(result["type"], FlowResultType.FORM)
        self.assertEqual(result["step_id"], "market_source")
        return await self.hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={
                "scenario": "heating",
                "price_source": f"community_nordpool:{self.nordpool_entity}",
            }
        )

    async def _choose_source(self, form, *, scenario="cheap_now", entity_id="sensor.test_nordpool"):
        registry = er.async_get(self.hass)
        if entity_id == "sensor.test_nordpool":
            entity_id = self.nordpool_entity
        else:
            registry.async_get_or_create(
                "sensor", "nordpool", entity_id.rsplit(".", 1)[-1],
                suggested_object_id=entity_id.rsplit(".", 1)[-1],
            )
        return await self.hass.config_entries.flow.async_configure(form["flow_id"], {
            "scenario": scenario,
            "price_source": f"community_nordpool:{entity_id}",
        })

    async def test_valid_setup_persists_source_and_compact_defaults(self):
        with self._patch_fetch() as mock_fetch, patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(return_value=self.snapshot),
        ):
            settings = await self._select_source(mock_fetch)
            self.assertEqual(settings["type"], FlowResultType.FORM)
            self.assertEqual(settings["step_id"], "settings")
            form_keys = {str(key) for key in settings["data_schema"].schema}
            self.assertIn("profile_name", form_keys)
            self.assertIn("max_cutoff_minutes", form_keys)
            self.assertIn("min_price_diff_per_kwh", form_keys)
            self.assertNotIn("max_periods", form_keys)

            result = await self.hass.config_entries.flow.async_configure(
                settings["flow_id"],
                user_input={
                    "profile_name": "Heating",
                    "max_cutoff_minutes": 300,
                    "min_price_diff_per_kwh": 0.03,
                },
            )
        self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
        entry = result["result"]
        await self.hass.async_block_till_done()
        self.assertEqual(entry.state, ConfigEntryState.LOADED)
        self.assertTrue(any(state.entity_id.endswith("cutoff_phase") for state in self.hass.states.async_all()))
        self.assertEqual(entry.title, "Heating")
        self.assertEqual(
            dict(entry.data),
            {
                "provider_type": "community_nordpool",
                "price_entity_id": "sensor.test_nordpool",
                "currency": "EUR",
            },
        )
        self.assertEqual(entry.options["max_periods"], 6)
        self.assertEqual(entry.options["multiplier_mode"], "fixed")
        self.assertNotIn("profile_name", entry.options)
        mock_fetch.assert_awaited_once()

    async def test_source_picker_offers_registered_source_and_custom_sensor(self):
        registry = er.async_get(self.hass)
        registry.async_get_or_create(
            "sensor", "nordpool", "test-source", suggested_object_id="test_nordpool"
        )
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        source_key = next(key for key in result["data_schema"].schema if str(key) == "price_source")
        options = result["data_schema"].schema[source_key].config["options"]
        values = {item["value"] for item in options}
        self.assertIn("community_nordpool:sensor.test_nordpool", values)
        self.assertIn("custom_price_sensor", values)

    async def test_sole_registered_community_source_is_preselected_but_custom_remains_available(self):
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        source = next(key for key in result["data_schema"].schema if str(key) == "price_source")
        self.assertEqual(source.default(), f"community_nordpool:{self.nordpool_entity}")
        values = {item["value"] for item in result["data_schema"].schema[source].config["options"]}
        self.assertIn("custom_price_sensor", values)

    async def test_multiple_registered_community_sources_are_not_guessed(self):
        registry = er.async_get(self.hass)
        first = registry.async_get_or_create(
            "sensor", "nordpool", "registered-test", suggested_object_id="registered_nordpool"
        )
        second = registry.async_get_or_create(
            "sensor", "nordpool", "registered-test-2", suggested_object_id="second_nordpool"
        )
        self.hass.states.async_set(first.entity_id, "1.2", {
            "raw_today": [], "currency": "EUR",
        })
        self.hass.states.async_set(second.entity_id, "1.5", {
            "raw_today": [], "currency": "EUR",
        })
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        price_key = next(key for key in result["data_schema"].schema if str(key) == "price_source")
        default = price_key.default() if callable(price_key.default) else price_key.default
        self.assertIs(default, vol.UNDEFINED)

    async def test_market_sources_include_only_eligible_entities(self):
        er.async_get(self.hass).async_remove(self.nordpool_entity)
        registry = er.async_get(self.hass)
        entsoe = registry.async_get_or_create(
            "sensor", "entsoe", "average", suggested_object_id="de_average_price"
        )
        self.hass.states.async_set(entsoe.entity_id, "1.2", {
            "prices_today": [], "unit_of_measurement": "EUR/kWh",
        })
        ignored = registry.async_get_or_create(
            "sensor", "entsoe", "not-average", suggested_object_id="de_current_price"
        )
        self.hass.states.async_set(ignored.entity_id, "0.05", {"unit_of_measurement": "EUR/kWh"})
        with self._patch_fetch() as mock_fetch:
            result = await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
            self.assertEqual(result["step_id"], "market_source")
            source = next(
                key for key in result["data_schema"].schema if str(key) == "price_source"
            )
            selected = f"entsoe:{entsoe.entity_id}"
            self.assertEqual(source.default(), selected)
            self.assertEqual(
                [item["value"] for item in result["data_schema"].schema[source].config["options"]],
                [selected, "custom_price_sensor"],
            )
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"], {"scenario": "cheap_now", "price_source": selected}
            )
            self.assertEqual(result["step_id"], "price_setup")
            self.assertEqual(mock_fetch.await_args.args[1].provider_type, "entsoe")

    async def test_market_reconfigure_preserves_threshold_until_new_currency_is_confirmed(self):
        with self._patch_fetch(), patch(
            "custom_components.electricity_price_scheduler.price_runtime.async_fetch_snapshot",
            new=AsyncMock(return_value=self.snapshot),
        ):
            first = await self._select_source(AsyncMock())
            original = await self.hass.config_entries.flow.async_configure(
                first["flow_id"], {
                    "profile_name": "Heating", "max_cutoff_minutes": 180,
                    "min_price_diff_per_kwh": 0.03,
                }
            )
            entry = original["result"]
            await self.hass.async_block_till_done()
            octopus = er.async_get(self.hass).async_get_or_create(
                "event", "octopus_energy", "rates",
                suggested_object_id="octopus_energy_electricity_test_meter_current_day_rates",
            )
            self.hass.states.async_set(octopus.entity_id, "2026-11-20T10:00:00+00:00")
            result = await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
            )
            self.assertEqual(result["step_id"], "market_reconfigure")
            source = f"octopus_energy:{octopus.entity_id}"
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"], {"price_source": source}
            )
            self.assertEqual(result["errors"]["min_price_diff_per_kwh"], "currency_changed_threshold_required")
            self.assertEqual(entry.data["currency"], "EUR")
            self.assertEqual(entry.options["min_price_diff_per_kwh"], 0.03)
            with patch(
                "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
                new=AsyncMock(return_value=self.snapshot),
            ):
                result = await self.hass.config_entries.flow.async_configure(
                    result["flow_id"], {"price_source": source, "min_price_diff_per_kwh": 0.05}
                )
                self.assertEqual(result["reason"], "reconfigure_successful")
                await self.hass.async_block_till_done()
            self.assertEqual(entry.data["currency"], "GBP")
            self.assertEqual(entry.options["min_price_diff_per_kwh"], 0.05)

    async def test_amber_source_defaults_to_cheap_now_and_rejects_other_scenarios(self):
        er.async_get(self.hass).async_remove(self.nordpool_entity)
        async def price_action(_call):
            return {"forecasts": []}

        self.hass.services.async_register("amberelectric", "get_forecasts", price_action)
        entry = ConfigEntry(
            data={}, discovery_keys=MappingProxyType({}), domain="amberelectric",
            minor_version=1, options={}, source=SOURCE_USER, subentries_data=None,
            title="Amber test", unique_id=None, version=1,
        )
        with patch.object(self.hass.config_entries, "async_setup", new=AsyncMock()):
            await self.hass.config_entries.async_add(entry)
        current_price = er.async_get(self.hass).async_get_or_create(
            "sensor", "amberelectric", "site-current-general",
            config_entry=entry,
            suggested_object_id="amber_general_price",
        )
        self.hass.states.async_set(current_price.entity_id, "0.15")
        with self._patch_fetch() as mock_fetch:
            form = await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
            self.assertEqual(form["step_id"], "market_source")
            scenario = next(key for key in form["data_schema"].schema if str(key) == "scenario")
            self.assertEqual(scenario.default(), "cheap_now")
            source = f"amber:{current_price.entity_id}"
            rejected = await self.hass.config_entries.flow.async_configure(
                form["flow_id"], {"scenario": "heating", "price_source": source}
            )
            self.assertEqual(rejected["errors"]["price_source"], "threshold_only_source")
            mock_fetch.assert_not_awaited()
            accepted = await self.hass.config_entries.flow.async_configure(
                form["flow_id"], {"scenario": "cheap_now", "price_source": source}
            )
            self.assertEqual(accepted["step_id"], "price_setup")
            self.assertEqual(mock_fetch.await_args.args[1].currency, "AUD")

    async def test_onboarding_and_decision_texts_load_for_supported_languages(self):
        now = dt_util.utcnow()
        slots = (PriceSlot(now - timedelta(minutes=5), now + timedelta(minutes=10), 0.08),)
        self.snapshot = PriceSnapshot(slots, "EUR", "community_nordpool")
        expected = {
            "en": ("Cheap electricity", "The current price exceeds your limit."),
            "fi": ("Halpa sähkö", "Nykyinen hinta ylittää asettamasi rajan."),
            "sv": ("Billig el", "Det aktuella priset överskrider din gräns."),
            "de": ("Günstiger Strom", "Der aktuelle Preis liegt über Ihrer Preisgrenze."),
            "es": ("Cheap electricity", "The current price exceeds your limit."),
        }
        prefix = f"component.{DOMAIN}"
        attr_key = f"{prefix}.entity.sensor.price_data_status.state_attributes.reason_code.state"
        preview_key = f"{prefix}.config.step.price_preview_threshold_above.description"
        for language, (starter_name, above_limit) in expected.items():
            with self.subTest(language=language), self._patch_fetch():
                self.hass.config.language = language
                form = await self.hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": SOURCE_USER}
                )
                flow_id = form["flow_id"]
                setup = await self._choose_source(form, scenario="cheap_now")
                profile_key = next(
                    key for key in setup["data_schema"].schema if str(key) == "profile_name"
                )
                self.assertEqual(profile_key.default(), starter_name)
                preview = await self.hass.config_entries.flow.async_configure(
                    flow_id, {"profile_name": starter_name, "threshold_price_per_kwh": 0.05}
                )
                self.assertEqual(preview["step_id"], "price_preview_threshold_above")
                messages = await async_get_translations(
                    self.hass, language, "config", integrations={DOMAIN}
                )
                rendered = messages[preview_key].format_map(preview["description_placeholders"])
                self.assertIn("0.05 EUR/kWh", rendered)
                self.assertIn("#connect-the-signal-to-an-automation", rendered)
                entities = await async_get_translations(
                    self.hass, language, "entity", integrations={DOMAIN}
                )
                self.assertEqual(entities[f"{attr_key}.above_threshold"], above_limit)
                self.assertTrue(entities[f"{attr_key}.waiting_for_window_prices"])
                self.assertTrue(entities[f"{attr_key}.source_unavailable"])

    async def test_price_profile_preview_creates_usable_binary_sensor(self):
        now = dt_util.utcnow()
        slots = (PriceSlot(now - timedelta(minutes=5), now + timedelta(minutes=10), 0.025),)
        self.snapshot = PriceSnapshot(slots, "EUR", "community_nordpool")
        with self._patch_fetch(), patch(
            "custom_components.electricity_price_scheduler.price_runtime.async_fetch_snapshot",
            new=AsyncMock(return_value=self.snapshot),
        ):
            start = await self.hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
            setup = await self._choose_source(start, scenario="cheap_now")
            self.assertEqual(setup["step_id"], "price_setup")
            self.assertEqual({str(key) for key in setup["data_schema"].schema}, {
                "profile_name", "threshold_price_per_kwh",
            })
            preview = await self.hass.config_entries.flow.async_configure(
                start["flow_id"],
                user_input={"profile_name": "Test Cheap", "threshold_price_per_kwh": 0.05},
            )
            self.assertEqual(preview["step_id"], "price_preview_threshold_active")
            self.assertEqual(preview["description_placeholders"]["current_price"], "0.025")
            self.assertEqual(preview["description_placeholders"]["threshold"], "0.05")
            entry_result = await self.hass.config_entries.flow.async_configure(
                start["flow_id"], user_input={}
            )
            await self.hass.async_block_till_done()
            entry = entry_result["result"]
            self.assertEqual(entry.state, ConfigEntryState.LOADED)
            signal = self.hass.states.get("binary_sensor.test_cheap_use_electricity_now")
            self.assertIsNotNone(signal)
            self.assertEqual(signal.state, "on")
            options = await self.hass.config_entries.options.async_init(entry.entry_id)
            self.assertEqual(options["step_id"], "price_options")
            changed = await self.hass.config_entries.options.async_configure(
                options["flow_id"], user_input={
                    "threshold_price_per_kwh": 0.01,
                    "window_start": "22:00:00",
                    "window_end": "07:00:00",
                    "weekdays": ["mon", "wed"],
                },
            )
            self.assertEqual(changed["type"], FlowResultType.CREATE_ENTRY)
            await self.hass.async_block_till_done()
            self.assertEqual(entry.options["weekdays"], [0, 2])
            self.assertEqual(self.hass.states.get(signal.entity_id).state, "off")

    async def test_threshold_preview_explains_actual_price_above_limit(self):
        now = dt_util.utcnow()
        self.snapshot = PriceSnapshot(
            (PriceSlot(now - timedelta(minutes=5), now + timedelta(minutes=10), 0.08),),
            "EUR", "community_nordpool",
        )
        with self._patch_fetch():
            start = await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
            await self._choose_source(start, scenario="cheap_now")
            preview = await self.hass.config_entries.flow.async_configure(
                start["flow_id"], user_input={
                    "profile_name": "Cheap", "threshold_price_per_kwh": 0.05,
                },
            )
        self.assertEqual(preview["step_id"], "price_preview_threshold_above")
        self.assertEqual(preview["description_placeholders"]["current_price"], "0.08")
        self.assertEqual(preview["description_placeholders"]["threshold"], "0.05")

    async def test_threshold_preview_waits_when_only_future_prices_are_available(self):
        now = dt_util.utcnow()
        self.snapshot = PriceSnapshot(
            (PriceSlot(now + timedelta(minutes=15), now + timedelta(minutes=30), 0.08),),
            "EUR", "community_nordpool",
        )
        with self._patch_fetch():
            start = await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER},
            )
            await self._choose_source(start, scenario="cheap_now")
            preview = await self.hass.config_entries.flow.async_configure(
                start["flow_id"], {"profile_name": "Cheap", "threshold_price_per_kwh": 0.05},
            )
        self.assertEqual(preview["step_id"], "price_preview_waiting")

    async def test_kelvin_temperature_sensor_is_accepted(self):
        self.hass.states.async_set(
            "sensor.outdoor_kelvin",
            "273.15",
            {"unit_of_measurement": "K"},
        )
        self.assertTrue(_temperature_is_valid(self.hass, "sensor.outdoor_kelvin"))

    async def test_missing_unit_metadata_requests_explicit_override(self):
        no_unit_entity = er.async_get(self.hass).async_get_or_create(
            "sensor", "template", "no-unit", suggested_object_id="no_unit"
        )
        self.hass.states.async_set(
            no_unit_entity.entity_id,
            "1.2",
            {"raw_today": [], "currency": "EUR"},
        )
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        form = await self.hass.config_entries.flow.async_configure(result["flow_id"], {
            "scenario": "heating",
            "price_source": "custom_price_sensor",
        })
        form = await self.hass.config_entries.flow.async_configure(form["flow_id"], {
            "price_entity_id": no_unit_entity.entity_id,
        })
        self.assertEqual(form["type"], FlowResultType.FORM)
        self.assertEqual(form["errors"]["unit_override"], "unit_override_required")
        self.assertIn("unit_override", {str(key) for key in form["data_schema"].schema})

    async def test_missing_currency_requires_user_selection(self):
        no_currency_entity = er.async_get(self.hass).async_get_or_create(
            "sensor", "template", "no-currency", suggested_object_id="no_currency"
        )
        self.hass.states.async_set(
            no_currency_entity.entity_id,
            "1.2",
            {"raw_today": [], "unit": "kWh", "price_in_cents": False},
        )
        with patch(
            "custom_components.electricity_price_scheduler.config_flow.async_fetch_snapshot",
            new=AsyncMock(return_value=self.snapshot),
        ) as mock_fetch:
            result = await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
            form = await self.hass.config_entries.flow.async_configure(result["flow_id"], {
                "scenario": "heating",
                "price_source": "custom_price_sensor",
            })
            form = await self.hass.config_entries.flow.async_configure(form["flow_id"], {
                "price_entity_id": no_currency_entity.entity_id,
            })
            self.assertEqual(form["type"], FlowResultType.FORM)
            self.assertEqual(form["errors"]["currency"], "currency_required")
            form = await self.hass.config_entries.flow.async_configure(
                form["flow_id"],
                user_input={
                    "scenario": "heating",
                    "price_source": "custom_price_sensor",
                    "price_entity_id": no_currency_entity.entity_id,
                    "currency": "NOK",
                },
            )
            self.assertEqual(form["step_id"], "custom_price_preview")
            form = await self.hass.config_entries.flow.async_configure(
                form["flow_id"], {"continue": True},
            )
        self.assertEqual(form["step_id"], "settings")
        self.assertEqual(form["description_placeholders"]["currency"], "NOK")
        # No EUR default may be copied to another currency.
        required = next(
            key for key in form["data_schema"].schema if getattr(key, "schema", None) == "min_price_diff_per_kwh"
        )
        self.assertFalse(hasattr(required, "default") and required.default != vol.UNDEFINED)
        self.assertGreaterEqual(mock_fetch.await_count, 1)

    async def test_options_flow_uses_readonly_current_entry_property(self):
        with self._patch_fetch() as _mock_fetch, patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(return_value=self.snapshot),
        ):
            settings = await self._select_source(_mock_fetch)
            created = await self.hass.config_entries.flow.async_configure(
                settings["flow_id"],
                user_input={
                    "profile_name": "Heating",
                    "max_cutoff_minutes": 300,
                    "min_price_diff_per_kwh": 0.03,
                },
            )
            entry = created["result"]
            result = await self.hass.config_entries.options.async_init(entry.entry_id)
            self.assertEqual(result["type"], FlowResultType.FORM)
            keys = {str(key) for key in result["data_schema"].schema}
            self.assertIn("max_periods", keys)
            self.assertNotIn("profile_name", keys)
            saved = await self.hass.config_entries.options.async_configure(
                result["flow_id"],
                user_input={
                    "max_cutoff_minutes": 360,
                    "min_price_diff_per_kwh": 0.05,
                    "max_periods": 6,
                    "multiplier_mode": "fixed",
                    "preheat_factor": 1.5,
                    "recovery_factor": 1.2,
                },
            )
        self.assertEqual(saved["type"], FlowResultType.CREATE_ENTRY)
        self.assertEqual(entry.options["max_cutoff_minutes"], 360)

    async def test_options_reject_more_than_six_and_clear_optional_temperature(self):
        with self._patch_fetch() as _mock_fetch, patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(return_value=self.snapshot),
        ):
            settings = await self._select_source(_mock_fetch)
            created = await self.hass.config_entries.flow.async_configure(
                settings["flow_id"],
                user_input={
                    "profile_name": "Heating",
                    "max_cutoff_minutes": 300,
                    "min_price_diff_per_kwh": 0.03,
                },
            )
            entry = created["result"]
            self.hass.config_entries.async_update_entry(
                entry,
                options={
                    **dict(entry.options),
                    "temperature_entity_id": "sensor.outdoor_temp",
                },
            )
            flow = await self.hass.config_entries.options.async_init(entry.entry_id)
            with self.assertRaises(InvalidData):
                await self.hass.config_entries.options.async_configure(
                    flow["flow_id"],
                    user_input={
                        "max_cutoff_minutes": 300,
                        "min_price_diff_per_kwh": 0.03,
                        "max_periods": 7,
                        "multiplier_mode": "fixed",
                        "preheat_factor": 1.5,
                        "recovery_factor": 1.2,
                    },
                )

            flow = await self.hass.config_entries.options.async_init(entry.entry_id)
            saved = await self.hass.config_entries.options.async_configure(
                flow["flow_id"],
                user_input={
                    "max_cutoff_minutes": 300,
                    "min_price_diff_per_kwh": 0.03,
                    "max_periods": 6,
                    "multiplier_mode": "fixed",
                    "preheat_factor": 1.5,
                    "recovery_factor": 1.2,
                },
            )
        self.assertEqual(saved["type"], FlowResultType.CREATE_ENTRY)
        self.assertIsNone(entry.options["temperature_entity_id"])

    async def test_dynamic_temperature_mode_requires_valid_selected_sensor(self):
        with self._patch_fetch() as _mock_fetch, patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(return_value=self.snapshot),
        ):
            settings = await self._select_source(_mock_fetch)
            created = await self.hass.config_entries.flow.async_configure(
                settings["flow_id"],
                user_input={
                    "profile_name": "Heating",
                    "max_cutoff_minutes": 300,
                    "min_price_diff_per_kwh": 0.03,
                },
            )
            entry = created["result"]
            flow = await self.hass.config_entries.options.async_init(entry.entry_id)
            result = await self.hass.config_entries.options.async_configure(
                flow["flow_id"],
                user_input={
                    "max_cutoff_minutes": 300,
                    "min_price_diff_per_kwh": 0.03,
                    "max_periods": 6,
                    "multiplier_mode": "outdoor_temperature",
                    "preheat_factor": 1.5,
                    "recovery_factor": 1.2,
                },
            )
        self.assertEqual(result["type"], FlowResultType.FORM)
        self.assertEqual(result["errors"]["temperature_entity_id"], "temperature_sensor_required")

    async def test_reconfigure_currency_change_requires_and_saves_new_threshold(self):
        nok_entity = er.async_get(self.hass).async_get_or_create(
            "sensor", "nordpool", "nok-source", suggested_object_id="test_nordpool_nok"
        )
        self.hass.states.async_set(
            nok_entity.entity_id,
            "1.2",
            {
                "raw_today": [],
                "raw_tomorrow": [],
                "currency": "NOK",
                "unit": "kWh",
                "price_in_cents": False,
            },
        )
        with self._patch_fetch() as _mock_fetch, patch(
            "custom_components.electricity_price_scheduler.coordinator.async_fetch_snapshot",
            new=AsyncMock(return_value=self.snapshot),
        ), patch.object(self.hass.config_entries, "async_schedule_reload"):
            settings = await self._select_source(_mock_fetch)
            created = await self.hass.config_entries.flow.async_configure(
                settings["flow_id"],
                user_input={
                    "profile_name": "Heating",
                    "max_cutoff_minutes": 300,
                    "min_price_diff_per_kwh": 0.03,
                },
            )
            entry = created["result"]
            flow = await self.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
            )
            form = await self.hass.config_entries.flow.async_configure(
                flow["flow_id"],
                user_input={"price_source": f"community_nordpool:{nok_entity.entity_id}"},
            )
            self.assertEqual(form["type"], FlowResultType.FORM)
            self.assertEqual(form["errors"]["min_price_diff_per_kwh"], "currency_changed_threshold_required")
            result = await self.hass.config_entries.flow.async_configure(
                form["flow_id"],
                user_input={
                    "price_source": f"community_nordpool:{nok_entity.entity_id}",
                    "min_price_diff_per_kwh": 3.0,
                },
            )
        self.assertEqual(result["type"], FlowResultType.ABORT)
        self.assertEqual(entry.data["currency"], "NOK")
        self.assertEqual(entry.options["min_price_diff_per_kwh"], 3.0)


if __name__ == "__main__":
    unittest.main()
