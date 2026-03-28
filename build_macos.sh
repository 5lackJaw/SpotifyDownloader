#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
APP_NAME="${APP_NAME:-SpotifyDownloader}"
FFMPEG_DIR="${FFMPEG_DIR:-}"
ICON_PATH="${ICON_PATH:-}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python interpreter not found: $PYTHON_BIN" >&2
  exit 1
fi

if [ ! -d ".mac-build-venv" ]; then
  "$PYTHON_BIN" -m venv .mac-build-venv
fi

source .mac-build-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-build.txt

BUILD_ARGS=(build_portable.py --onedir --name "$APP_NAME")

if [ -n "$FFMPEG_DIR" ]; then
  BUILD_ARGS+=(--ffmpeg-dir "$FFMPEG_DIR")
fi

if [ -n "$ICON_PATH" ]; then
  BUILD_ARGS+=(--icon "$ICON_PATH")
fi

python "${BUILD_ARGS[@]}"

echo
echo "Build complete."
echo "App bundle: $ROOT_DIR/dist/$APP_NAME.app"
