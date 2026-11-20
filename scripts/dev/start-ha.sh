#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
container_name="nordpool-optimizer-ha-community"
image="ghcr.io/home-assistant/home-assistant:2026.11.0@sha256:c6c1ca9931036bf3a2c4cc088d8db25dfea3b33935a5ecc83c40349462e2f69e"

"$repo_root/scripts/dev/prepare-ha-community.sh"

if docker container inspect "$container_name" >/dev/null 2>&1; then
  state="$(docker container inspect --format '{{.State.Status}}' "$container_name")"
  if [[ "$state" == "running" ]]; then
    echo "Refreshing source in the running dev container."
  else
    docker start "$container_name"
  fi
else
  docker volume create nordpool-optimizer-ha-community-config >/dev/null
  docker run --detach \
    --name "$container_name" \
    --platform linux/arm64 \
    --network bridge \
    --publish 127.0.0.1:8124:8123 \
    --volume nordpool-optimizer-ha-community-config:/config \
    --env PYTHONDONTWRITEBYTECODE=1 \
    "$image"
fi

"$repo_root/scripts/dev/sync-ha.sh"
docker restart "$container_name" >/dev/null
echo "HA is available at http://127.0.0.1:8124; complete onboarding if this config is new."
