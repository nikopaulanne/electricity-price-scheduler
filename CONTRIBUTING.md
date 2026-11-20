# Contributing

This repository contains a legacy Python Script and an in-development Home Assistant custom integration. No public integration release or Marketplace listing exists yet. Keep changes for the legacy script separate from the new integration and do not overwrite its entity IDs.

## Local checks

The supported host environment uses Python 3.14 and `uv`:

```sh
uv sync --group dev
uv run ruff check custom_components tests
uv run python -m unittest discover -s tests/core -v
uv run python -m unittest discover -s tests/providers -v
```

To run the existing tests inside the real HA 2026.11.0 Python runtime, start the isolated instance using [local-testing.md](docs/development/local-testing.md), then run:

```sh
scripts/dev/sync-ha.sh
scripts/dev/python-ha.sh -m unittest discover -s /config/dev-tests -p 'test_*.py' -v
scripts/dev/python-ha.sh -m unittest discover -s /config/dev-tests/core -v
scripts/dev/python-ha.sh -m unittest discover -s /config/dev-tests/providers -v
```

HA flow, lifecycle, and entity tests must use the actual supported HA APIs; do not replace `homeassistant` modules with test-only fakes. Provider fixtures and mocked action responses are valuable but do not count as live price-source validation. Report `community fixture`, `community live`, `core action mocked`, or `core live` accurately.

The GitHub validation workflow runs Home Assistant hassfest and HACS validation. Additions to manifests, translations, or repository metadata should pass both checks before review.

## Bug reports

Include:

- Home Assistant and optimizer versions, or commit hash for a local build.
- Provider type and provider version, market area, configured currency, and declared price unit.
- Expected result, observed result, and a short reproducible sequence.
- Relevant redacted log lines and one representative price interval, including the runtime type of timestamp fields when time parsing is involved.
- Whether the report came from fixture tests, mocked action tests, the isolated HA container, or a live price source.

Do not attach access tokens, full configuration backups, unredacted diagnostics, or complete Home Assistant state dumps.

## Pull requests

Keep changes narrowly scoped. Update versioned docs when behavior changes, add regression coverage for data normalization/availability, and explain what was tested. Do not claim that code is Marketplace-available, a live provider is supported, or modeled savings are realized savings without evidence from the corresponding release/test gate.

The optimizer does not control devices. Contributions that add equipment control or automate migration require a separate design review.

If another open source project informed a design decision, link it in the pull request and add a short note to [Acknowledgments](docs/acknowledgments.md). Describe the specific idea that informed the work. For reused code, assets, or documentation, record the upstream path and revision, identify the license, and retain the required attribution and notices with the reused material.

## License

Contributions are made under the MIT License in [LICENSE](LICENSE).
