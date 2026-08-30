#!/usr/bin/env bash
set -euo pipefail

# Launches BugZapper (flashui.py): a single window to pick the serial port /
# firmware / baud / flash mode / erase, flash an ESP8266/ESP8285, and watch the
# serial output — replacing separate PyFlasher + CoolTerm windows.
#
# Usage: ./bugzapper.sh [firmware-dir]
#   firmware-dir   folder to list .bin files from (default: ./firmware, else cwd)
# Env: BUGZAPPER_TITLE, BUGZAPPER_ICON, BUGZAPPER_FW_DIR (see flashui.py).
#
# Requires: a python3 with tkinter.
#   tkinter:  brew install python-tk@3.10   (any python-tk works)
# esptool is bundled (vendor/, pure python), so flashing needs no install;
# a system esptool is used instead if one is found. The serial monitor reads the
# port directly via stty, so pyserial isn't needed there either.

DIR="$(cd "$(dirname "$0")" && pwd)"

# Find a python3 that actually has tkinter (Homebrew's plain python3 usually
# doesn't; python-tk@<ver> provides it). Test by importing, not by version.
# Two passes: prefer a Tk 8.6+ python — Apple's ancient system Tk 8.5
# (/usr/bin/python3) renders a BLANK window on modern macOS — then fall back
# to any tkinter at all.
CANDS=(python3 python3.13 python3.12 python3.11 python3.10 python3.9
       /usr/local/opt/python@3.10/bin/python3.10
       /opt/homebrew/opt/python@3.10/bin/python3.10
       /usr/bin/python3)
for py in "${CANDS[@]}"; do
  command -v "$py" >/dev/null 2>&1 || continue
  if "$py" -c 'import tkinter as t, sys; sys.exit(0 if t.TkVersion >= 8.6 else 1)' >/dev/null 2>&1; then
    exec "$py" "$DIR/flashui.py" "$@"
  fi
done
for py in "${CANDS[@]}"; do   # last resort: old Tk beats no app at all
  command -v "$py" >/dev/null 2>&1 || continue
  if "$py" -c "import tkinter" >/dev/null 2>&1; then
    exec "$py" "$DIR/flashui.py" "$@"
  fi
done

echo "Error: no python3 with tkinter found." >&2
echo "Install it with:  brew install python-tk@3.10" >&2
exit 1
