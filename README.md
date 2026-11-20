# Electricity Price Scheduler for Home Assistant

Find the cheapest hours to run a water heater or charge an EV, or turn on a switch when the electricity price drops below your limit. For electric heating and heat pumps, plan when to preheat, pause during a price spike, and resume. Use the schedules and signals in your Home Assistant automations.

> **In development.** The integration is awaiting its first GitHub release. To try it today, use the [isolated test installation](docs/setup.md#local-development-instance).

See the [price source guide](docs/configuration.md#profile-and-price-source) for Nord Pool, Tibber, Octopus Agile, EPEX Spot through sources such as aWATTar, ENTSO-E, and Amber Electric. Scheduling uses the intervals supplied by your source, including 15-minute prices. The guide lists which scenarios each source supports.

## Quick start

1. [Install Electricity Price Scheduler](docs/setup.md#marketplace-installation) and check your price source.
2. [Create a Cheap now profile and verify its price signal](docs/setup.md#create-your-first-profile).
3. Try the [switch example](docs/scenarios.md#first-example-with-a-switch) or choose a [scheduling scenario](docs/scenarios.md) for a water heater, EV charger, or heat pump.

If you already loaded the code in a [local test instance](docs/development/local-testing.md), start at [Create your first profile](docs/setup.md#create-your-first-profile).

**Already using the Python Script?** Follow the [migration guide](docs/migration.md) to compare the two schedules in parallel, preserve your existing control priorities, and keep a way back.

<!-- ha-marketplace:exclude-start -->
## Installation

[![Open Electricity Price Scheduler in Home Assistant Marketplace](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=nikopaulanne&repository=electricity-price-scheduler&category=integration)

Use the badge with Home Assistant 2026.11 or later once the first integration release is published. For manual steps, see [Marketplace installation](docs/setup.md#marketplace-installation).
<!-- ha-marketplace:exclude-end -->

## Heat pump scheduling around price spikes

The optimizer compares equal-duration preheat, shutdown, and recovery windows using normalized price slots. It selects at most six non-overlapping periods in the current UI design. The default maximum cutoff is 300 minutes. The minimum price difference defaults to `0.03 EUR/kWh`; a non-EUR source requires a threshold in that selected currency rather than copying the EUR default. These settings describe a model, not a guaranteed bill saving.

Modeled savings use a 1 kW reference load. They are not measured or predicted household-euro savings. See [Model and limitations](docs/model.md) for the assumptions.

## Version guide

- Existing users of the Python Script can use the [v2.0.0 guide](https://github.com/nikopaulanne/electricity-price-scheduler/tree/v2.0.0) and the [migration guide](docs/migration.md).
- The current development code is available only in this repository. Both Nord Pool paths have local FI/EUR live-data checks. EPEX Spot and aWATTar also passed isolated live HA checks. For the other price sources, see the source matrix for the data and test method used. See [setup and status](docs/setup.md).
- The Home Assistant Community Forum thread documents the earlier Python Script solution: [Optimizing HVAC energy savings with Nord Pool 15-minute pricing](https://community.home-assistant.io/t/optimizing-hvac-energy-savings-with-nordpool-15-min-pricing-part-1-3-theory-implementation-and-examples/936741).

## Documentation

Start at the [documentation index](docs/README.md) for setup, configuration, entities, migration, troubleshooting, and model notes.

<!-- ha-marketplace:exclude-start -->
## Development

The isolated HA 2026.11 development environment and local test commands are documented in [local testing](docs/development/local-testing.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the supported local checks and the information useful in a bug report.
<!-- ha-marketplace:exclude-end -->

## Safety and responsibility

The integration only creates schedule data. Users decide whether and how to connect that data to automations. Validate any separate automation against the equipment manufacturer’s requirements, household comfort, freeze protection, and local safety needs before enabling it.

For help with your setup, check [Troubleshooting](docs/troubleshooting.md). Report reproducible problems on [GitHub Issues](https://github.com/nikopaulanne/electricity-price-scheduler/issues).

## Acknowledgments

Thanks to the maintainers of [Power Saver](https://github.com/jyourstone/power_saver), [AIO Energy Management](https://github.com/kotope/aio_energy_management), [Heat Pump Cost Optimizer](https://github.com/tvofi/heatpump_optimizer), and other open source projects whose work informed our design. See [what we learned from each project](docs/acknowledgments.md).

## License

MIT; see [LICENSE](LICENSE).
