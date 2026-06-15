# ⚡ BugZapper

A tiny, dependency-light flasher for **ESP8266 / ESP8285** boards — a GUI **and**
a CLI — that flashes firmware *and* shows the serial output in one place, so you
don't need separate [NodeMCU PyFlasher](https://github.com/marcelstoer/nodemcu-pyflasher)
+ [CoolTerm](https://freeware.the-meiers.org/) windows.

- **No install needed** — a pure-python `esptool` + `pyserial` are bundled in
  [`vendor/`](vendor); only `python3` is required (plus Tk for the GUI). A
  system `esptool` is used instead if one is on `PATH`.
- **GUI (`bugzapper.sh`)** — pick port / firmware / baud / flash mode / erase,
  flash, and a built-in serial monitor (ANSI colors, live baud switching,
  send-to-serial, save / live-log to file). After a flash it reopens the monitor
  to show the boot log — so no "port busy" clash.
- **NodeMCU Lua tab** (optional) — for boards running NodeMCU-Lua firmware,
  upload `init.lua` & data files into the device filesystem (compile, run, or
  restart after), list files, or format the filesystem. Uses the bundled
  [`nodemcu-uploader`](https://github.com/kmpm/nodemcu-uploader) — no install.
- **CLI (`flash.sh`)** — the same flashing as a one-liner; version-robust across
  esptool 4.x/5.x.
- **Drop-in** — auto-detects `./firmware/*.bin`; customize via env vars (below).

## Use it

```sh
# GUI
./bugzapper.sh                 # lists ./firmware/*.bin
./bugzapper.sh path/to/bins    # or point it at a firmware folder

# CLI
./flash.sh                     # flash the first ./firmware/*.bin to the auto-found port
./flash.sh -f build/app.bin -e # specific file, erase first
./flash.sh -h                  # all options
```

Requirements: macOS or Linux, `python3`. For the GUI, Tk:
`brew install python-tk@3.10` (any `python-tk` works).

## Customize (no code edits)

| Env var | What | Default |
|---|---|---|
| `BUGZAPPER_TITLE` | GUI window title | `BugZapper` |
| `BUGZAPPER_ICON`  | path to a PNG window icon | `./icon.png` |
| `BUGZAPPER_FW_DIR`| folder of `.bin` files | `./firmware`, else cwd |

## Add to your project

Copy `bugzapper.sh`, `flash.sh`, `flashui.py`, `vendor/` (and optionally
`icon.png`) into the repo — or add it as a **git submodule** and call it via a
thin wrapper that sets `BUGZAPPER_TITLE` / `BUGZAPPER_ICON` for your project.

## Notes

- The bundled esptool is **2.8** (single-file, pure-python) — ideal for
  ESP8266/ESP8285. For newer ESP32 variants, install a current `esptool` on
  `PATH` and BugZapper will use it.
- The serial monitor reads the port directly via `stty` + a file descriptor, so
  `pyserial` isn't needed for monitoring.

## Licenses

BugZapper's own code is MIT (see [LICENSE](LICENSE)). Bundled in `vendor/`:
`esptool` (GPLv2), `pyserial` (BSD-3-Clause), and `nodemcu-uploader` (MIT),
each under its own license.
