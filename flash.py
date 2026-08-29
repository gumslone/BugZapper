#!/usr/bin/env python3
"""BugZapper CLI — flash ESP8266/ESP8285/ESP32 firmware .bin(s) over serial.

A cross-platform (Windows / macOS / Linux) twin of flash.sh: same behavior, but
pure-python so it runs anywhere python3 does. esptool + pyserial are bundled in
vendor/, so nothing needs installing.

  python3 flash.py                      # flash the first ./firmware/*.bin
  python3 flash.py -e                   # erase all flash, then write
  python3 flash.py -p COM5 -b 460800    # explicit port + baud (COMx on Windows)
  python3 flash.py -f build/app.bin
  python3 flash.py -l                   # list detected serial ports
  python3 flash.py -f 0x1000:boot.bin -f 0x8000:partitions.bin \\
                   -f 0x10000:app.bin   # ESP32: multi-part image at offsets
  python3 flash.py --scan build/        # ESP32: scan a build folder & flash it
  python3 flash.py -i                   # chip info (type, MAC, flash size)
"""
import argparse
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor")
if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)
from serial.tools.list_ports import comports  # noqa: E402  (after sys.path setup)


def tool_env():
    """Env for the bundled tools: vendored pyserial on PYTHONPATH."""
    pp = VENDOR
    if os.environ.get("PYTHONPATH"):
        pp += os.pathsep + os.environ["PYTHONPATH"]
    return dict(os.environ, PYTHONPATH=pp)


def list_ports():
    """Serial ports across all platforms (COMx on Windows, /dev/* elsewhere)."""
    return sorted(p.device for p in comports())


def files_in_folder(folder, exts):
    """Top-level files in folder matching the given extensions. exts is a string
    of space/comma-separated extensions in any form (lua, .lua, *.lua); empty or
    a '*'/'all' token means every file. Not recursive — the NodeMCU filesystem is
    flat, so pulling from subfolders would just flatten and collide."""
    raw = [t.strip().lower() for t in re.split(r"[,\s]+", exts or "") if t.strip()]
    # glob.escape the folder: a literal [ * ? in the chosen directory's path
    # would otherwise be read as a glob pattern and match nothing.
    files = sorted(p for p in glob.glob(os.path.join(glob.escape(folder), "*"))
                   if os.path.isfile(p))
    if not raw or "*" in raw or "*.*" in raw or "all" in raw:
        return files
    tokens = [t.lstrip("*").lstrip(".") for t in raw]  # *.lua / .lua / lua -> lua
    return [p for p in files
            if os.path.splitext(p)[1].lstrip(".").lower() in tokens]


# Conventional ESP32 image layout, keyed by tell-tale part names. Used only for
# hints and folder scanning — never to silently override an offset the user
# chose. Deliberately no entry for the app image: names like "app"/"firmware"
# are too generic (an ESP8266 build called myapp.bin must not get an ESP32 hint).
ESP32_PART_OFFSETS = (("bootloader", "0x1000"), ("partition", "0x8000"),
                      ("boot_app0", "0xe000"), ("ota_data", "0xe000"))


def suggest_offset(filename):
    """The conventional ESP32 offset for a part named like filename, or None
    when the name isn't a recognizable part."""
    name = os.path.basename(filename).lower()
    for key, off in ESP32_PART_OFFSETS:
        if key in name:
            return off
    return None


def scan_esp32_folder(folder):
    """Map a build folder's .bin files onto the conventional ESP32 layout.

    Returns (parts, leftovers): parts is [(offset, path)] sorted by offset —
    every recognizable part (bootloader/partition/boot_app0/ota_data) at its
    conventional offset, plus the app at 0x10000 when that guess is safe:
    exactly one unrecognized .bin, next to at least one recognized part.
    Everything else (no recognizable name, or a second file for an
    already-taken offset) goes to leftovers for the user to place manually."""
    placed = {}      # offset -> path
    leftovers = []   # duplicates for a taken offset
    unknown = []     # no recognizable part name
    for p in files_in_folder(folder, "bin"):
        off = suggest_offset(p)
        if off is None:
            unknown.append(p)
        elif off in placed:
            leftovers.append(p)
        else:
            placed[off] = p
    if placed and len(unknown) == 1 and "0x10000" not in placed:
        placed["0x10000"] = unknown.pop(0)
    parts = sorted(placed.items(), key=lambda t: int(t[0], 0))
    return parts, unknown + leftovers


# A -f spec's offset prefix: 0x-hex or decimal. Anything else (a Windows drive
# letter like C:, a plain path) is not an offset.
OFFSET_RE = re.compile(r"^(0[xX][0-9a-fA-F]+|\d+)$")


def parse_part(spec):
    """Split a -f spec into (offset, file): '0x1000:boot.bin' -> ('0x1000',
    'boot.bin'); a plain path (including Windows 'C:\\...' — a drive letter is
    not a number) flashes at 0x0."""
    head, sep, tail = spec.partition(":")
    if sep and tail and OFFSET_RE.match(head):
        return head, tail
    return "0x0", spec


def build_flash_cmd(esptool, port, baud, mode, erase, parts):
    """esptool write_flash argv for one or more (offset, file) parts. Short
    flags (-fm/-fs/-e) + write_flash work on esptool 4.x and 5.x; --after is
    omitted because its default is a hard reset in both (the long spelling
    differs: hard_reset vs hard-reset), so the device still reboots into the
    new firmware."""
    cmd = esptool + ["--port", port, "--baud", str(baud),
                     "write_flash", "-fm", mode, "-fs", "detect"]
    if erase:
        cmd.append("-e")
    for off, path in parts:
        cmd += [off, path]
    return cmd


def resolve_esptool():
    """A working esptool argv prefix, or None. Prefers the bundled pure-python
    esptool (no install); falls back to a system one. Tested by executing
    'version' (a stale esptool.py with a dead shebang passes a presence check
    but fails to run)."""
    bundled = os.path.join(VENDOR, "esptool.py")
    candidates = []
    if os.path.isfile(bundled):
        candidates.append([sys.executable, bundled])
    candidates += [["esptool"], ["esptool.py"], [sys.executable, "-m", "esptool"]]
    for cand in candidates:
        try:
            if subprocess.run(cand + ["version"], capture_output=True,
                              env=tool_env()).returncode == 0:
                return cand
        except (FileNotFoundError, OSError):
            continue
    return None


def main():
    ap = argparse.ArgumentParser(
        description="Flash ESP8266/ESP8285/ESP32 firmware .bin(s) over serial.")
    ap.add_argument("-p", "--port", default=os.environ.get("ESPTOOL_PORT"),
                    help="serial port (default: first detected, or $ESPTOOL_PORT)")
    ap.add_argument("-f", "--file", action="append", metavar="[OFFSET:]FILE",
                    help="firmware .bin, optionally prefixed with its flash "
                         "offset (default 0x0). Repeat for ESP32 multi-part "
                         "images. Default: first ./firmware/*.bin")
    ap.add_argument("-b", "--baud", default="115200",
                    help="baud rate (default: 115200)")
    ap.add_argument("-m", "--mode", default="dio", choices=["dio", "qio", "dout"],
                    help="flash mode (default: dio)")
    ap.add_argument("-e", "--erase", action="store_true",
                    help="erase the whole flash before writing (wipes all data)")
    ap.add_argument("-l", "--list", action="store_true",
                    help="list detected serial ports and exit")
    ap.add_argument("--scan", metavar="FOLDER",
                    help="scan an ESP32 build folder and flash its recognizable "
                         "parts (bootloader/partitions/boot_app0 + the app)")
    ap.add_argument("-i", "--chip-info", action="store_true",
                    help="probe the chip (type, MAC, flash size) and exit")
    args = ap.parse_args()

    if args.scan and args.file:
        ap.error("--scan and -f are mutually exclusive")

    ports = list_ports()
    if args.list:
        print("\n".join(ports) if ports else "(no serial ports detected)")
        return 0

    esptool = resolve_esptool()
    if not esptool:
        print("Error: a working esptool was not found. The bundled esptool "
              "(vendor/) needs python3; or install one: pipx install esptool",
              file=sys.stderr)
        return 1

    port = args.port
    if not port:
        port = ports[0] if ports else None
        if not port:
            print("Error: no serial port found. Plug in the device, or pass "
                  "-p PORT.", file=sys.stderr)
            return 1
        print(f"==> Auto-selected serial port: {port}")

    if args.chip_info:
        # read-only probe: chip type, MAC, flash manufacturer/size
        return subprocess.run(esptool + ["--port", port, "--baud",
                                         str(args.baud), "flash_id"],
                              env=tool_env()).returncode

    if args.scan:
        parts, leftovers = scan_esp32_folder(args.scan)
        if not parts:
            print(f"Error: no recognizable ESP32 parts in {args.scan}",
                  file=sys.stderr)
            return 1
        for p in leftovers:
            print(f"    (skipping {os.path.basename(p)} — flash it separately "
                  "with -f OFFSET:FILE if it belongs to the image)")
    elif args.file:
        # lowest offset first, the conventional bootloader→app order
        parts = sorted((parse_part(s) for s in args.file),
                       key=lambda t: int(t[0], 0))
    else:
        found = sorted(glob.glob(os.path.join("firmware", "*.bin")))
        parts = [("0x0", found[0])] if found else []
    if not parts:
        print("Error: no firmware .bin found in ./firmware. Pass one with -f FILE.",
              file=sys.stderr)
        return 1
    for _, path in parts:
        if not os.path.isfile(path):
            print(f"Error: firmware not found: {path}", file=sys.stderr)
            return 1

    print("==> Flashing " + ", ".join(f"{p} @ {o}" for o, p in parts))
    print(f"    port={port} baud={args.baud} mode={args.mode} "
          f"erase={'yes' if args.erase else 'no'}")

    cmd = build_flash_cmd(esptool, port, args.baud, args.mode, args.erase, parts)
    rc = subprocess.run(cmd, env=tool_env()).returncode
    if rc == 0:
        print("==> Done. The device has been reset into the new firmware.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
