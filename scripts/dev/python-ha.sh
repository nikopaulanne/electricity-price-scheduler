#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "Usage: scripts/dev/python-ha.sh <python arguments>" >&2
  exit 2
fi

docker exec -i \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --env PYTHONPATH=/config \
  nordpool-optimizer-ha-community \
  python "$@"
