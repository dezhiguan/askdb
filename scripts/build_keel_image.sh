#!/usr/bin/env bash
set -euo pipefail
askdb_root=$(cd "$(dirname "$0")/.." && pwd)
askdb_sdk_tag=$(cat "$askdb_root/.keel-sdk-version")
if [[ ! "$askdb_sdk_tag" =~ ^sdk-v1\.[0-9]+\.[0-9]+$ ]]; then
  echo 'Expected a formal SDK release in .keel-sdk-version' >&2
  exit 1
fi
askdb_sdk_version=${askdb_sdk_tag#sdk-v}
askdb_wheel="keel_sdk-${askdb_sdk_version}-py3-none-any.whl"
mkdir -p "$askdb_root/wheel"
curl --fail --location --retry 3 \
  "https://github.com/dezhiguan/keel/releases/download/${askdb_sdk_tag}/${askdb_wheel}" \
  -o "$askdb_root/wheel/${askdb_wheel}.download"
mv "$askdb_root/wheel/${askdb_wheel}.download" "$askdb_root/wheel/${askdb_wheel}"
docker build --platform linux/amd64 --build-arg "KEEL_SDK_VERSION=${askdb_sdk_version}" \
  -f "$askdb_root/Dockerfile.keel" -t "${1:?Usage: scripts/build_keel_image.sh registry/askdb:revision}" "$askdb_root"
