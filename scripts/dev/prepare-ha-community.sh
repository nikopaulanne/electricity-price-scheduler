#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work_dir="$repo_root/work/ha-community"
source_dir="$work_dir/source/nordpool"
source_tag="0.0.18"
config_components="$work_dir/config/custom_components"

mkdir -p "$work_dir/source" "$config_components"

if [[ ! -d "$source_dir/.git" ]]; then
  git clone --depth 1 --branch "$source_tag" \
    https://github.com/custom-components/nordpool.git "$source_dir"
else
  checked_out="$(git -C "$source_dir" describe --tags --exact-match 2>/dev/null || true)"
  if [[ "$checked_out" != "$source_tag" ]]; then
    echo "Expected custom-components/nordpool $source_tag; found ${checked_out:-untagged checkout}." >&2
    exit 1
  fi
fi

rm -rf "$config_components/nordpool"
cp -R "$source_dir/custom_components/nordpool" "$config_components/nordpool"
echo "Prepared Nord Pool integration $source_tag in $config_components/nordpool"
