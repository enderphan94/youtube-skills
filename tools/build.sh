#!/usr/bin/env bash
# Build dist/youtube-sub.zip, ready to upload to Claude.ai / Claude Desktop
# (Settings > Capabilities > Skills).
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p dist
rm -f dist/*.zip
(cd skills && zip -qr ../dist/youtube-sub.zip youtube-sub -x '*/__pycache__/*' '*.pyc' '*/.DS_Store')
echo "✓ dist/youtube-sub.zip"
