# Acknowledgments

Thank you to the people who build and document open source energy tools for Home Assistant. These projects helped us explore how a single integration can offer a quick start and give experienced users more control and visibility.

## Scheduling and setup

- **Kotope and contributors to AIO Energy Management** gave us a reference for choosing a price provider and offering continuous or split runs with a minimum run length. [AIO Energy Management](https://github.com/kotope/aio_energy_management)
- **jyourstone and Power Saver contributors** showed how choosing a scheduling strategy first can keep the initial form focused on the settings that apply. [Power Saver](https://github.com/jyourstone/power_saver#setup-wizard)
- **tvofi, strutsfarm, and Heat Pump Cost Optimizer contributors** gave us a reference for a short setup path, a review before saving, and the option to inspect a heating plan before connecting equipment. Its choice between Tibber and a Nord Pool or similar price entity informed our optional custom price-sensor path. [Heat Pump Cost Optimizer](https://github.com/tvofi/heatpump_optimizer)
- **attaxia and Electricity Optimizer contributors** gave us an example of making upcoming runs and their history visible before handing over control. [Electricity Optimizer](https://github.com/attaxia/hass-electricity-optimizer)

## Price sources and automation

- **TheFes and Cheapest Energy Hours contributors** documented configurable source attributes, timestamp keys, and price-value keys for several providers. This informed our documentation of a shared price-series contract for user-selected sensors. [Cheapest Energy Hours](https://github.com/TheFes/cheapest-energy-hours)
- **smefa and EMHASS Companion contributors** gave us a reference for showing price integrations already installed in Home Assistant and offering an advanced adapter for other entity data. [EMHASS Companion setup](https://smefa.github.io/emhass-ha-companion/guide/setup/#2-electricity-prices)
- **The HAEO contributors** provided a reference for using Home Assistant entities as price inputs and exposing plans that existing automations can use. [Home Assistant Energy Optimiser](https://github.com/hass-energy/haeo)
- **The community Nordpool maintainers** provide the price sensor used by our current test setup. Thanks also to the Home Assistant Community forum members who shared examples, asked questions, and helped us understand how the original script works in other homes. [Community Nordpool](https://github.com/custom-components/nordpool) · [Project discussion](https://community.home-assistant.io/t/optimizing-hvac-energy-savings-with-nordpool-15-min-pricing-part-1-3-theory-implementation-and-examples/936741)

- **JaccoR and ENTSO-E Transparency Platform contributors** document and provide the HA price sensor consumed by the ENTSO-E adapter. [ENTSO-E Transparency Platform for Home Assistant](https://github.com/JaccoR/hass-entso-e)
- **BottlecapDave and Home Assistant Octopus Energy contributors** provide the UK rate events and examples used for the Agile adapter. [Octopus Energy electricity rate events](https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/entities/electricity/#current-day-rates)
- **Home Assistant Core contributors** maintain the documented price actions used by the built-in Nord Pool, Tibber and Amber Electric adapters. [Nord Pool](https://www.home-assistant.io/actions/nordpool.get_prices_for_date/) · [Tibber](https://www.home-assistant.io/actions/tibber.get_prices/) · [Amber Electric](https://www.home-assistant.io/actions/amberelectric.get_forecasts/)
- **mampfes and EPEX Spot integration contributors** provide the HA market-price and total-price sensor contracts. The integration lets us reuse aWATTar and Energy-Charts source setup. [EPEX Spot for Home Assistant](https://github.com/mampfes/ha_epex_spot)
- **The evcc community** published an Amber API trace that exposed the one-second offset in retail settlement timestamps. We used the price-only excerpt to add a regression case. [evcc Amber discussion](https://github.com/evcc-io/evcc/discussions/21795)
