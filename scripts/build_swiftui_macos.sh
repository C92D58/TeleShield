#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PACKAGE_DIR="$ROOT_DIR/swiftui"
APP_DIR="$ROOT_DIR/dist/TeleShieldSwiftUI.app"

swift build --package-path "$PACKAGE_DIR" -c release -Xswiftc -gnone -Xswiftc -strict-concurrency=complete -Xswiftc -warnings-as-errors
BIN_DIR="$(swift build --package-path "$PACKAGE_DIR" -c release -Xswiftc -gnone -Xswiftc -strict-concurrency=complete -Xswiftc -warnings-as-errors --show-bin-path)"

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp "$BIN_DIR/TeleShieldApp" "$APP_DIR/Contents/MacOS/TeleShieldApp"
cp "$PACKAGE_DIR/Info.plist" "$APP_DIR/Contents/Info.plist"
chmod 755 "$APP_DIR/Contents/MacOS/TeleShieldApp"

echo "Built $APP_DIR"
