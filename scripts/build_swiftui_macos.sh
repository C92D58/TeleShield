#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

APP_NAME="TeleShieldSwiftUI"
APP_DIR="${ROOT_DIR}/dist/${APP_NAME}.app"
SIDECAR_DIR="${ROOT_DIR}/dist/TeleShieldCore"

rm -rf "$APP_DIR" "$SIDECAR_DIR"

python -m PyInstaller \
  --noconfirm \
  --clean \
  --console \
  --name TeleShieldCore \
  --add-data "build/tesseract-runtime:tesseract-runtime" \
  core_service.py

swift build --package-path swiftui -c release

mkdir -p \
  "$APP_DIR/Contents/MacOS" \
  "$APP_DIR/Contents/Helpers"
cp "swiftui/.build/release/TeleShieldApp" "$APP_DIR/Contents/MacOS/TeleShieldApp"
cp -R "$SIDECAR_DIR" "$APP_DIR/Contents/Helpers/TeleShieldCore"
cp "swiftui/Info.plist" "$APP_DIR/Contents/Info.plist"
chmod 755 "$APP_DIR/Contents/MacOS/TeleShieldApp"
chmod 755 "$APP_DIR/Contents/Helpers/TeleShieldCore/TeleShieldCore"

test -x "$APP_DIR/Contents/MacOS/TeleShieldApp"
test -x "$APP_DIR/Contents/Helpers/TeleShieldCore/TeleShieldCore"

echo "Built $APP_DIR"
du -sh "$APP_DIR"
du -sh "$APP_DIR/Contents/Helpers/TeleShieldCore"
