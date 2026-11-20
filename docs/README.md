# Electricity Price Scheduler guides

Start with the [quick start](../README.md#quick-start). The [source matrix](configuration.md#profile-and-price-source) explains which integrations supply the prices and how each path was tested.

Home Assistant displays the integration in English, Finnish, Swedish, or German according to your [language setting](setup.md#interface-language).

## Choose a guide

- [Install from Home Assistant Marketplace](setup.md#marketplace-installation) for the first profile and current release availability. See [local testing](development/local-testing.md) for development builds.
- [Set an electricity price limit or schedule the cheapest hours](scenarios.md) for a switch, appliance cycle, water heater, or EV charger.
- [Plan heating around electricity price spikes](../README.md#heat-pump-scheduling-around-price-spikes) and review the model's preheat, cutoff, and recovery windows.
- [Configuration](configuration.md): input concepts, defaults, bounds, units, and price-source constraints.
- [Entities and data](entities.md): current output contract and timestamp semantics, with local smoke-test results.
- [Migration from the Python Script](migration.md): safe parallel evaluation without replacing legacy entities or automations.
- [Troubleshooting](troubleshooting.md): data validation and source diagnosis.
- [Model and limitations](model.md): 1 kW reference calculations and HVAC assumptions.
- [Local development and tests](development/local-testing.md): container startup and actual HA Python runtime.
- [Python Script v2.0.0](https://github.com/nikopaulanne/electricity-price-scheduler/tree/v2.0.0) for the original script instructions.

- [Acknowledgments](acknowledgments.md): open source projects and contributors that informed the design.

## Support boundary

The older [Home Assistant Community Forum thread](https://community.home-assistant.io/t/optimizing-hvac-energy-savings-with-nordpool-15-min-pricing-part-1-3-theory-implementation-and-examples/936741) explains the legacy approach. These new integration documents are the source of truth for the development build. Neither source should be read as evidence that a release is available.
