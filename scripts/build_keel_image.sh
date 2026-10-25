#!/usr/bin/env bash
set -euo pipefail
askdb_root=$(cd "$(dirname "$0")/.." && pwd)
askdb_sdk_ref=$(cat "$askdb_root/.keel-sdk-ref")
if [[ ! "$askdb_sdk_ref" =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Expected a full Keel source revision in .keel-sdk-ref' >&2
  exit 1
fi
askdb_build_dir=$(mktemp -d /tmp/askdb-keel-build.XXXXXX)
trap 'rm -rf "$askdb_build_dir"' EXIT
git -C "$askdb_build_dir" init -q
git -C "$askdb_build_dir" remote add origin https://github.com/dezhiguan/keel.git
git -C "$askdb_build_dir" fetch --depth 1 origin "$askdb_sdk_ref"
git -C "$askdb_build_dir" checkout --detach FETCH_HEAD
mkdir -p "$askdb_root/wheel"
uv build --wheel --out-dir "$askdb_build_dir/wheels" "$askdb_build_dir/sdk-python"
rm -f "$askdb_root"/wheel/keel_sdk-*.whl
cp "$askdb_build_dir"/wheels/keel_sdk-*.whl "$askdb_root/wheel/"
docker build --platform linux/amd64 -f "$askdb_root/Dockerfile.keel" -t "${1:?Usage: scripts/build_keel_image.sh registry/askdb:revision}" "$askdb_root"
