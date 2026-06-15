#!/bin/bash
#
# Build TranslateAI.app — a native, double-clickable macOS app bundle that
# launches TranslateAI. Run this once (double-click it in Finder); afterwards
# you can launch TranslateAI.app from Finder, the Dock, or Launchpad.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$DIR/TranslateAI.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>          <string>TranslateAI</string>
  <key>CFBundleDisplayName</key>   <string>TranslateAI</string>
  <key>CFBundleIdentifier</key>    <string>co.d8s.translateai</string>
  <key>CFBundleVersion</key>       <string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key>   <string>APPL</string>
  <key>CFBundleExecutable</key>    <string>TranslateAI</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key>
  <string>TranslateAI listens to your microphone to translate speech in real time.</string>
</dict>
</plist>
PLIST

# The bundle's executable just hands off to launch.command, so the app always
# runs the current code in this folder.
cat > "$APP/Contents/MacOS/TranslateAI" <<LAUNCH
#!/bin/bash
exec "$DIR/launch.command"
LAUNCH

chmod +x "$APP/Contents/MacOS/TranslateAI"
chmod +x "$DIR/launch.command"

echo "Built: $APP"
open -R "$APP"   # reveal it in Finder
