#!/bin/bash
# Compila o Cutter.app em ./build. Com --install, copia para ~/Applications.
set -euo pipefail
cd "$(dirname "$0")/.."
APP="build/Cutter.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
# binario universal (Apple Silicon + Intel)
TMPBIN="$(mktemp -d)"
for arch in arm64 x86_64; do
  swiftc -O -target "$arch-apple-macos11.0" -o "$TMPBIN/Cutter-$arch" mac/main.swift -framework AppKit -framework WebKit
done
lipo -create "$TMPBIN/Cutter-arm64" "$TMPBIN/Cutter-x86_64" -output "$APP/Contents/MacOS/Cutter"
cp mac/Info.plist "$APP/Contents/Info.plist"
cp app.py index.html "$APP/Contents/Resources/"
if command -v rsvg-convert >/dev/null && command -v iconutil >/dev/null; then
  ICONSET="$(mktemp -d)/AppIcon.iconset"
  mkdir -p "$ICONSET"
  for s in 16 32 128 256 512; do
    rsvg-convert -w $s -h $s icon.svg -o "$ICONSET/icon_${s}x${s}.png"
    rsvg-convert -w $((s*2)) -h $((s*2)) icon.svg -o "$ICONSET/icon_${s}x${s}@2x.png"
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
else
  cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
fi
codesign --force --sign - "$APP" 2>/dev/null
echo "OK: $APP"

if [[ "${1:-}" == "--install" ]]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/Cutter.app"
  cp -R "$APP" "$HOME/Applications/"
  echo "Instalado em ~/Applications/Cutter.app"
fi
