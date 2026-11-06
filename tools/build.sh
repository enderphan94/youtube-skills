#!/usr/bin/env bash
# Sync the shared fetcher into every skill, then build one upload-ready zip per
# skill in dist/ (for Claude.ai / Claude Desktop: Settings > Capabilities > Skills).
#
#   ./tools/build.sh          sync + zip
#   ./tools/build.sh --check  fail if the copies have drifted (used by tests/CI)
set -euo pipefail
cd "$(dirname "$0")/.."

CANONICAL="skills/youtube-sub/scripts/fetch_transcript.py"
COPIES=("skills/youtube-sum/scripts/fetch_transcript.py")

if [[ "${1:-}" == "--check" ]]; then
  for c in "${COPIES[@]}"; do
    cmp -s "$CANONICAL" "$c" || { echo "✗ $c differs from $CANONICAL (run ./tools/build.sh)"; exit 1; }
  done
  echo "✓ shared scripts in sync"
  exit 0
fi

for c in "${COPIES[@]}"; do cp "$CANONICAL" "$c"; done
echo "✓ synced fetch_transcript.py into ${#COPIES[@]} skill(s)"

mkdir -p dist
for dir in skills/*/; do
  name="$(basename "$dir")"
  rm -f "dist/$name.zip"
  (cd skills && zip -qr "../dist/$name.zip" "$name" -x '*/__pycache__/*' '*.pyc' '*/.DS_Store')
  echo "✓ dist/$name.zip"
done
