#!/usr/bin/env python3
"""BugZapper — a small Tkinter GUI to flash ESP8266/ESP8285/ESP32 firmware,
upload NodeMCU Lua files, and watch the serial output, in one window (no
separate PyFlasher + CoolTerm + nodemcu-uploader).

Launched by bugzapper.sh (macOS/Linux) or bugzapper.bat (Windows), which pick a
python3 that has tkinter. Flashing uses the bundled esptool, the optional NodeMCU
Lua tab uses the bundled nodemcu-uploader, and the serial monitor + port list use
the bundled pyserial — all pure-python (no install) and cross-platform
(Windows / macOS / Linux).

Drop-in for any project. Customize without editing this file:
  BUGZAPPER_TITLE   window title            (default "BugZapper")
  BUGZAPPER_ICON    path to a PNG icon      (default ./icon.png next to this file)
  BUGZAPPER_FW_DIR  folder of .bin files    (default: ./firmware, else cwd)
  argv[1]           a firmware folder, overrides BUGZAPPER_FW_DIR
"""
import glob
import json
import os
import re
import subprocess
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

HERE = os.path.dirname(os.path.abspath(__file__))
# Bundled pure-python esptool + pyserial, so everything works with no install.
VENDOR = os.path.join(HERE, "vendor")
# Put the bundled pyserial on the path so the GUI itself (port list + monitor)
# can use it — this is what makes the monitor cross-platform (Win/macOS/Linux).
if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)
import serial                                   # noqa: E402  (after sys.path setup)
from serial.tools.list_ports import comports    # noqa: E402

TITLE = os.environ.get("BUGZAPPER_TITLE", "BugZapper")
ICON = os.environ.get("BUGZAPPER_ICON") or os.path.join(HERE, "icon.png")


def firmware_dir():
    """Where to look for .bin files: a CLI arg, else $BUGZAPPER_FW_DIR, else
    ./firmware under the current dir, else the current dir."""
    if len(sys.argv) > 1 and sys.argv[1]:
        return sys.argv[1]
    if os.environ.get("BUGZAPPER_FW_DIR"):
        return os.environ["BUGZAPPER_FW_DIR"]
    cwd_fw = os.path.join(os.getcwd(), "firmware")
    return cwd_fw if os.path.isdir(cwd_fw) else os.getcwd()


FW_DIR = firmware_dir()
BAUDS = ["9600", "57600", "74880", "115200", "230400", "460800", "921600"]
MODES = ["dio", "qio", "dout"]
LINE_ENDINGS = {"NL": "\n", "CR": "\r", "CR+NL": "\r\n", "None": ""}

# ANSI escape sequences (colors like \e[0;33m, cursor moves like \e[1A / \e[2K).
# ANSI_RE strips them (used for the plain-text log file); ESC_RE splits them out
# so _write can render SGR colors and handle cursor/erase codes in the widget.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
ESC_RE = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")

# 8 standard + 8 bright foreground colors (SGR 30-37 / 90-97), VS Code-ish hues
# that read well on the dark log background.
PALETTE = {30: "#666666", 31: "#cd3131", 32: "#0dbc79", 33: "#e5e510",
           34: "#2472c8", 35: "#bc3fbc", 36: "#11a8cd", 37: "#e5e5e5",
           90: "#888888", 91: "#f14c4c", 92: "#23d18b", 93: "#f5f543",
           94: "#3b8eea", 95: "#d670d6", 96: "#29b8db", 97: "#ffffff"}


def list_ports():
    """Serial ports across macOS / Linux / Windows, via pyserial (COMx on
    Windows, /dev/* elsewhere)."""
    return sorted(p.device for p in comports())


def list_firmware():
    # glob.escape guards against [ * ? in the firmware dir's path (see files_in_folder).
    return sorted(glob.glob(os.path.join(glob.escape(FW_DIR), "*.bin")))


# Shared pure helpers live in flash.py (the CLI — import-safe with no tkinter),
# re-exported here so the GUI, CLI and tests share one source of truth.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from flash import (files_in_folder, suggest_offset,           # noqa: E402,F401
                   scan_esp32_folder, ESP32_PART_OFFSETS)     # noqa: E402,F401


def parse_offset(text):
    """Normalize a flash offset typed into the GUI ('0x1000', '4096', blank →
    0x0). Returns the trimmed string — esptool accepts hex or decimal — and
    raises ValueError if it isn't a number."""
    off = (text or "").strip() or "0x0"
    int(off, 0)  # validate (0x-hex or decimal)
    return off


def settings_path():
    """Per-user settings file. $BUGZAPPER_SETTINGS overrides (also used by the
    tests to stay isolated); otherwise the platform's config home."""
    if os.environ.get("BUGZAPPER_SETTINGS"):
        return os.environ["BUGZAPPER_SETTINGS"]
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "bugzapper", "settings.json")


def load_settings():
    """Saved GUI settings, or {} when missing/corrupt — never fatal."""
    try:
        with open(settings_path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    """Best-effort write — a read-only config dir must never break closing."""
    path = settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


def tool_env():
    """Env for running the bundled tools (esptool, nodemcu-uploader): bundled
    pyserial on PYTHONPATH, and NO_COLOR (we render/strip ANSI ourselves)."""
    pp = VENDOR
    if os.environ.get("PYTHONPATH"):
        pp += os.pathsep + os.environ["PYTHONPATH"]
    return dict(os.environ, NO_COLOR="1", PYTHONPATH=pp)


def resolve_esptool():
    """Return a working esptool argv prefix, or None. Prefers the bundled
    pure-python esptool in vendor/ (no install needed); falls back to a
    system esptool. Tests by executing 'version' (a broken-shebang esptool.py
    passes a presence check but fails to run)."""
    bundled = os.path.join(VENDOR, "esptool.py")
    candidates = []
    if os.path.isfile(bundled):
        candidates.append([sys.executable, bundled])
    candidates += [["esptool"], ["esptool.py"],
                   [sys.executable, "-m", "esptool"], ["python3", "-m", "esptool"]]
    for cand in candidates:
        try:
            if subprocess.run(cand + ["version"], capture_output=True,
                              env=tool_env()).returncode == 0:
                return cand
        except (FileNotFoundError, OSError):
            continue
    return None


def resolve_nodemcu():
    """Return a working nodemcu-uploader argv prefix, or None. Prefers the
    bundled pure-python package in vendor/ (no install needed); falls back to a
    system one. Tested by executing '--version' (mirrors resolve_esptool)."""
    candidates = []
    if os.path.isdir(os.path.join(VENDOR, "nodemcu_uploader")):
        candidates.append([sys.executable, "-m", "nodemcu_uploader"])
    candidates += [["nodemcu-uploader"], ["nodemcu-uploader.py"]]
    for cand in candidates:
        try:
            if subprocess.run(cand + ["--version"], capture_output=True,
                              env=tool_env()).returncode == 0:
                return cand
        except (FileNotFoundError, OSError):
            continue
    return None


def nodemcu_upload_flags(compile_lc=False, dofile=False, restart=False,
                         verify="none"):
    """The 'upload' subcommand + option flags for nodemcu-uploader, in the order
    the GUI checkboxes map to (-c compile, -e run, -r restart, -v verify)."""
    flags = ["upload"]
    if compile_lc:
        flags.append("-c")
    if dofile:
        flags.append("-e")
    if restart:
        flags.append("-r")
    if verify and verify != "none":
        flags += ["-v", verify]
    return flags


class FlasherApp:
    def __init__(self, root):
        self.root = root
        root.title(TITLE)
        root.minsize(720, 520)
        self._set_icon()

        self.q = queue.Queue()
        self.monitor_ser = None  # open serial.Serial while the monitor runs
        self.monitor_stop = threading.Event()
        self.logfile = None  # open file handle when "Log to file" is active
        self._sgr_fg = None   # current ANSI foreground (None = default)
        self._sgr_bold = False
        self.busy = False  # flashing in progress
        self._proc = None  # running esptool/uploader subprocess (for cancel)
        self._can_cancel = False  # True only while cancelling is still safe
        self._op_cancelled = False
        self._cancel_note = "operation cancelled"  # log line on cancel
        self._gutter_pending = False  # debounce line-number gutter redraws

        self._build_header()
        self._build_tabs()
        self._build_log()
        self._build_send()
        # Action buttons disabled while an external tool (esptool / uploader) runs.
        self.action_btns = [self.flash_btn, self.monitor_btn, self.upload_btn,
                            self.lualist_btn, self.luaformat_btn,
                            self.chipinfo_btn]
        # One ✕ Cancel per tab, armed/locked/disarmed together.
        self.cancel_btns = [self.cancel_btn, self.nm_cancel_btn]
        self._refresh_ports(select_first=True)
        self._refresh_firmware()
        self._apply_settings(load_settings())  # after ports, so a saved port wins

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._drain)

    def _set_icon(self):
        """Use the icon (BUGZAPPER_ICON or ./icon.png) as the window icon."""
        try:
            self._icon = tk.PhotoImage(file=ICON)  # keep a ref (avoid GC)
            self.root.iconphoto(True, self._icon)
        except tk.TclError:
            pass  # icon missing/unreadable — not fatal

    # ---- UI construction ----------------------------------------------------
    def _build_header(self):
        """Port + baud + monitor/log controls, shared by both tabs."""
        f = ttk.Frame(self.root, padding=(10, 10, 10, 4))
        f.pack(fill="x")
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Serial port").grid(row=0, column=0, sticky="w", pady=3)
        self.port = ttk.Combobox(f, state="readonly", width=34)
        self.port.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="Refresh", command=self._refresh_ports).grid(row=0, column=2)

        row = ttk.Frame(f)
        row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(row, text="Baud").pack(side="left")
        self.baud = ttk.Combobox(row, state="readonly", width=8, values=BAUDS)
        self.baud.set("115200")
        self.baud.pack(side="left", padx=(4, 16))
        # retune a live monitor when the baud changes (e.g. 74880 boot ROM <-> 115200)
        self.baud.bind("<<ComboboxSelected>>", self._on_baud_change)
        self.monitor_btn = ttk.Button(row, text="▶ Connect monitor",
                                      command=self._toggle_monitor)
        self.monitor_btn.pack(side="left")
        ttk.Button(row, text="Clear log", command=self._clear).pack(side="left", padx=6)
        ttk.Button(row, text="Save log…", command=self._save_log).pack(side="left")
        self.logfile_btn = ttk.Button(row, text="● Log to file",
                                      command=self._toggle_logfile)
        self.logfile_btn.pack(side="left", padx=6)
        # Probe the connected board (chip type, MAC, flash size) via esptool
        # flash_id — answers "ESP8266 or ESP32, and how big is the flash?"
        self.chipinfo_btn = ttk.Button(row, text="Chip info",
                                       command=self._chip_info)
        self.chipinfo_btn.pack(side="left")
        self.status = ttk.Label(row, text="ready", foreground="#1FA67A")
        self.status.pack(side="right")

    def _build_tabs(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="x", padx=10, pady=(4, 0))
        self._build_flash_tab(nb)
        self._build_upload_tab(nb)

    def _build_flash_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="Flash firmware")
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Firmware").grid(row=0, column=0, sticky="w", pady=3)
        self.firmware = ttk.Combobox(f, width=34)
        self.firmware.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="Browse…", command=self._browse_fw).grid(row=0, column=2)

        row = ttk.Frame(f)
        row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(row, text="Flash mode").pack(side="left")
        self.mode = ttk.Combobox(row, state="readonly", width=6, values=MODES)
        self.mode.set("dio")
        self.mode.pack(side="left", padx=(4, 16))
        self.erase = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Erase flash (wipes all data)",
                        variable=self.erase).pack(side="left")

        btns = ttk.Frame(f)
        btns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.flash_btn = ttk.Button(btns, text="⚡ Flash", command=self._flash)
        self.flash_btn.pack(side="left")
        # Enabled only while esptool is still connecting; once the chip responds
        # (writing about to start) it disables — cancelling mid-write can brick.
        self.cancel_btn = ttk.Button(btns, text="✕ Cancel", command=self._cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        # ESP8266 users never need offsets — everything ESP32/multi-part lives
        # behind this disclosure, collapsed by default so the simple flow stays
        # a two-click affair. The label summarizes hidden non-default state.
        self.adv_btn = ttk.Button(btns, command=self._toggle_advanced)
        self.adv_btn.pack(side="left", padx=(16, 0))

        # Multi-part images (ESP32: bootloader @0x1000, partition table @0x8000,
        # app @0x10000). "+ Add part" queues Firmware@Offset; a non-empty Parts
        # list is flashed together in one esptool call, otherwise the single
        # Firmware file above is flashed at Offset (0x0 unless changed here).
        adv = ttk.Frame(f)
        adv.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        adv.columnconfigure(0, weight=1)
        self.adv_frame = adv

        offrow = ttk.Frame(adv)
        offrow.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(offrow, text="Offset").pack(side="left")
        self.fw_offset = ttk.Entry(offrow, width=8)
        self.fw_offset.insert(0, "0x0")
        self.fw_offset.pack(side="left", padx=(4, 4))
        ttk.Label(offrow, text="— where the file is written; ESP8266 images "
                              "stay at 0x0").pack(side="left")
        ttk.Button(offrow, text="+ Add part",
                   command=self._add_part).pack(side="left", padx=(16, 0))

        ttk.Label(adv, text="Parts (ESP32 multi-file image — empty = just the "
                            "file above):").grid(row=1, column=0, columnspan=2,
                                                 sticky="w", pady=(6, 0))
        self.parts_box = tk.Listbox(adv, height=3, activestyle="none",
                                    selectmode="extended")
        self.parts_box.grid(row=2, column=0, sticky="ew", pady=(2, 0))
        pb = ttk.Frame(adv)
        pb.grid(row=2, column=1, sticky="n", padx=(6, 0))
        ttk.Button(pb, text="Scan…", command=self._scan_parts_folder,
                   width=8).pack(fill="x")
        ttk.Button(pb, text="Remove", command=self._remove_part, width=8).pack(fill="x", pady=4)
        ttk.Button(pb, text="Clear", command=self._clear_parts, width=8).pack(fill="x")
        self._parts = []  # [(offset_str, full_path)]; parts_box shows basenames

        self.adv_visible = False
        adv.grid_remove()  # collapsed by default — plain ESP8266 UI
        self._update_adv_label()

    def _toggle_advanced(self):
        """Show/hide the ESP32 offset + parts section."""
        self.adv_visible = not self.adv_visible
        if self.adv_visible:
            self.adv_frame.grid()
        else:
            self.adv_frame.grid_remove()
        self._update_adv_label()

    def _update_adv_label(self):
        """Toggle-button text; when collapsed, surface any non-default state so
        a hidden offset or queued parts can never silently change a flash."""
        if self.adv_visible:
            self.adv_btn.configure(text="▾ ESP32 / advanced")
            return
        extras = []
        try:
            off = parse_offset(self.fw_offset.get())
            if int(off, 0) != 0:
                extras.append(f"offset {off}")
        except ValueError:
            extras.append("bad offset")
        if self._parts:
            extras.append(f"{len(self._parts)} part(s)")
        suffix = f" ({', '.join(extras)})" if extras else ""
        self.adv_btn.configure(text=f"▸ ESP32 / advanced{suffix}")

    def _build_upload_tab(self, nb):
        """Optional tab: upload Lua/data files into the NodeMCU filesystem via
        the bundled nodemcu-uploader (only useful with NodeMCU-Lua firmware)."""
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text="NodeMCU Lua")
        f.columnconfigure(0, weight=1)

        listwrap = ttk.Frame(f)
        listwrap.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        listwrap.columnconfigure(0, weight=1)
        self.lua_files = tk.Listbox(listwrap, height=4, activestyle="none",
                                    selectmode="extended")
        self.lua_files.grid(row=0, column=0, sticky="ew")
        sb = ttk.Scrollbar(listwrap, orient="vertical",
                           command=self.lua_files.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.lua_files.configure(yscrollcommand=sb.set)

        ttk.Label(f, text="Lua / data files to upload to the NodeMCU filesystem:"
                  ).grid(row=0, column=0, columnspan=2, sticky="w")

        filebtns = ttk.Frame(f)
        filebtns.grid(row=1, column=1, sticky="n", padx=(6, 0))
        ttk.Button(filebtns, text="Add…", command=self._lua_add, width=10).pack(fill="x")
        ttk.Button(filebtns, text="Add folder…", command=self._lua_add_folder, width=10).pack(fill="x", pady=4)
        ttk.Button(filebtns, text="Remove", command=self._lua_remove, width=10).pack(fill="x")
        ttk.Button(filebtns, text="Clear", command=self._lua_clear, width=10).pack(fill="x", pady=(4, 0))

        opts = ttk.Frame(f)
        opts.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.lua_compile = tk.BooleanVar(value=False)
        self.lua_dofile = tk.BooleanVar(value=False)
        self.lua_restart = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Compile (.lc)",
                        variable=self.lua_compile).pack(side="left")
        ttk.Checkbutton(opts, text="Run after upload",
                        variable=self.lua_dofile).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(opts, text="Restart after",
                        variable=self.lua_restart).pack(side="left", padx=(12, 0))
        ttk.Label(opts, text="Verify").pack(side="left", padx=(12, 4))
        self.lua_verify = ttk.Combobox(opts, state="readonly", width=6,
                                       values=["none", "raw", "sha1"])
        self.lua_verify.set("none")
        self.lua_verify.pack(side="left")

        btns = ttk.Frame(f)
        btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.upload_btn = ttk.Button(btns, text="⬆ Upload", command=self._upload)
        self.upload_btn.pack(side="left")
        self.lualist_btn = ttk.Button(btns, text="List files", command=self._lua_list)
        self.lualist_btn.pack(side="left", padx=6)
        self.luaformat_btn = ttk.Button(btns, text="Format FS…", command=self._lua_format)
        self.luaformat_btn.pack(side="left")
        # Same cancel semantics as flashing: active while the op is still safe
        # to abort (an upload locks once file transfer starts).
        self.nm_cancel_btn = ttk.Button(btns, text="✕ Cancel",
                                        command=self._cancel, state="disabled")
        self.nm_cancel_btn.pack(side="left", padx=6)

    def _build_log(self):
        # A gutter Canvas (line numbers) + the log Text + a scrollbar, laid out
        # side by side. self.log stays a plain Text so all insert/delete/tag and
        # copy bindings below keep working; the gutter redraws in sync with it.
        wrap = ttk.Frame(self.root)
        wrap.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self._gutter_font = ("Menlo", 11)
        self.gutter = tk.Canvas(wrap, width=52, bg="#1e1e1e",
                                highlightthickness=0, bd=0, takefocus=0)
        self.gutter.pack(side="left", fill="y")
        sb = ttk.Scrollbar(wrap, orient="vertical")
        sb.pack(side="right", fill="y")
        # padx/pady give inner padding so text isn't flush against the edges;
        # bd/relief flat keeps the border clean.
        self.log = tk.Text(wrap, height=20, wrap="char",
                           bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
                           font=("Menlo", 11), padx=10, pady=8, bd=0,
                           relief="flat", yscrollcommand=self._on_yscroll)
        self.log.pack(side="left", fill="both", expand=True)
        sb.configure(command=self.log.yview)
        self._log_sb = sb
        for code, hexc in PALETTE.items():
            self.log.tag_configure(f"fg{code}", foreground=hexc)
        self.log.tag_configure("bold", font=("Menlo", 11, "bold"))
        self.log.configure(state="disabled")
        # Keep numbers aligned when the widget is resized (wrapping changes).
        self.log.bind("<Configure>", lambda e: self._schedule_gutter(), add="+")

        # The widget is disabled so the log can't be typed into, but a disabled
        # Text never takes focus, so the copy accelerator had nothing to act on
        # and selected output could not be copied. Give it focus on click and
        # wire copy / select-all explicitly (Command on macOS, Control
        # elsewhere), plus a right-click menu for discoverability.
        self.log.bind("<Button-1>", lambda e: self.log.focus_set(), add="+")
        for seq in ("<Control-c>", "<Command-c>"):
            self.log.bind(seq, self._copy_log)
        for seq in ("<Control-a>", "<Command-a>"):
            self.log.bind(seq, self._select_all_log)

        self._log_menu = tk.Menu(self.log, tearoff=0)
        self._log_menu.add_command(label="Copy", command=self._copy_log)
        self._log_menu.add_command(label="Select All",
                                   command=self._select_all_log)
        self._log_menu.add_separator()
        self._log_menu.add_command(label="Clear", command=self._clear)
        for seq in ("<Button-3>", "<Button-2>"):  # right-click / macOS trackpad
            self.log.bind(seq, self._show_log_menu)

    def _copy_log(self, event=None):
        """Copy the selection (or everything, if nothing is selected)."""
        try:
            text = self.log.get("sel.first", "sel.last")
        except tk.TclError:
            text = self.log.get("1.0", "end-1c")
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        return "break"

    def _select_all_log(self, event=None):
        self.log.tag_add("sel", "1.0", "end-1c")
        self.log.focus_set()
        return "break"

    def _show_log_menu(self, event):
        self.log.focus_set()
        try:
            self._log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._log_menu.grab_release()
        return "break"

    # ---- line-number gutter -------------------------------------------------
    def _on_yscroll(self, first, last):
        """Text's yscrollcommand: drive the scrollbar and repaint the gutter."""
        self._log_sb.set(first, last)
        self._schedule_gutter()

    def _schedule_gutter(self):
        """Coalesce redraws to one per idle cycle (the log streams fast) and let
        layout settle first, so dlineinfo returns real pixel positions."""
        if not self._gutter_pending:
            self._gutter_pending = True
            self.root.after_idle(self._redraw_gutter)

    def _redraw_gutter(self):
        self._gutter_pending = False
        g = self.gutter
        g.delete("all")
        # Widen the gutter to fit the largest line number currently shown.
        total = int(self.log.index("end-1c").split(".")[0])
        need = 14 + 8 * len(str(max(total, 1)))
        if int(g.cget("width")) != need:
            g.configure(width=need)
        gw = need
        idx = self.log.index("@0,0")  # first visible display line
        while True:
            info = self.log.dlineinfo(idx)
            if info is None:
                break  # past the last visible line
            line, col = idx.split(".")
            if col == "0":  # only the first display row of a wrapped logical line
                y, h = info[1], info[3]
                g.create_text(gw - 6, y + h // 2, anchor="e", text=line,
                              font=self._gutter_font, fill="#5a5a5a")
            nxt = self.log.index(f"{idx}+1 display lines")
            if nxt == idx:  # can't advance (end of text) — avoid an infinite loop
                break
            idx = nxt

    def _build_send(self):
        f = ttk.Frame(self.root, padding=(10, 6, 10, 10))
        f.pack(fill="x")
        ttk.Label(f, text="Send").pack(side="left")
        self.send_entry = ttk.Entry(f)
        self.send_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.send_entry.bind("<Return>", self._send)
        self.line_ending = ttk.Combobox(f, state="readonly", width=6,
                                        values=list(LINE_ENDINGS))
        self.line_ending.set("NL")
        self.line_ending.pack(side="left", padx=(0, 6))
        ttk.Button(f, text="Send", command=self._send).pack(side="left")

    # ---- helpers ------------------------------------------------------------
    def _refresh_ports(self, select_first=False):
        ports = list_ports()
        self.port["values"] = ports
        if ports and (select_first or self.port.get() not in ports):
            self.port.set(ports[0])

    def _refresh_firmware(self):
        fws = list_firmware()
        self.firmware["values"] = fws
        if fws and self.firmware.get() not in fws:
            self.firmware.set(fws[0])

    def _browse_fw(self):
        path = filedialog.askopenfilename(
            initialdir=FW_DIR,
            filetypes=[("Firmware", "*.bin"), ("All files", "*")])
        if path:
            self.firmware.set(path)

    def _lua_add(self):
        self._lua_add_paths(filedialog.askopenfilenames(
            initialdir=FW_DIR,
            filetypes=[("Lua / data", "*.lua *.lc *.html *.json *.txt"),
                       ("All files", "*")]))

    def _lua_add_folder(self):
        """Add every file in a chosen folder matching one or more extensions
        (e.g. 'lua html' to grab all .lua and .html files at once)."""
        folder = filedialog.askdirectory(
            initialdir=FW_DIR, title="Add all files from a folder")
        if not folder:
            return
        exts = simpledialog.askstring(
            "Add folder",
            "Extensions to add (space/comma separated; * = all):",
            initialvalue="lua html lc json txt", parent=self.root)
        if exts is None:  # cancelled
            return
        paths = files_in_folder(folder, exts)
        if not paths:
            self._emit(f"! no matching files in {folder}\n")
            return
        self._lua_add_paths(paths)

    def _lua_add_paths(self, paths):
        """Append files to the upload list, skipping dupes and non-files."""
        existing = set(self.lua_files.get(0, "end"))
        for p in paths:
            if p and p not in existing and os.path.isfile(p):
                self.lua_files.insert("end", p)
                existing.add(p)

    def _lua_remove(self):
        for i in reversed(self.lua_files.curselection()):
            self.lua_files.delete(i)

    def _lua_clear(self):
        self.lua_files.delete(0, "end")

    def _set_status(self, text, color="#d4d4d4"):
        self.status.configure(text=text, foreground=color)

    def _emit(self, text):
        self.q.put(text)

    def _clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._schedule_gutter()

    def _save_log(self):
        """One-shot: write the current log buffer to a file."""
        path = filedialog.asksaveasfilename(
            defaultextension=".log", initialdir=FW_DIR,
            filetypes=[("Log", "*.log *.txt"), ("All files", "*")])
        if not path:
            return
        try:
            with open(path, "w") as fh:
                fh.write(self.log.get("1.0", "end-1c"))
        except OSError as e:
            self._emit(f"! could not save log: {e}\n")
            return
        self._emit(f"--- log saved to {path} ---\n")

    def _toggle_logfile(self):
        """Continuously append all output to a file until toggled off."""
        if self.logfile is not None:
            try:
                self.logfile.close()
            except OSError:
                pass
            self.logfile = None
            self.logfile_btn.configure(text="● Log to file")
            self._emit("--- stopped logging to file ---\n")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".log", initialdir=FW_DIR,
            filetypes=[("Log", "*.log *.txt"), ("All files", "*")])
        if not path:
            return
        try:
            self.logfile = open(path, "a", buffering=1)  # line-buffered
        except OSError as e:
            self._emit(f"! could not open log file: {e}\n")
            return
        self.logfile_btn.configure(text="■ Logging…")
        self._emit(f"--- logging output to {path} ---\n")

    def _drain(self):
        try:
            while True:
                self._write(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self._drain)

    def _write(self, text):
        """Append to the log, rendering ANSI SGR colors as text tags and
        honoring carriage returns / line-erase so progress bars update one line
        instead of spamming. The optional file log gets plain (stripped) text."""
        if self.logfile is not None:
            try:
                self.logfile.write(ANSI_RE.sub("", text))
            except OSError:
                pass
        self.log.configure(state="normal")
        pos = 0
        for m in ESC_RE.finditer(text):
            seg = text[pos:m.start()]
            if seg:
                self._insert_styled(seg)
            params, letter = m.group(1), m.group(2)
            if letter == "m":          # SGR: set color / bold
                self._apply_sgr(params)
            elif letter == "K":        # erase line (progress redraw)
                self.log.delete("end-1c linestart", "end-1c")
            # other CSI codes (cursor moves etc.) are ignored
            pos = m.end()
        tail = text[pos:]
        if tail:
            self._insert_styled(tail)
        self.log.see("end")
        self.log.configure(state="disabled")
        self._schedule_gutter()

    def _insert_styled(self, seg):
        """Insert a plain (escape-free) span, applying the current SGR style and
        handling \\r (clear line) and \\n."""
        tags = self._sgr_tags()
        for part in re.split(r"(\r\n|\n|\r)", seg):
            if part in ("\n", "\r\n"):
                self.log.insert("end", "\n")
            elif part == "\r":
                self.log.delete("end-1c linestart", "end-1c")
            elif part:
                self.log.insert("end", part, tags)

    def _apply_sgr(self, params):
        codes = [int(p) for p in params.split(";") if p.isdigit()]
        if not codes:           # bare ESC[m means reset
            codes = [0]
        for code in codes:
            if code == 0:
                self._sgr_fg, self._sgr_bold = None, False
            elif code == 1:
                self._sgr_bold = True
            elif code == 22:
                self._sgr_bold = False
            elif code == 39:
                self._sgr_fg = None
            elif code in PALETTE:
                self._sgr_fg = code

    def _sgr_tags(self):
        tags = []
        if self._sgr_fg is not None:
            tags.append(f"fg{self._sgr_fg}")
        if self._sgr_bold:
            tags.append("bold")
        return tuple(tags)

    # ---- serial monitor (pyserial — cross-platform) ------------------------
    def _toggle_monitor(self):
        if self.monitor_ser is not None:
            self._stop_monitor()
        else:
            self._start_monitor()

    def _start_monitor(self):
        port = self.port.get()
        if not port:
            self._emit("! no serial port selected\n")
            return
        baud = self.baud.get()
        # pyserial opens the port and configures the baud atomically, holding the
        # handle open — so the macOS "reopen resets baud" gotcha doesn't bite, and
        # it works identically on Windows/Linux/macOS (no stty, no /dev assumptions).
        try:
            ser = serial.Serial(port, int(baud), timeout=0.2)
        except (serial.SerialException, ValueError, OSError) as e:
            self._emit(f"! could not open {port} @ {baud}: {e}\n")
            return
        self.monitor_ser = ser
        self.monitor_stop.clear()
        self.monitor_btn.configure(text="■ Disconnect monitor")
        self._set_status(f"monitor @ {baud}", "#1FA67A")
        self._emit(f"--- monitor connected: {port} @ {baud} ---\n")
        self._emit("(a short gibberish burst at reset is the ESP boot ROM at "
                   "74880 baud; firmware output follows at the selected baud)\n")
        threading.Thread(target=self._read_monitor, args=(ser,),
                         daemon=True).start()

    def _send(self, *_):
        if self.monitor_ser is None:
            self._emit("! connect the monitor first to send\n")
            return
        msg = self.send_entry.get()
        ending = LINE_ENDINGS.get(self.line_ending.get(), "\n")
        try:
            self.monitor_ser.write((msg + ending).encode("utf-8"))
        except (serial.SerialException, OSError) as e:
            self._emit(f"! send failed: {e}\n")
            return
        self._emit(f">> {msg}\n")
        self.send_entry.delete(0, "end")

    def _on_baud_change(self, *_):
        """Retune the live monitor without reconnecting — pyserial applies the
        new baud to the already-open port in place."""
        if self.monitor_ser is None:
            return
        baud = self.baud.get()
        try:
            self.monitor_ser.baudrate = int(baud)
        except (serial.SerialException, ValueError, OSError) as e:
            self._emit(f"! could not set baud {baud}: {e}\n")
            return
        self._set_status(f"monitor @ {baud}", "#1FA67A")
        self._emit(f"--- baud changed to {baud} ---\n")

    def _read_monitor(self, ser):
        while not self.monitor_stop.is_set():
            try:
                # read() returns after the timeout with whatever arrived (possibly
                # empty); in_waiting drains the buffer without an extra wait.
                data = ser.read(ser.in_waiting or 1)
            except (serial.SerialException, OSError):
                break  # device unplugged / port closed
            if data:
                self._emit(data.decode("utf-8", "replace"))

    def _stop_monitor(self):
        self.monitor_stop.set()
        ser = self.monitor_ser
        self.monitor_ser = None
        if ser is not None:
            try:
                ser.close()
            except (serial.SerialException, OSError):
                pass
        self.monitor_btn.configure(text="▶ Connect monitor")
        self._set_status("ready")
        self._emit("--- monitor disconnected ---\n")

    # ---- external tools (esptool / nodemcu-uploader) ------------------------
    # Both need exclusive use of the serial port, so they share the same
    # prep/teardown: stop the monitor, lock the buttons, run, then reopen the
    # monitor on success to show the boot log (like CoolTerm).
    def _begin_tool(self, status):
        """Free the port and lock the action buttons before running a tool.
        Returns True if the monitor was running, so it can be reopened after."""
        was_monitoring = self.monitor_ser is not None
        if was_monitoring:
            self._stop_monitor()
        self.busy = True
        for b in self.action_btns:
            b.configure(state="disabled")
        self._set_status(status, "#e0a800")
        return was_monitoring

    def _end_tool(self):
        self.busy = False
        self._can_cancel = False
        for b in self.cancel_btns:
            b.configure(state="disabled")
        for b in self.action_btns:
            b.configure(state="normal")

    def _pump(self, cmd, uncancel_marker=None, on_uncancellable=None):
        """Run cmd, stream its combined stdout+stderr to the log, return the exit
        code. tool_env(): bundled pyserial on PYTHONPATH + NO_COLOR (we render or
        strip any remaining ANSI ourselves).

        If uncancel_marker is given, the first time it appears in the output we
        call on_uncancellable (on the UI thread) — used to close the "cancel while
        connecting" window once esptool reports the chip (writing is imminent)."""
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=tool_env())
            self._proc = proc
            fd = proc.stdout.fileno()
            passed_marker = False
            recent = ""
            while True:
                data = os.read(fd, 512)
                if not data:
                    break
                text = data.decode("utf-8", "replace")
                self._emit(text)
                if uncancel_marker and not passed_marker:
                    recent = (recent + text)[-256:]  # marker may span two reads
                    if uncancel_marker in recent:
                        passed_marker = True
                        if on_uncancellable:
                            self.root.after(0, on_uncancellable)
            return proc.wait()
        except OSError as e:
            self._emit(f"\n! error: {e}\n")
            return 1
        finally:
            self._proc = None

    # ---- chip probe ---------------------------------------------------------
    def _chip_info(self):
        """Read chip type / MAC / flash size off the connected board (esptool
        flash_id). Non-destructive; esptool times out by itself if the board
        isn't in bootloader mode."""
        if self.busy:
            return
        port = self.port.get()
        if not port:
            self._emit("! no serial port selected\n")
            return
        reconnect = self._begin_tool("probing…")
        # the probe is read-only, so it stays cancellable for its whole run
        self._arm_cancel("chip probe cancelled")
        threading.Thread(target=self._chip_info_worker,
                         args=(port, self.baud.get(), reconnect),
                         daemon=True).start()

    def _chip_info_worker(self, port, baud, reconnect):
        rc = 1
        try:
            esptool = resolve_esptool()
            if not esptool:
                self._emit("! no working esptool found. Install: brew install esptool\n")
            else:
                self._emit("\n==> Reading chip info\n")
                rc = self._pump(esptool + ["--port", port, "--baud", baud,
                                           "flash_id"])
        except Exception as e:  # never leave the UI stuck busy
            self._emit(f"\n! error: {e}\n")
        self.root.after(0, self._chip_info_done, rc, reconnect)

    def _chip_info_done(self, rc, reconnect):
        self._end_tool()
        if self._op_cancelled:
            self._set_status("cancelled", "#d9534f")
        elif rc == 0:
            self._set_status("chip info ✓", "#1FA67A")
        else:
            self._emit(f"\n! chip probe failed (exit {rc}). Is the board in "
                       "bootloader mode and the port free?\n")
            self._set_status("probe failed", "#d9534f")
        if reconnect:
            # the probe is read-only, so restore the monitor even on failure
            self._start_monitor()

    # ---- flashing -----------------------------------------------------------
    def _add_part(self):
        """Queue Firmware@Offset as one part of a multi-file image (ESP32 ships
        bootloader / partition table / app at different offsets)."""
        fw = self.firmware.get()
        if not fw or not os.path.isfile(fw):
            self._emit(f"! firmware not found: {fw}\n")
            return
        try:
            off = parse_offset(self.fw_offset.get())
        except ValueError:
            self._emit(f"! bad offset {self.fw_offset.get()!r} "
                       "(use hex like 0x10000, or decimal)\n")
            return
        if any(o == off for o, _ in self._parts):
            self._emit(f"! a part at offset {off} is already queued\n")
            return
        # Recognizable ESP32 part at an unconventional offset? Hint, don't
        # override — the user's choice always wins in a flasher.
        hint = suggest_offset(fw)
        if hint and int(hint, 0) != int(off, 0):
            self._emit(f"hint: {os.path.basename(fw)} is conventionally flashed "
                       f"at {hint} (you queued it at {off})\n")
        self._parts.append((off, fw))
        self.parts_box.insert("end", f"{off}  {os.path.basename(fw)}")
        self._update_adv_label()

    def _scan_parts_folder(self):
        """Fill the Parts list from an ESP32 build folder (see
        scan_esp32_folder). Everything lands in the list for review — nothing
        is flashed until the user hits Flash."""
        folder = filedialog.askdirectory(
            initialdir=FW_DIR, title="Scan an ESP32 build folder for parts")
        if not folder:
            return
        found, leftovers = scan_esp32_folder(folder)
        if not found:
            self._emit(f"! no recognizable ESP32 parts in {folder}\n")
            return
        if self._parts and not messagebox.askyesno(
                "Replace parts",
                "Replace the %d queued part(s) with the scanned folder?"
                % len(self._parts)):
            return
        self._clear_parts()
        for off, path in found:
            self._parts.append((off, path))
            self.parts_box.insert("end", f"{off}  {os.path.basename(path)}")
        self._emit("--- scanned %s: %s ---\n" % (
            folder, ", ".join("%s @ %s" % (os.path.basename(p), o)
                              for o, p in found)))
        for p in leftovers:
            self._emit(f"    (skipped {os.path.basename(p)} — add it manually "
                       "with an offset if it belongs to the image)\n")
        self._update_adv_label()

    def _remove_part(self):
        for i in reversed(self.parts_box.curselection()):
            del self._parts[i]
            self.parts_box.delete(i)
        self._update_adv_label()

    def _clear_parts(self):
        self._parts.clear()
        self.parts_box.delete(0, "end")
        self._update_adv_label()

    def _flash(self):
        if self.busy:
            return
        port = self.port.get()
        if not port:
            self._emit("! no serial port selected\n")
            return
        if self._parts:
            # multi-part image: flash the queued parts, lowest offset first
            parts = sorted(self._parts, key=lambda t: int(t[0], 0))
        else:
            fw = self.firmware.get()
            if not fw or not os.path.isfile(fw):
                self._emit(f"! firmware not found: {fw}\n")
                return
            try:
                off = parse_offset(self.fw_offset.get())
            except ValueError:
                self._emit(f"! bad offset {self.fw_offset.get()!r} "
                           "(use hex like 0x10000, or decimal)\n")
                return
            parts = [(off, fw)]
        missing = [p for _, p in parts if not os.path.isfile(p)]
        if missing:
            self._emit("! firmware not found: %s\n" % ", ".join(missing))
            return
        # esptool resolution runs subprocesses, so do it in the worker (below) to
        # keep the UI responsive. Snapshot the widget values here on the UI thread.
        reconnect = self._begin_tool("connecting…")
        # Cancel is allowed while connecting; the "Chip is" marker closes the
        # window once esptool reports the chip (about to write).
        self._arm_cancel("flash cancelled while connecting (device not touched)")
        threading.Thread(
            target=self._run_flash,
            args=(port, parts, self.baud.get(), self.mode.get(),
                  self.erase.get(), reconnect),
            daemon=True).start()

    def _run_flash(self, port, parts, baud, mode, erase, reconnect):
        # try/finally guarantees _flash_done runs (re-enabling the buttons) even
        # if resolution or _pump raises unexpectedly.
        rc = 1
        try:
            esptool = resolve_esptool()
            if not esptool:
                self._emit("! no working esptool found. Install: brew install esptool\n")
            else:
                cmd = esptool + ["--port", port, "--baud", baud, "write_flash",
                                 "-fm", mode, "-fs", "detect"]
                if erase:
                    cmd.append("-e")
                for off, path in parts:
                    cmd += [off, path]
                names = ", ".join("%s @ %s" % (os.path.basename(p), o)
                                  for o, p in parts)
                self._emit("\n==> Flashing %s\n    %s\n" % (names, " ".join(cmd)))
                # "Chip is" is esptool's first line after a successful connect,
                # before any erase/write — the safe cutoff for cancellation.
                rc = self._pump(
                    cmd, uncancel_marker="Chip is",
                    on_uncancellable=lambda: self._lock_cancel("flashing (writing)…"))
        except Exception as e:  # never leave the UI stuck busy
            self._emit(f"\n! error: {e}\n")
        self.root.after(0, self._flash_done, rc, reconnect)

    def _arm_cancel(self, note):
        """Enable ✕ Cancel for the operation that is starting; note is what the
        log prints if the user cancels."""
        self._op_cancelled = False
        self._cancel_note = note
        self._can_cancel = True
        for b in self.cancel_btns:
            b.configure(state="normal")

    def _lock_cancel(self, status=None):
        """Close the cancel window — writing is imminent, aborting is no longer
        safe. Runs on the UI thread (via _pump's marker callback)."""
        self._can_cancel = False
        for b in self.cancel_btns:
            b.configure(state="disabled")
        if status:
            self._set_status(status, "#e0a800")

    def _cancel(self):
        """Abort the running operation while it's still safe — never mid-write
        (see _arm_cancel and the uncancel markers)."""
        proc = self._proc
        if not self._can_cancel or proc is None:
            return
        self._op_cancelled = True
        self._lock_cancel()
        try:
            proc.terminate()  # kills the tool; the worker then finishes via _pump
        except OSError:
            pass
        self._emit(f"\n--- {self._cancel_note} ---\n")

    def _flash_done(self, rc, reconnect):
        self._end_tool()
        if self._op_cancelled:
            self._set_status("cancelled", "#d9534f")
            if reconnect:
                self._start_monitor()  # device untouched — safe to reopen
        elif rc == 0:
            self._emit("\n==> Done. Device reset into the new firmware.\n")
            self._set_status("flashed ✓", "#1FA67A")
            if reconnect:
                self._start_monitor()  # show the boot log, like CoolTerm
        else:
            self._emit(f"\n! flash failed (exit {rc}). "
                       "Free the port (close CoolTerm) and retry.\n")
            self._set_status("flash failed", "#d9534f")

    # ---- NodeMCU Lua upload (optional tab) ----------------------------------
    def _run_nodemcu(self, subcmd, intro, status,
                     cancel_note="operation cancelled",
                     uncancel_marker=None, lock_status=None):
        """Shared launcher for the NodeMCU subcommands (upload / list / format).
        subcmd is the nodemcu-uploader subcommand + args (the port/baud and tool
        resolution are added in the worker so the UI stays responsive). Cancel is
        armed with cancel_note; when uncancel_marker is given, its appearance in
        the output locks cancel (and shows lock_status) — used by upload, whose
        writes start at 'Transferring'."""
        if self.busy:
            return
        if not self.port.get():
            self._emit("! no serial port selected\n")
            return
        reconnect = self._begin_tool(status)
        self._arm_cancel(cancel_note)
        self._emit(intro)
        threading.Thread(
            target=self._nodemcu_worker,
            args=(subcmd, self.port.get(), self.baud.get(), reconnect,
                  uncancel_marker, lock_status),
            daemon=True).start()

    def _nodemcu_worker(self, subcmd, port, baud, reconnect, marker, lock_status):
        # try/finally guarantees _nodemcu_done runs even if resolution/_pump raise.
        rc = 1
        try:
            tool = resolve_nodemcu()
            if not tool:
                self._emit("! NodeMCU uploader not found (expected bundled in "
                           "vendor/nodemcu_uploader)\n")
            else:
                on_lock = ((lambda: self._lock_cancel(lock_status))
                           if marker else None)
                rc = self._pump(tool + ["--port", port, "--baud", baud] + subcmd,
                                uncancel_marker=marker, on_uncancellable=on_lock)
        except Exception as e:  # never leave the UI stuck busy
            self._emit(f"\n! error: {e}\n")
        self.root.after(0, self._nodemcu_done, rc, reconnect)

    def _nodemcu_done(self, rc, reconnect):
        self._end_tool()
        if self._op_cancelled:
            self._set_status("cancelled", "#d9534f")
            if reconnect:
                self._start_monitor()
        elif rc == 0:
            self._emit("\n==> NodeMCU operation complete.\n")
            self._set_status("done ✓", "#1FA67A")
            if reconnect:
                self._start_monitor()
        else:
            self._emit(f"\n! NodeMCU operation failed (exit {rc}). Check the "
                       "board runs NodeMCU-Lua firmware and the port is free.\n")
            self._set_status("nodemcu failed", "#d9534f")

    def _upload(self):
        if self.busy:
            return
        files = list(self.lua_files.get(0, "end"))
        if not files:
            self._emit("! add at least one file to upload\n")
            return
        missing = [f for f in files if not os.path.isfile(f)]
        if missing:
            self._emit("! file(s) not found: %s\n" % ", ".join(missing))
            return
        subcmd = nodemcu_upload_flags(
            self.lua_compile.get(), self.lua_dofile.get(),
            self.lua_restart.get(), self.lua_verify.get()) + files
        self._run_nodemcu(
            subcmd, "\n==> Uploading %d file(s) to NodeMCU: %s\n"
            % (len(files), ", ".join(os.path.basename(f) for f in files)),
            "uploading…",
            cancel_note="upload cancelled before transfer (filesystem untouched)",
            uncancel_marker="Transferring", lock_status="uploading (writing)…")

    def _lua_list(self):
        self._run_nodemcu(["file", "list"],
                          "\n==> Listing files on the NodeMCU filesystem\n",
                          "listing…", cancel_note="list cancelled")

    def _lua_format(self):
        if not messagebox.askyesno(
                "Format filesystem",
                "Erase ALL files on the NodeMCU filesystem? This cannot be undone."):
            return
        self._run_nodemcu(["file", "format"],
                          "\n==> Formatting the NodeMCU filesystem\n",
                          "formatting…",
                          cancel_note="format cancelled (a format already sent "
                                      "still completes on the device)")

    def _apply_settings(self, s):
        """Restore remembered choices, best-effort: unknown or stale values
        (a baud not in the list, an unplugged port) are silently ignored.
        Deliberately NOT restored: erase, offset, parts — per-flash decisions
        that would be dangerous to carry across sessions."""
        if s.get("baud") in BAUDS:
            self.baud.set(s["baud"])
        if s.get("mode") in MODES:
            self.mode.set(s["mode"])
        if s.get("line_ending") in LINE_ENDINGS:
            self.line_ending.set(s["line_ending"])
        if s.get("lua_verify") in ("none", "raw", "sha1"):
            self.lua_verify.set(s["lua_verify"])
        if s.get("port") in (self.port["values"] or ()):
            self.port.set(s["port"])
        geo = s.get("geometry", "")
        if isinstance(geo, str) and re.fullmatch(r"\d+x\d+([+-]\d+[+-]\d+)?", geo):
            try:
                self.root.geometry(geo)
            except tk.TclError:
                pass

    def _on_close(self):
        save_settings({
            "port": self.port.get(),
            "baud": self.baud.get(),
            "mode": self.mode.get(),
            "line_ending": self.line_ending.get(),
            "lua_verify": self.lua_verify.get(),
            "geometry": self.root.winfo_geometry(),
        })
        self._stop_monitor()
        if self.logfile is not None:
            try:
                self.logfile.close()
            except OSError:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    FlasherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
