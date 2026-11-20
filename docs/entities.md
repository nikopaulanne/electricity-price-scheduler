# Entities and data contract

These entities are available in the development build. The version is unpublished, so exact registry IDs or translations may still change before a release. Each profile's unique IDs are based on its config-entry ID, not the editable profile name or source sensor entity ID.

In the 2026-11-05 disposable FI/EUR smoke test, the data-status and phase sensors reported `ready` and `normal`; two periods included UTC-aware timestamps. Changing K from 6 to 1 reduced the selected schedule to one period without changing unique IDs. The modeled-saving diagnostic entity is disabled by default.

## Current output entities

| Output | Meaning |
| --- | --- |
| `Cutoff schedule` sensor | Count of selected periods; its `periods` attribute contains the full schedule |
| `Cutoff phase` sensor | Machine values `normal`, `preheat`, `shutdown`, or `recovery`; `unavailable` when the snapshot is unavailable |
| `Cutoff decision` sensor | Translated decision state and attributes for limits, sources, timing and the active lock |
| `Cutoff active` binary sensor | `on` only during a valid shutdown period, `off` during other valid phases, `unavailable` when schedule data is unavailable |
| `Next cutoff starts` / `Next cutoff ends` sensors | UTC-aware timestamp values for the next shutdown start and recovery start; HA renders them in local time |
| `Price data status` sensor | Diagnostic machine state and error/status attributes |
| `Modeled saving per kW` sensor | Disabled by default; currency/kW estimate for a 1 kW reference load, not a measured household saving |

Each schedule period includes `preheat_start`, `shutdown_start`, `recovery_start`, and `recovery_end` as ISO 8601 timestamps with an explicit UTC offset, plus `cost_saving_per_kw`, `cost_saving_percent`, `shutdown_duration_hours`, `price_difference_per_kwh`, and `minimum_price_difference_per_kwh`. Period windows are half-open and the preheat, shutdown, and recovery durations are equal in the current model. The schedule also exposes `currency`, `provider`, `data_status`, `tomorrow_available`, `warnings`, `last_success`, `calculated_at`, candidate counts, and slot count.

## Data states

The implemented data-status values are `ready`, `waiting_for_prices`, `no_profitable_periods`, `source_unavailable`, and `invalid_data`. A valid price snapshot with no accepted period is distinguishable from missing or invalid prices. An unavailable or invalid source makes the schedule, phase, cutoff-active state, timestamps, and savings unavailable rather than leaving an old cutoff marked active.

HA display time is local to the configured Home Assistant timezone. Automation templates should compare timestamps as datetimes or epoch values, not lexicographically compare localized strings. These attributes are not a stable released API yet; check the exact version before building automations around them.

## Understand a decision

For heating, open **Cutoff decision** on the profile page. It shows whether a phase is planned now, a later cutoff was selected, more prices are needed, or no cutoff qualified. To see the attributes in Home Assistant, open **Developer tools → States** and select the profile's **Cutoff decision** entity. Look for `next_change` and `effective_settings`. The latter tells you the duration and price-difference limits in use and whether each came from a helper, a fixed setting, or a fallback. When `locked` is true, `locked_effective_settings` shows the values that selected the active period. If an older lock has no saved provenance, `locked_settings_known` is false. Open the profile's settings to see the selected helpers and price sensor.

For other scenarios, open **Schedule status** to check the decision and its **Decision reason** attribute. For automations, use the sensor state or read the language-independent `reason_code` attribute. For example, `above_threshold`, `waiting_for_window_prices`, and `source_unavailable` identify distinct outcomes. Use **Developer tools → States** to inspect raw codes and `next_change` for the next planned evaluation. **Current price** uses the same currency per kWh as the threshold. **Running schedule** lists the selected runs, time window, and settings that shaped the plan. The start and end timestamp sensors refer to the same upcoming run. For a run in progress, check `active_period_end` on **Running schedule**.

These entities describe the plan. To verify that a device is running, open its entity or the automation trace.
