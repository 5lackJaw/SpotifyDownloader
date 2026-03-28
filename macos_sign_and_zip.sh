#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

APP_NAME="${APP_NAME:-SpotifyDownloader}"
APP_PATH="${APP_PATH:-$ROOT_DIR/dist/$APP_NAME.app}"
APPLE_IDENTITY="${APPLE_IDENTITY:-}"
APPLE_TEAM_ID="${APPLE_TEAM_ID:-}"
APPLE_NOTARY_PROFILE="${APPLE_NOTARY_PROFILE:-}"
ZIP_PATH="${ZIP_PATH:-$ROOT_DIR/dist/$APP_NAME-mac.zip}"

if [ ! -d "$APP_PATH" ]; then
  echo "App bundle not found: $APP_PATH" >&2
  exit 1
fi

if [ -z "$APPLE_IDENTITY" ]; then
  echo "Set APPLE_IDENTITY to your Developer ID Application certificate name." >&2
  exit 1
fi

codesign --force --deep --options runtime --sign "$APPLE_IDENTITY" "$APP_PATH"

if [ -n "$APPLE_TEAM_ID" ]; then
  codesign --verify --deep --strict --verbose=2 "$APP_PATH"
fi

ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"

if [ -n "$APPLE_NOTARY_PROFILE" ]; then
  xcrun notarytool submit "$ZIP_PATH" --keychain-profile "$APPLE_NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP_PATH"
fi

echo
echo "Signed app: $APP_PATH"
echo "Archive: $ZIP_PATH"
