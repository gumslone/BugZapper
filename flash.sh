#!/usr/bin/env bash
set -euo pipefail

# Flashes an ESP8266/ESP8285 firmware .bin over serial, using esptool (the same
# tool NodeMCU PyFlasher drives).
#
# Usage: ./flash.sh [options]
#   -p PORT    serial port (default: first /dev/cu.usbserial* / *.SLAB* found,
#              or $ESPTOOL_PORT)
#   -f FILE    firmware .bin to flash (default: the first ./firmware/*.bin)
#   -b BAUD    baud rate: 9600 57600 74880 115200 230400 460800 921600
#              (default: 115200)
#   -m MODE    flash mode: dio (default) | qio | dout
#   -e         erase the whole flash before writing ("yes, wipes all data")
#   -l         list detected serial ports and exit
#   -h         show this help
#
# Examples:
#   ./flash.sh                          # flash the first ./firmware/*.bin
#   ./flash.sh -e                       # erase all flash, then write
#   ./flash.sh -p /dev/cu.usbserial-110 -b 460800
#   ./flash.sh -f build/app.bin
#
# esptool: a pure-python esptool + pyserial are bundled in vendor/, so no
# install is needed — only python3 on PATH. A system esptool (4.x/5.x) is used
# instead if one is found.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

PORT="${ESPTOOL_PORT:-}"
# default: first .bin in ./firmware (of the current project); -f overrides
FIRMWARE="$(ls firmware/*.bin 2>/dev/null | sort | head -n1 || true)"
BAUD=115200
FLASH_MODE=dio
ERASE=0

# Lists likely USB-serial devices on macOS (cu.usbserial*, cu.SLAB*, cu.wchusb*)
# and Linux (ttyUSB*, ttyACM*).
list_ports() {
  ls /dev/cu.usbserial* /dev/cu.SLAB* /dev/cu.wchusb* \
     /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true
}

while getopts ":p:f:b:m:elh" opt; do
  case "$opt" in
    p) PORT="$OPTARG" ;;
    f) FIRMWARE="$OPTARG" ;;
    b) BAUD="$OPTARG" ;;
    m) FLASH_MODE="$OPTARG" ;;
    e) ERASE=1 ;;
    l) list_ports; exit 0 ;;
    h) sed -n '4,26p' "$0"; exit 0 ;;
    :) echo "Error: -$OPTARG needs an argument" >&2; exit 1 ;;
    \?) echo "Error: unknown option -$OPTARG (try -h)" >&2; exit 1 ;;
  esac
done

case "$FLASH_MODE" in
  dio|qio|dout) ;;
  *) echo "Error: flash mode must be dio, qio or dout" >&2; exit 1 ;;
esac

# Resolve a *working* esptool. Prefer the bundled pure-python esptool in
# vendor/ (no install needed); else fall back to a system esptool. Test by
# actually running it ("version"), not just `command -v`: a broken shebang
# (e.g. a stale pip esptool.py pointing at a removed python) passes a presence
# check but fails to execute.
VENDOR="$SCRIPT_DIR/vendor"
if [ -f "$VENDOR/esptool.py" ] && command -v python3 >/dev/null 2>&1 \
   && PYTHONPATH="$VENDOR" python3 "$VENDOR/esptool.py" version >/dev/null 2>&1; then
  ESPTOOL=(env "PYTHONPATH=$VENDOR" python3 "$VENDOR/esptool.py")
elif esptool version >/dev/null 2>&1; then
  ESPTOOL=(esptool)
elif esptool.py version >/dev/null 2>&1; then
  ESPTOOL=(esptool.py)
elif python3 -m esptool version >/dev/null 2>&1; then
  ESPTOOL=(python3 -m esptool)
else
  echo "Error: a working esptool was not found." >&2
  echo "The bundled esptool (vendor/) needs python3 on PATH;" >&2
  echo "or install one:  brew install esptool   (or: pipx install esptool)" >&2
  exit 1
fi

# Auto-detect the port when none was given.
if [ -z "$PORT" ]; then
  PORT="$(list_ports | head -n1 || true)"
  if [ -z "$PORT" ]; then
    echo "Error: no serial port found. Plug in the device, or pass -p PORT." >&2
    echo "Detected ports:"; list_ports | sed 's/^/  /' || echo "  (none)"
    exit 1
  fi
  echo "==> Auto-selected serial port: $PORT"
fi

if [ -z "$FIRMWARE" ]; then
  echo "Error: no firmware .bin found in ./firmware. Pass one with -f FILE." >&2
  exit 1
fi
if [ ! -f "$FIRMWARE" ]; then
  echo "Error: firmware not found: $FIRMWARE" >&2
  exit 1
fi

echo "==> Flashing $FIRMWARE"
echo "    port=$PORT baud=$BAUD mode=$FLASH_MODE erase=$([ "$ERASE" = 1 ] && echo yes || echo no)"

# Short flags (-fm/-fs/-e) and the write_flash subcommand work on both esptool
# 4.x and 5.x. --after is omitted on purpose: its default is a hard reset in
# both versions (the long value spelling differs: hard_reset vs hard-reset), so
# the device still reboots straight into the freshly flashed firmware.
WRITE_ARGS=(-fm "$FLASH_MODE" -fs detect)
[ "$ERASE" = 1 ] && WRITE_ARGS+=(-e)

"${ESPTOOL[@]}" --port "$PORT" --baud "$BAUD" \
  write_flash "${WRITE_ARGS[@]}" 0x0 "$FIRMWARE"

echo "==> Done. The device has been reset into the new firmware."
