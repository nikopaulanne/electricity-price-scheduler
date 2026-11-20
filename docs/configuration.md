# Configuration reference

The options available for a profile depend on the scenario and price source you select.

## Profile and price source

Each config entry represents one named profile. Install and configure the price integration in Home Assistant first. Choose a scenario and one source for that profile. Setup suggests the sole compatible source when there is only one. You can choose another source in **Reconfigure**.

| Source | What to choose and what the price represents | Local verification |
| --- | --- | --- |
| Community Nordpool | The price sensor. Its price reflects the sensor's tax and additional cost settings. | Live FI/EUR in Docker. |
| Home Assistant Nord Pool | The configured market area. The public action supplies spot prices. | Live FI/EUR in a separate Home Assistant process. |
| ENTSO-E Transparency Platform | The **Average electricity price** sensor from `JaccoR/hass-entso-e`. It supplies 15- or 60-minute prices and includes any VAT or modifier configured there. | Public DE–LU market prices replayed through the sensor format. Live ENTSO-E access requires a token. |
| EPEX Spot / aWATTar | Set up `mampfes/ha_epex_spot` and select **Market Price** for spot or **Total Price** for the taxes and surcharges configured in that integration. aWATTar is an upstream source for DE and AT. EUR/kWh and GBP/kWh are accepted. | Live HA setup with aWATTar DE/AT and Energy-Charts DE–LU in EUR. GBP format covered by a contract test. |
| Octopus Energy Agile UK | The standard electricity **Current Day Rates** event. Enable it and the matching **Next Day Rates** event in Octopus Energy. Prices use GBP/kWh including VAT. | Public Agile UK tariff rates replayed as HA rate events. |
| Tibber | One configured Tibber account. When it has multiple active homes, enter the home name and verify the price currency. Uses Tibber's tax-inclusive total price. | SEK prices from Tibber’s public developer demo replayed through the HA action format. |
| Amber Electric | The **General** or **Controlled Load** current price sensor, in AUD/kWh. **Cheap now** uses the current sensor to decide whether to run. Forecast prices provide a preview. | Archived real Amber current and forecast prices replayed through HA formats. Five-minute intervals covered by contract tests. Live retail access requires an Amber account. |
| My price sensor | A Home Assistant sensor with Nordpool-compatible `raw_today` data; `raw_tomorrow` is optional. The selected unit and currency apply to the sensor's prices. | The production FI/EUR proxy data has been replayed through the optimizer. A replay does not verify an actual upstream outage. |

Prices are kept in each source's currency. When switching between spot and retail sources, check the displayed current price and your price limit before connecting an automation. Amber's forecast is cut to a maximum of 200 price slots to bound work on small hosts.

### Use your own price sensor

Select **My price sensor** when your price source is a template sensor, a proxy, or another Home Assistant sensor that exposes a Nordpool-compatible price series. This lets you keep using a sensor shared by other automations.

1. In **Price source**, choose **My price sensor**, then select **Submit** to open the sensor picker.
2. Select your sensor and confirm its currency and price unit if prompted. If its series is in euro cents per kWh, select `snt/kWh`.
3. Review the current price in currency per kWh and the end of the last published interval. Keep **Use this price sensor** on and select **Submit**. For an existing profile, open **Reconfigure** to use the same source picker.

The sensor must expose a `raw_today` list and may expose a `raw_tomorrow` list. Each price row contains `start`, `end`, and numeric `value` fields. Select the unit and currency that match the values published by the sensor. Where the value's unit cannot be inferred from its attributes, choose a full unit such as `EUR/kWh`, `EUR/MWh`, or `snt/kWh`. Check whether values already include VAT, grid fees, or other modifiers before comparing them with your price limit.

The source is read locally from Home Assistant state. State or attribute changes on the chosen sensor trigger recalculation. The integration identifies the local dates from interval timestamps, including when today's prices are still in `raw_tomorrow` after midnight. During an upstream outage, it can use intervals the proxy continues to expose for the current and next local day. After Home Assistant restarts, the integration reads the sensor's attributes again. If the source becomes unavailable or its published price coverage ends, the schedule and phase become unavailable.

Use a proxy whose freshness and recovery behavior you understand. Confirm the displayed price, currency, units, and coverage before connecting any schedule to device controls.

## Optimizer options

The heating profile exposes these settings:

| Setting | Default | Current bound / meaning |
| --- | ---: | --- |
| Minimum cutoff duration | 30 minutes | Fixed internal lower bound; not exposed as a user option |
| Maximum cutoff duration | 300 minutes | User option from 30 to 360 minutes in 15-minute increments |
| Minimum price difference | 0.03 EUR/kWh | Required non-negative threshold in selected currency/kWh. The 0.03 default applies to EUR; other currencies have no automatic equivalent/default |
| Maximum periods (`K`) | 6 | UI range 1–6; bounded to protect smaller HA hosts |
| Multiplier mode | Fixed | `fixed` or `outdoor_temperature` in the current core model |
| Preheat factor | 1.5 | Fixed factor from 1 to 3 |
| Recovery factor | 1.2 | Fixed factor from 1 to 3 |
| Outdoor temperature entity | None | Required when outdoor-temperature mode is selected; missing or invalid runtime readings fall back to fixed factors and produce a warning |

`K` is the maximum count of non-overlapping periods selected by the optimizer. The user-facing maximum remains six until runtime measurements support a different limit. The core implementation can accept a larger bound for performance experiments, but that is not a UI or release setting.

The initial flow asks for the price source. It asks for currency or a full-unit override when needed. It then asks for the profile name and heating limits. The profile options expose maximum periods, multiplier mode, preheat and recovery factors, and the heating limits.

### Change price source or currency

Open the integration and select **Reconfigure** from the profile entry's menu. Choose the replacement source, then enter a new threshold if the currency changes and the profile uses one.

If a heating profile uses a **Minimum price difference helper**, disconnect that helper in the profile options before switching currency. Enter a fixed threshold in the new currency during reconfiguration. Reconnect the helper after updating what writes its value and selecting the correct unit for the new source. Confirm the effective value in [Cutoff decision](entities.md#understand-a-decision) before reconnecting equipment control.

## Units and time

The core contract stores prices as the selected currency per kWh and instants as timezone-aware UTC datetimes. Supported source-unit conversions include currency/MWh to currency/kWh (divide by 1000) and cents/kWh to currency/kWh (divide by 100). Currency is never inferred from an entity name, and foreign exchange is never applied.

Negative prices are valid. Non-finite prices, ambiguous units, overlapping conflicting intervals, and naive timestamps without an unambiguous source timezone are rejected. Missing intervals remain missing; the optimizer does not invent prices or plan across a gap.

## Dynamic heating settings

Open the heating profile's options to bind a number helper to the maximum cutoff duration or minimum price difference. Select hours or minutes for duration. For the price difference, select currency units or hundredths of a currency unit per kWh. For example, a helper value of 3.5 in cents/kWh becomes 0.035 EUR/kWh.

The optimizer listens to the helpers and uses their updated values for future planning. Set fixed values for use when a helper becomes unavailable or invalid. Open **Cutoff decision** to see which value was used and why. A valid duration must be 30–360 minutes in 15-minute steps. The optional outdoor temperature setting controls the preheat and recovery factors and can be used together with a duration helper.
