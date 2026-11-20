# Migrate from the v2 Python Script

Start by [installing v3 or later](setup.md#marketplace-installation). The integration creates its own entities and leaves the script, helpers, and automations in place.

## 1. Inventory your existing setup

- Find the price sensor the script actually used. Check `nordpool_entity` on `sensor.nordpool_cutoff_periods_python`. If this points to a template or proxy, check whether it changes the underlying prices and whether it supplies the full price series. In v3.0.1 or later you can keep a compatible proxy by selecting [**My price sensor**](configuration.md#use-your-own-price-sensor).
- Search for the old schedule entity and any template sensors that wrap it. Check automations, scripts, dashboards, helpers, and any external flows for references to those IDs. Include notification templates and custom cards.
- Check which automations **write** the old input helpers, which **run** the script, and which **consume** the result. Save copies of each configuration you plan to change so you can restore it during rollback.

## 2. Set up a separate profile and compare

Keep the existing price integration and legacy control chain active during comparison. In **Settings → Devices & services → Helpers → Create helper**, choose **Electricity Price Scheduler → Heating and heat pump**. Select the same community Nordpool price sensor or compatible proxy used by the script and enter the corresponding settings below. For a local rehearsal, [load the legacy script into the test container](development/local-testing.md#rehearse-migration-from-the-legacy-script).

| Legacy input | V3 setting |
| --- | --- |
| Maximum duration in `input_number.nordpool_price_savings_sequential_hours` | Multiply hours by 60. For example, 3 hours is 180 minutes. V3 accepts 30–360 minutes in 15-minute steps. |
| Minimum difference in `input_number.nordpool_price_savings_minimum_price_difference` | Use currency per kWh. For example, 3.5 euro cents/kWh is 0.035 EUR/kWh. Confirm the old source's unit before converting. |
| Outside temperature used by the script | Select the temperature entity explicitly and choose outdoor temperature mode if you want temperature-based preheat and recovery multipliers. |

If your old automation adjusts the maximum duration or minimum price difference, open the heating profile's options and bind **Maximum duration helper (optional)** and **Minimum price difference helper (optional)**. For helpers containing `3` hours and `3.5` euro cents, select **Hours** and **Minor units per kWh (e.g. cents/kWh)**. Open **Cutoff decision** and check that `effective_settings` shows `max_cutoff_minutes: 180` and `min_price_difference_per_kwh: 0.035` when those helpers are available. If the legacy setup uses the script's built-in fallback, use 300 minutes and 0.030 EUR/kWh as the fixed fallback. Check the legacy output's `multiplier_source`, `preheat_mult`, and `recovery_mult`. For `dynamic_by_temp`, select the same **Outdoor temperature sensor** and verify the **Preheat and recovery model** in options.

Compare both schedules without connecting v3 to equipment control. Refresh the old script after a new price publication so both use the same prices. Compare active and upcoming phase boundaries as timestamps, and repeat around local midnight. V3 uses the source's real interval boundaries, preserves a valid active window, and optimizes within its period limit. Verify differences against the current or locked settings in **Cutoff decision**.

For a restart check, compare **Cutoff decision** before and after restarting during an active phase. The lock can be restored if the profile settings still match and valid prices cover the remaining period. During a source fault, **Cutoff decision** reports `source_unavailable` or `invalid_data`, and the phase, active signal, and schedule become unavailable. Verify that the automation uses the existing heating fallback with manual and grid priorities in force, then reevaluates after recovery. See the [decision attributes](entities.md#understand-a-decision).

## 3. Switch consumers

Find the actual IDs on the v3 profile's device page. Use the [entity contract](entities.md) to choose the replacement for each consumer.

| Legacy use | V3 output |
| --- | --- |
| Period count or full schedule | **Cutoff schedule** and its `periods` attribute |
| Identify preheat or shutdown in a control automation | **Cutoff phase**. Preheat is `preheat` and shutdown is `shutdown`. Use **Cutoff active** if the automation needs only a shutdown signal. Decide explicitly how to handle `recovery` based on your current automation. |
| Show the next cutoff | **Next cutoff starts** and **Next cutoff ends**. A card that needs the current period or all future periods should read the schedule instead. |
| Alert when the old script state becomes `error` | **Price data status**. Treat `source_unavailable` and `invalid_data` as faults. `waiting_for_prices` can have a usable partial schedule, and `no_profitable_periods` is a valid result. |

If a template reads the schedule, compare dates as datetimes or epoch values. Convert UTC to local time before displaying a clock time or deciding whether a period is tomorrow. For example, use `as_local(as_datetime(period.shutdown_start))` after checking the value is present and valid. Legacy string slices such as `[11:16]` display UTC hours on v3 timestamps. Legacy fields such as `details.high_price_values`, `shutdown_duration_text`, and `cost_saving` have no direct equivalent. V3 exposes `cost_saving_per_kw` in currency per kW of reference load.

Switch while the old control is in its normal state. Preserve existing manual overrides, independent control priorities, equipment targets, and normal heating behavior. Explicitly handle `unavailable` / `unknown` in the schedule consumer so a source failure cannot leave an earlier cutoff or preheat command latched. Once the new controller is active, run its normal evaluation once to apply the current phase and overrides. Check startup, source recovery, and every phase transition. Keep only one automation path applying the schedule to equipment.

Where an existing automation handles manual overrides or a grid signal, keep those conditions in the automation and replace only its schedule input. In a dry run with a helper as the output, activate each override during a planned cutoff. Check the trace for the winning branch. Release the override while the phase stays the same and verify that the automation reevaluates immediately. Repeat with an unavailable source, a restored source, and a Home Assistant restart.

Update dashboards and alerts, then disable the legacy script refresh once no live consumer needs it. Leave unrelated price and temperature helpers in place. Check the migrated consumers for remaining references to the old schedule and its wrappers.

## 4. Roll back

Disable the new schedule control first. Restore the saved legacy consumer configurations, re-enable and run the legacy refresh, and confirm it produces a fresh schedule before re-enabling the legacy controller. Run the restored controller's normal evaluation once so it applies the current phase even if the state has already settled. Verify that equipment or control flags match the winning action after applying manual and grid priorities. Keep the legacy script, helpers, and source available until the switch is proven in your installation.

See [Python Script v2.0.0](https://github.com/nikopaulanne/electricity-price-scheduler/tree/v2.0.0) for the original setup.
