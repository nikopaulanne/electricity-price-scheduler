# Troubleshooting

Check the profile’s **Schedule status** and **Decision reason** to find out why a signal or schedule is unavailable.

## No periods found

For **Heating and heat pump**, first check that your source has current prices and that **Minimum price difference per kWh** uses the source's currency. The default is `0.03 EUR/kWh` for EUR sources. For other currencies, enter a value in that currency. The default maximum cutoff is 300 minutes and the profile can select up to six periods. **No suitable periods** means the calculation found no qualifying cutoff. **Price source unavailable** means the input needs attention.

## Missing tomorrow prices

Some providers publish tomorrow's prices separately. When **Schedule status** shows **Waiting for prices**, open its **Decision reason**. An overnight schedule can be waiting for the rest of its window. **Cheap now** can be waiting for the current price interval. The profile updates as prices arrive. Heating can still show a partial schedule using the prices it has.

## Above the price limit

When **Cheap now** reports that the price is above the limit, compare **Current price** and your limit in the same unit. The profile is working and its signal is off. If a running schedule reports that a ceiling prevents the plan, check whether you want to keep that ceiling or allow a higher price to meet the required running time.

## Unknown or unavailable values

If the status reports an unavailable source, open the selected source integration and check that it is receiving prices. If it reports invalid price data, check the source unit and dates. The optimizer marks affected outputs unavailable so that a stale cutoff does not appear active. Check your automation's separate response to an unavailable signal.

## Price source missing from the picker

Check that the source integration is configured, loaded, and exposes the expected sensor, market area, or event. For Octopus Agile, enable **Current Day Rates** so it appears in the picker and **Next Day Rates** for schedules that need tomorrow's prices. Amber Electric is available for **Cheap now**. If Tibber asks for a home name, use the exact name shown in Tibber. See the [source matrix](configuration.md#profile-and-price-source) for the exact input expected from each provider.

## Issue #4: invalid time values

The legacy issue concerned raw price entries containing unexpected time value types. In the new provider adapters, supported inputs should be normalized explicitly and unsupported or ambiguous values should fail with a clear data error. This design does not prove that the legacy script is fixed or that every community Nordpool release has been live-tested. When reporting the issue, include the HA version, integration version, Nordpool version, unit, currency, and a redacted example of one raw interval with its Python value type. Do not include tokens or full state dumps.

## Useful development checks

For the local test instance, use `scripts/dev/logs-ha.sh` and run Python tests with `scripts/dev/python-ha.sh -m unittest ...`. The current development container does not include pytest. Test reports must distinguish fixture/mocked behavior from live provider results.
