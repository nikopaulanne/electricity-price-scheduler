#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
container_name="nordpool-optimizer-ha-community"
community_integration="$repo_root/work/ha-community/config/custom_components/nordpool"

docker exec "$container_name" mkdir -p /config/custom_components
docker exec "$container_name" rm -rf /config/custom_components/nordpool
docker cp "$community_integration" "$container_name:/config/custom_components/"
# Clear both package paths when refreshing a test container from before the rename.
docker exec "$container_name" rm -rf /config/custom_components/nordpool_cutoff_optimizer
docker exec "$container_name" rm -rf /config/custom_components/electricity_price_scheduler
docker cp "$repo_root/custom_components/electricity_price_scheduler" \
  "$container_name:/config/custom_components/"

docker exec "$container_name" rm -rf /config/dev-tests
if [[ -d "$repo_root/tests" ]]; then
  docker cp "$repo_root/tests" "$container_name:/config/dev-tests"
fi

echo "Synced integration source and tests into $container_name."
