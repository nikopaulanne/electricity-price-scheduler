#!/usr/bin/env bash
set -euo pipefail
container_name="nordpool-optimizer-ha-community"
if docker container inspect "$container_name" >/dev/null 2>&1; then
  docker stop "$container_name"
fi
