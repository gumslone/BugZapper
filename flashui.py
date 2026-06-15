#!/usr/bin/env python3
"""BugZapper — a small Tkinter GUI to flash ESP8266/ESP8285 firmware, upload
NodeMCU Lua files, and watch the serial output, in one window (no separate
PyFlasher + CoolTerm + nodemcu-uploader).

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
import os
import re
import subprocess
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox, simpledialog

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
    return sorted(glob.glob(os.path.join(FW_DIR, "*.bin")))


def files_in_folder(folder, exts):
    """Top-level files in folder matching the given extensions. exts is a string
    of space/comma-separated extensions in any form (lua, .lua, *.lua); empty or
    a '*'/'all' token means every file. Not recursive — the NodeMCU filesystem is
    flat, so pulling from subfolders would just flatten and collide."""
    raw = [t.strip().lower() for t in re.split(r"[,\s]+", exts or "") if t.strip()]
    files = sorted(p for p in glob.glob(os.path.join(folder, "*"))
                   if os.path.isfile(p))
    if not raw or "*" in raw or "*.*" in raw or "all" in raw:
        return files
    tokens = [t.lstrip("*").lstrip(".") for t in raw]  # *.lua / .lua / lua -> lua
    return [p for p in files
            if os.path.splitext(p)[1].lstrip(".").lower() in tokens]


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

        self._build_header()
        self._build_tabs()
        self._build_log()
        self._build_send()
        # Action buttons disabled while an external tool (esptool / uploader) runs.
        self.action_btns = [self.flash_btn, self.monitor_btn, self.upload_btn,
                            self.lualist_btn, self.luaformat_btn]
        self._refresh_ports(select_first=True)
        self._refresh_firmware()

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

    def _build_log(self):
        # padx/pady give inner padding so text isn't flush against the edges;
        # bd/relief flat keeps the border clean.
        self.log = scrolledtext.ScrolledText(self.root, height=20, wrap="char",
                                             bg="#1e1e1e", fg="#d4d4d4",
                                             insertbackground="#d4d4d4",
                                             font=("Menlo", 11),
                                             padx=10, pady=8,
                                             bd=0, relief="flat")
        self.log.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        for code, hexc in PALETTE.items():
            self.log.tag_configure(f"fg{code}", foreground=hexc)
        self.log.tag_configure("bold", font=("Menlo", 11, "bold"))
        self.log.configure(state="disabled")

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
        for b in self.action_btns:
            b.configure(state="normal")

    def _pump(self, cmd):
        """Run cmd, stream its combined stdout+stderr to the log, return the exit
        code. tool_env(): bundled pyserial on PYTHONPATH + NO_COLOR (we render or
        strip any remaining ANSI ourselves)."""
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=tool_env())
            fd = proc.stdout.fileno()
            while True:
                data = os.read(fd, 512)
                if not data:
                    break
                self._emit(data.decode("utf-8", "replace"))
            return proc.wait()
        except OSError as e:
            self._emit(f"\n! error: {e}\n")
            return 1

    # ---- flashing -----------------------------------------------------------
    def _flash(self):
        if self.busy:
            return
        port = self.port.get()
        fw = self.firmware.get()
        if not port:
            self._emit("! no serial port selected\n")
            return
        if not fw or not os.path.isfile(fw):
            self._emit(f"! firmware not found: {fw}\n")
            return
        esptool = resolve_esptool()
        if not esptool:
            self._emit("! no working esptool found. Install: brew install esptool\n")
            return

        cmd = esptool + ["--port", port, "--baud", self.baud.get(),
                         "write_flash", "-fm", self.mode.get(), "-fs", "detect"]
        if self.erase.get():
            cmd.append("-e")
        cmd += ["0x0", fw]

        reconnect = self._begin_tool("flashing…")
        self._emit("\n==> Flashing %s\n    %s\n" % (os.path.basename(fw),
                                                    " ".join(cmd)))
        threading.Thread(target=self._run_flash, args=(cmd, reconnect),
                         daemon=True).start()

    def _run_flash(self, cmd, reconnect):
        rc = self._pump(cmd)
        self.root.after(0, self._flash_done, rc, reconnect)

    def _flash_done(self, rc, reconnect):
        self._end_tool()
        if rc == 0:
            self._emit("\n==> Done. Device reset into the new firmware.\n")
            self._set_status("flashed ✓", "#1FA67A")
            if reconnect:
                self._start_monitor()  # show the boot log, like CoolTerm
        else:
            self._emit(f"\n! flash failed (exit {rc}). "
                       "Free the port (close CoolTerm) and retry.\n")
            self._set_status("flash failed", "#d9534f")

    # ---- NodeMCU Lua upload (optional tab) ----------------------------------
    def _nodemcu_cmd(self):
        """Base nodemcu-uploader argv with the selected port/baud, or None (and
        an error in the log) if no port is chosen or the uploader is missing."""
        port = self.port.get()
        if not port:
            self._emit("! no serial port selected\n")
            return None
        tool = resolve_nodemcu()
        if not tool:
            self._emit("! NodeMCU uploader not found (expected bundled in "
                       "vendor/nodemcu_uploader)\n")
            return None
        return tool + ["--port", port, "--baud", self.baud.get()]

    def _run_nodemcu(self, cmd, intro, status):
        """Shared launcher for the NodeMCU subcommands (upload / list / format)."""
        if self.busy:
            return
        reconnect = self._begin_tool(status)
        self._emit(intro)
        threading.Thread(target=self._nodemcu_worker, args=(cmd, reconnect),
                         daemon=True).start()

    def _nodemcu_worker(self, cmd, reconnect):
        rc = self._pump(cmd)
        self.root.after(0, self._nodemcu_done, rc, reconnect)

    def _nodemcu_done(self, rc, reconnect):
        self._end_tool()
        if rc == 0:
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
        cmd = self._nodemcu_cmd()
        if cmd is None:
            return
        cmd += nodemcu_upload_flags(self.lua_compile.get(), self.lua_dofile.get(),
                                    self.lua_restart.get(), self.lua_verify.get())
        cmd += files
        self._run_nodemcu(cmd, "\n==> Uploading %d file(s) to NodeMCU\n    %s\n"
                          % (len(files), " ".join(cmd)), "uploading…")

    def _lua_list(self):
        cmd = self._nodemcu_cmd()
        if cmd is None:
            return
        self._run_nodemcu(cmd + ["file", "list"],
                          "\n==> Listing files on the NodeMCU filesystem\n",
                          "listing…")

    def _lua_format(self):
        if not messagebox.askyesno(
                "Format filesystem",
                "Erase ALL files on the NodeMCU filesystem? This cannot be undone."):
            return
        cmd = self._nodemcu_cmd()
        if cmd is None:
            return
        self._run_nodemcu(cmd + ["file", "format"],
                          "\n==> Formatting the NodeMCU filesystem\n",
                          "formatting…")

    def _on_close(self):
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
