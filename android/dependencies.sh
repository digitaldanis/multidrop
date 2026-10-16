#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p dependencies
verify_download() {
  local url="$1" target="$2" checksum="$3"
  if [ ! -f "$target" ]; then curl --fail --location --silent --show-error "$url" -o "$target"; fi
  printf '%s  %s\n' "$checksum" "$target" | sha256sum --check --status
}
verify_download 'https://repo.maven.apache.org/maven2/com/google/zxing/core/3.5.3/core-3.5.3.jar' 'dependencies/zxing-core-3.5.3.jar' '8d8064c1636fdaef7189dd9055c7d59950a8940a12f2293956446ec3c109fd82'
if [ "${1:-}" = '--test' ]; then
  verify_download 'https://repo.maven.apache.org/maven2/org/json/json/20240303/json-20240303.jar' 'dependencies/json-20240303.jar' '3cf6cd6892e32e2b4c1c39e0f52f5248a2f5b37646fdfbb79a66b46b618414ed'
fi
