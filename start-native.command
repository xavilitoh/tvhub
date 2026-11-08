#!/bin/bash
# Run TVHub natively on macOS (no Docker). Recommended when controlling Android TVs,
# since Docker Desktop on Mac cannot route ADB connections through the host's LAN IP.
set -e
cd "$(dirname "$0")"

# adb
if ! command -v adb >/dev/null 2>&1; then
  echo "adb not found. Install it with: brew install android-platform-tools"
  exit 1
fi

# Python venv
if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi
source .venv/bin/activate

# Dependencies
pip install --quiet -r requirements.txt

# Data directory (same as Docker volume, but local)
export DATA_DIR="$HOME/.tvhub"
mkdir -p "$DATA_DIR"

echo
echo "TVHub (native) → http://localhost:8080"
echo "Data: $DATA_DIR    Stop: Ctrl+C"
echo
sleep 1
open "http://localhost:8080" 2>/dev/null || true

python3 app/server.py
