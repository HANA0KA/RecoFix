#!/bin/bash
# Build dist/RecoFix.app: native Swift shell + the Python engine in Resources.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
APP="$ROOT/dist/RecoFix.app"
VERSION=$(python3 -c "import re;print(re.search(r'VERSION = \"(.+?)\"', open('recofix/server.py').read()).group(1))")
rm -rf "$ROOT/build/icon.iconset"
if [ -e "$APP" ] && ! rm -rf "$APP" 2>/dev/null; then
  # an older build ran its engine as root and left root-owned caches inside the bundle
  OLD="$ROOT/build/stale-$(date +%Y%m%d%H%M%S).app"
  mkdir -p "$ROOT/build" && mv "$APP" "$OLD"
  echo "Aviso: la app anterior tiene archivos de root; movida a $OLD (bórrala con: sudo rm -rf \"$OLD\")"
fi
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$ROOT/build"

swiftc -O -swift-version 5 app/main.swift -o "$APP/Contents/MacOS/RecoFix" -framework Cocoa -framework WebKit

rsync -a --exclude '__pycache__' --exclude '*.pyc' recofix "$APP/Contents/Resources/"

# icon: render the SVG with Quick Look, then build an .icns
qlmanage -t -s 1024 -o "$ROOT/build" recofix/web/icon.svg >/dev/null 2>&1 || true
if [ -f "$ROOT/build/icon.svg.png" ]; then
  mkdir -p "$ROOT/build/icon.iconset"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$ROOT/build/icon.svg.png" --out "$ROOT/build/icon.iconset/icon_${s}x${s}.png" >/dev/null
    d=$((s * 2)); sips -z $d $d "$ROOT/build/icon.svg.png" --out "$ROOT/build/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$ROOT/build/icon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>RecoFix</string>
  <key>CFBundleDisplayName</key><string>RecoFix</string>
  <key>CFBundleIdentifier</key><string>com.recofix.app</string>
  <key>CFBundleExecutable</key><string>RecoFix</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleDevelopmentRegion</key><string>es</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSAppleEventsUsageDescription</key><string>RecoFix pide permisos de administrador para leer discos en modo de solo lectura.</string>
  <key>NSHumanReadableCopyright</key><string>RecoFix</string>
</dict>
</plist>
PLIST

codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
# make Finder and the Dock pick up the icon of the rebuilt bundle instead of showing the cached (or generic) one
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" >/dev/null 2>&1 || true
echo "Construido: $APP"
