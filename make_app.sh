#!/usr/bin/env bash
set -euo pipefail

# Builds a double-clickable macOS app: dist/BugZapper.app
#
# The bundle carries its own copy of flashui.py, flash.py, vendor/ and the
# icon, so it can be dragged to /Applications (or the Dock) and launched from
# Finder — with a proper name and Dock icon instead of the Python rocket.
# Like the rest of BugZapper it uses a system python3 with tkinter at runtime;
# nothing is compiled and no pip install is needed to build or run.
#
# Usage: ./make_app.sh [output.app]     (default: ./dist/BugZapper.app)
#
# Notes:
# - Built locally, the app has no quarantine flag, so it opens on double-click.
#   If you zip it and give it to someone, Gatekeeper will demand right-click →
#   Open the first time (it's unsigned). For a signed/notarized or fully
#   self-contained bundle (Python included), use PyInstaller instead.
# - The GUI remembers settings in ~/.config/bugzapper/ as usual; the firmware
#   list starts empty when launched from Finder (no project cwd) — use Browse….

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="${1:-$DIR/dist/BugZapper.app}"
NAME="$(basename "${APP%.app}")"

command -v iconutil >/dev/null || { echo "Error: iconutil not found (this script is macOS-only)" >&2; exit 1; }

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ---- resources: the app's own copy of the flasher ---------------------------
cp "$DIR/flashui.py" "$DIR/flash.py" "$DIR/icon.png" "$APP/Contents/Resources/"
cp -R "$DIR/vendor" "$APP/Contents/Resources/vendor"
find "$APP/Contents/Resources/vendor" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

# ---- icon: icon.png (256px) -> icon.icns ------------------------------------
ICONSET="$(mktemp -d)/icon.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256; do
  sips -z "$s" "$s" "$DIR/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
done
sips -z 32 32   "$DIR/icon.png" --out "$ICONSET/icon_16x16@2x.png"  >/dev/null
sips -z 64 64   "$DIR/icon.png" --out "$ICONSET/icon_32x32@2x.png"  >/dev/null
sips -z 256 256 "$DIR/icon.png" --out "$ICONSET/icon_128x128@2x.png" >/dev/null
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns"

# ---- Info.plist -------------------------------------------------------------
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key>            <string>$NAME</string>
	<key>CFBundleDisplayName</key>     <string>$NAME</string>
	<key>CFBundleIdentifier</key>      <string>com.gumslone.bugzapper</string>
	<key>CFBundleVersion</key>         <string>1.0</string>
	<key>CFBundleShortVersionString</key> <string>1.0</string>
	<key>CFBundlePackageType</key>     <string>APPL</string>
	<key>CFBundleExecutable</key>      <string>launcher</string>
	<key>CFBundleIconFile</key>        <string>icon</string>
	<key>NSHighResolutionCapable</key> <true/>
</dict>
</plist>
PLIST

# ---- launcher ---------------------------------------------------------------
# Finder launches with a minimal PATH (no Homebrew), so absolute candidates
# matter. Same probe idea as bugzapper.sh: pick the first python3 that can
# actually import tkinter.
cat > "$APP/Contents/MacOS/launcher" <<'LAUNCH'
#!/bin/bash
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
for py in python3 /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 \
          /opt/homebrew/opt/python-tk@3.13/libexec/bin/python3 \
          /usr/local/opt/python-tk@3.13/libexec/bin/python3 \
          python3.13 python3.12 python3.11 python3.10; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c "import tkinter" >/dev/null 2>&1; then
    exec "$py" "$RES/flashui.py"
  fi
done
osascript -e 'display alert "BugZapper" message "No python3 with tkinter found. Install it with:  brew install python-tk"' >/dev/null 2>&1
exit 1
LAUNCH
chmod +x "$APP/Contents/MacOS/launcher"

plutil -lint "$APP/Contents/Info.plist" >/dev/null
echo "==> Built $APP"
echo "    Drag it to /Applications or the Dock; double-click to launch."
