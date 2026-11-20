# Local Home Assistant development

This is an isolated development instance for the custom integration. Its clean config lives in the dedicated Docker named volume `nordpool-optimizer-ha-community-config`. The pinned Nordpool source clone and disposable developer files live under the ignored `work/` directory. The container publishes only on `127.0.0.1:8124` and has no connection to a production Home Assistant instance or physical devices.

## Requirements

- Docker Desktop with `linux/arm64` support.
- The pinned image `ghcr.io/home-assistant/home-assistant:2026.11.0@sha256:c6c1ca9931036bf3a2c4cc088d8db25dfea3b33935a5ecc83c40349462e2f69e` (Home Assistant 2026.11.0, Linux arm64, Python 3.14.10).
- Git, for fetching the pinned community Nord Pool integration at tag `0.0.18`.

The development container uses a default bridge network and a dedicated Docker named volume for its clean config. Integration and test files are copied into that container with `docker cp`; no host paths are shared with Docker Desktop. It has no HACS manager. Do not put production credentials or backups in the dev instance.

## Start and stop

```sh
scripts/dev/start-ha.sh
```

Open `http://127.0.0.1:8124`; complete onboarding with a local development account only if this config is new. The config contains the community `nordpool` integration source but has no production credentials. The optimizer and tests are copied into the config volume before the start script's final restart, so HA's integration loader discovers them at startup.

Stop the container with `scripts/dev/stop-ha.sh`; this preserves the local config. Follow logs with `scripts/dev/logs-ha.sh`. After editing integration source, run `scripts/dev/start-ha.sh` to sync and restart the dev instance. To only copy tests/source without restarting, run `scripts/dev/sync-ha.sh` and then restart the named container when HA must reload changed Python files. To reset the instance, run `scripts/dev/stop-ha.sh`, then explicitly remove only the named container and its config volume with `docker rm nordpool-optimizer-ha-community && docker volume rm nordpool-optimizer-ha-community-config`. This deletes the isolated local instance.

## Runtime checks

If this development instance has profiles created before the domain changed from `nordpool_cutoff_optimizer` to `electricity_price_scheduler`, remove those test profiles in **Settings → Devices & services** before syncing. After the restart, recreate them with **Electricity Price Scheduler** and update any test automations that reference the old profile entities.

The HA image includes Python's `unittest` but does not include `pytest`. `scripts/dev/python-ha.sh` runs Python in the actual HA container, so checks use the real Home Assistant 2026.11 runtime rather than mocked HA modules. The project integration is copied to `/config/custom_components/electricity_price_scheduler` and tests to `/config/dev-tests`. For example:

```sh
scripts/dev/python-ha.sh -c 'import homeassistant; print(homeassistant.__path__)'
```

Host-side development dependencies are recorded in `pyproject.toml`. Tests that require Home Assistant fixtures should use the versioned host test environment; the container is for runtime/import and manual config-flow smoke checks.

## Try the switch blueprint in the development instance

Copy the blueprint into the named development container:

```sh
docker exec nordpool-optimizer-ha-community mkdir -p /config/blueprints/automation/electricity_price_scheduler
docker cp blueprints/automation/electricity_price_scheduler/follow_price_profile.yaml nordpool-optimizer-ha-community:/config/blueprints/automation/electricity_price_scheduler/
```

For a disposable test target, create a **Toggle** helper named **Optimizer test load**. Confirm its entity ID is `input_boolean.optimizer_test_load`, or substitute the actual ID in this example. Add this to the development instance's `configuration.yaml` and restart Home Assistant. If the file already has a `template:` section, append the list item there.

```yaml
template:
  - switch:
      - name: Optimizer test switch
        state: "{{ is_state('input_boolean.optimizer_test_load', 'on') }}"
        turn_on:
          - action: input_boolean.turn_on
            target:
              entity_id: input_boolean.optimizer_test_load
        turn_off:
          - action: input_boolean.turn_off
            target:
              entity_id: input_boolean.optimizer_test_load
```

Open **Settings → Automations & scenes → Blueprints** and select **Run a switch with Electricity Price Scheduler**. Choose your profile's **Use electricity now** signal and the virtual **Optimizer test switch**. Save the test automation and use **Run actions** to check the initial state. Use the [scenario guide](../scenarios.md) to stop or remove the automation after testing.

## Rehearse migration from the legacy script

In the test container, add `python_script:` to `/config/configuration.yaml` if it is missing. From the repository root, copy the legacy script and restart the test container:

```sh
docker exec nordpool-optimizer-ha-community mkdir -p /config/python_scripts
docker cp python_scripts/nordpool_cutoff_optimizer.py nordpool-optimizer-ha-community:/config/python_scripts/
docker restart nordpool-optimizer-ha-community
```

Open **Developer tools → Actions**, switch to YAML mode, and run the action below. Replace the sample `np_entity` with your community Nord Pool sensor's entity ID.

```yaml
action: python_script.nordpool_cutoff_optimizer
data:
  np_entity: sensor.nordpool_fi
```

Check `sensor.nordpool_cutoff_periods_python` in **Developer tools → States**. Use the [migration guide](../migration.md) to compare it with a heating profile. Use test helpers for any automation outputs in this rehearsal.
