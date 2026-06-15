# ⚡ BugZapper

A tiny, dependency-light flasher for **ESP8266 / ESP8285** boards — a GUI **and**
a CLI, on **Windows, macOS and Linux** — that flashes firmware *and* shows the
serial output in one place, so you don't need separate
[NodeMCU PyFlasher](https://github.com/marcelstoer/nodemcu-pyflasher)
+ [CoolTerm](https://freeware.the-meiers.org/) windows.

- **No install needed** — pure-python `esptool` + `pyserial` are bundled in
  [`vendor/`](vendor); only `python3` is required (plus Tk for the GUI). A
  system `esptool` is used instead if one is on `PATH`.
- **Cross-platform** — port detection and the serial monitor use the bundled
  `pyserial`, so the same code runs on Windows (`COMx`), macOS and Linux.
- **GUI (`bugzapper.sh` / `bugzapper.bat`)** — pick port / firmware / baud /
  flash mode / erase, flash, and a built-in serial monitor (ANSI colors, live
  baud switching, send-to-serial, save / live-log to file). After a flash it
  reopens the monitor to show the boot log — so no "port busy" clash.
- **NodeMCU Lua tab** (optional) — for boards running NodeMCU-Lua firmware,
  upload `init.lua` & data files into the device filesystem (compile, run, or
  restart after), list files, or format the filesystem. Uses the bundled
  [`nodemcu-uploader`](https://github.com/kmpm/nodemcu-uploader) — no install.
  Add files with the **Add…** button, or — if `tkinterdnd2` is installed
  (`pip install tkinterdnd2`) — **drag files** straight from your file manager
  onto the list. (tkdnd ships compiled per-platform libs, so it's an optional
  extra rather than bundled; the button is the always-available fallback.)
- **CLI (`flash.py`, or `flash.sh` on Unix)** — the same flashing as a one-liner;
  version-robust across esptool 4.x/5.x.
- **Drop-in** — auto-detects `./firmware/*.bin`; customize via env vars (below).

## Use it

```sh
# GUI
./bugzapper.sh                 # macOS / Linux — lists ./firmware/*.bin
./bugzapper.sh path/to/bins    # or point it at a firmware folder
bugzapper.bat                  # Windows (same args / env vars)

# CLI (cross-platform)
python3 flash.py               # flash the first ./firmware/*.bin to the auto-found port
python3 flash.py -f build/app.bin -e   # specific file, erase first
python3 flash.py -p COM5 -b 460800     # explicit port (COMx on Windows) + baud
python3 flash.py -h            # all options
./flash.sh                     # macOS / Linux bash twin of flash.py
```

Requirements: `python3` on any of Windows / macOS / Linux. For the GUI you also
need Tk — it ships with the python.org Windows/macOS installers; on Linux or
Homebrew install it (`apt install python3-tk`, or `brew install python-tk@3.13`).

## Customize (no code edits)

| Env var | What | Default |
|---|---|---|
| `BUGZAPPER_TITLE` | GUI window title | `BugZapper` |
| `BUGZAPPER_ICON`  | path to a PNG window icon | `./icon.png` |
| `BUGZAPPER_FW_DIR`| folder of `.bin` files | `./firmware`, else cwd |

## Add to your project

Copy `flashui.py`, `flash.py`, `bugzapper.sh`, `bugzapper.bat`, `flash.sh`,
`vendor/` (and optionally `icon.png`) into the repo — or add it as a **git
submodule** and call it via a thin wrapper that sets `BUGZAPPER_TITLE` /
`BUGZAPPER_ICON` for your project.

## Notes

- The bundled esptool is **2.8** (single-file, pure-python) — ideal for
  ESP8266/ESP8285. For newer ESP32 variants, install a current `esptool` on
  `PATH` and BugZapper will use it.
- The serial monitor uses the bundled `pyserial` (`serial.Serial`), so it works
  the same on Windows, macOS and Linux — no `stty`/`/dev` assumptions.

## Tests

Stdlib `unittest` — no install needed. Covers vendor integrity (the bundled
tools execute, no compiled binaries), the CLI (command construction + help /
list / error paths), the GUI helpers and a Tk build smoke test, and the
pyserial read/write path via a pty loopback.

```sh
./run_tests.sh        # macOS / Linux (picks a tkinter python so GUI tests run)
run_tests.bat         # Windows
python3 -m unittest discover -s tests -p 'test_*.py' -v   # direct
```

Tests that need Tk or a serial pty skip cleanly where unavailable (e.g. headless
or Windows), so the suite is green everywhere. CI runs it on Ubuntu, macOS and
Windows ([`.github/workflows/tests.yml`](.github/workflows/tests.yml)).

## Licenses

BugZapper's own code is MIT (see [LICENSE](LICENSE)). Bundled in `vendor/`:
`esptool` (GPLv2), `pyserial` (BSD-3-Clause), and `nodemcu-uploader` (MIT),
each under its own license.
