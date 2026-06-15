#!/usr/bin/env python3
"""BugZapper CLI — flash an ESP8266/ESP8285 firmware .bin over serial.

A cross-platform (Windows / macOS / Linux) twin of flash.sh: same behavior, but
pure-python so it runs anywhere python3 does. esptool + pyserial are bundled in
vendor/, so nothing needs installing.

  python3 flash.py                      # flash the first ./firmware/*.bin
  python3 flash.py -e                   # erase all flash, then write
  python3 flash.py -p COM5 -b 460800    # explicit port + baud (COMx on Windows)
  python3 flash.py -f build/app.bin
  python3 flash.py -l                   # list detected serial ports
"""
import argparse
import glob
import os
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


def build_flash_cmd(esptool, port, baud, mode, erase, fw):
    """esptool write_flash argv. Short flags (-fm/-fs/-e) + write_flash work on
    esptool 4.x and 5.x; --after is omitted because its default is a hard reset
    in both (the long spelling differs: hard_reset vs hard-reset), so the device
    still reboots into the new firmware."""
    cmd = esptool + ["--port", port, "--baud", str(baud),
                     "write_flash", "-fm", mode, "-fs", "detect"]
    if erase:
        cmd.append("-e")
    cmd += ["0x0", fw]
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
        description="Flash an ESP8266/ESP8285 firmware .bin over serial.")
    ap.add_argument("-p", "--port", default=os.environ.get("ESPTOOL_PORT"),
                    help="serial port (default: first detected, or $ESPTOOL_PORT)")
    ap.add_argument("-f", "--file", help="firmware .bin (default: first ./firmware/*.bin)")
    ap.add_argument("-b", "--baud", default="115200",
                    help="baud rate (default: 115200)")
    ap.add_argument("-m", "--mode", default="dio", choices=["dio", "qio", "dout"],
                    help="flash mode (default: dio)")
    ap.add_argument("-e", "--erase", action="store_true",
                    help="erase the whole flash before writing (wipes all data)")
    ap.add_argument("-l", "--list", action="store_true",
                    help="list detected serial ports and exit")
    args = ap.parse_args()

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

    fw = args.file
    if not fw:
        found = sorted(glob.glob(os.path.join("firmware", "*.bin")))
        fw = found[0] if found else None
    if not fw:
        print("Error: no firmware .bin found in ./firmware. Pass one with -f FILE.",
              file=sys.stderr)
        return 1
    if not os.path.isfile(fw):
        print(f"Error: firmware not found: {fw}", file=sys.stderr)
        return 1

    print(f"==> Flashing {fw}")
    print(f"    port={port} baud={args.baud} mode={args.mode} "
          f"erase={'yes' if args.erase else 'no'}")

    cmd = build_flash_cmd(esptool, port, args.baud, args.mode, args.erase, fw)
    rc = subprocess.run(cmd, env=tool_env()).returncode
    if rc == 0:
        print("==> Done. The device has been reset into the new firmware.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
