#!/usr/bin/env python3
"""BugZapper — a small Tkinter GUI to flash ESP8266/ESP8285 firmware and watch
the serial output, in one window (no separate PyFlasher + CoolTerm).

Launched by bugzapper.sh (which picks a python3 that has tkinter). Flashing uses
the bundled esptool; the serial monitor opens the port directly (stty + fd
read), so no pyserial is required.

Drop-in for any project. Customize without editing this file:
  BUGZAPPER_TITLE   window title            (default "BugZapper")
  BUGZAPPER_ICON    path to a PNG icon      (default ./icon.png next to this file)
  BUGZAPPER_FW_DIR  folder of .bin files    (default: ./firmware, else cwd)
  argv[1]           a firmware folder, overrides BUGZAPPER_FW_DIR
"""
import glob
import os
import re
import select
import subprocess
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext

HERE = os.path.dirname(os.path.abspath(__file__))
# Bundled pure-python esptool + pyserial, so flashing needs no local install.
VENDOR = os.path.join(HERE, "vendor")
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
PORT_GLOBS = ("/dev/cu.usbserial*", "/dev/cu.SLAB*", "/dev/cu.wchusb*",
              "/dev/cu.usbmodem*", "/dev/tty.usbserial*",
              "/dev/ttyUSB*", "/dev/ttyACM*")

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
    found = []
    for pat in PORT_GLOBS:
        found += glob.glob(pat)
    return sorted(set(found))


def list_firmware():
    return sorted(glob.glob(os.path.join(FW_DIR, "*.bin")))


def esptool_env():
    """Env for running esptool: bundled pyserial on PYTHONPATH, and NO_COLOR
    (we render/strip ANSI ourselves)."""
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
                              env=esptool_env()).returncode == 0:
                return cand
        except (FileNotFoundError, OSError):
            continue
    return None


class FlasherApp:
    def __init__(self, root):
        self.root = root
        root.title(TITLE)
        root.minsize(720, 520)
        self._set_icon()

        self.q = queue.Queue()
        self.monitor_fd = None
        self.monitor_stop = threading.Event()
        self.logfile = None  # open file handle when "Log to file" is active
        self._sgr_fg = None   # current ANSI foreground (None = default)
        self._sgr_bold = False
        self.busy = False  # flashing in progress

        self._build_controls()
        self._build_log()
        self._build_send()
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
    def _build_controls(self):
        f = ttk.Frame(self.root, padding=10)
        f.pack(fill="x")
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Serial port").grid(row=0, column=0, sticky="w", pady=3)
        self.port = ttk.Combobox(f, state="readonly", width=34)
        self.port.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="Refresh", command=self._refresh_ports).grid(row=0, column=2)

        ttk.Label(f, text="Firmware").grid(row=1, column=0, sticky="w", pady=3)
        self.firmware = ttk.Combobox(f, width=34)
        self.firmware.grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="Browse…", command=self._browse_fw).grid(row=1, column=2)

        row2 = ttk.Frame(f)
        row2.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(row2, text="Baud").pack(side="left")
        self.baud = ttk.Combobox(row2, state="readonly", width=8, values=BAUDS)
        self.baud.set("115200")
        self.baud.pack(side="left", padx=(4, 16))
        # retune a live monitor when the baud changes (e.g. 74880 boot ROM <-> 115200)
        self.baud.bind("<<ComboboxSelected>>", self._on_baud_change)
        ttk.Label(row2, text="Flash mode").pack(side="left")
        self.mode = ttk.Combobox(row2, state="readonly", width=6, values=MODES)
        self.mode.set("dio")
        self.mode.pack(side="left", padx=(4, 16))
        self.erase = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="Erase flash (wipes all data)",
                        variable=self.erase).pack(side="left")

        btns = ttk.Frame(f)
        btns.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.flash_btn = ttk.Button(btns, text="⚡ Flash", command=self._flash)
        self.flash_btn.pack(side="left")
        self.monitor_btn = ttk.Button(btns, text="▶ Connect monitor",
                                      command=self._toggle_monitor)
        self.monitor_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="Clear log", command=self._clear).pack(side="left")
        ttk.Button(btns, text="Save log…", command=self._save_log).pack(side="left", padx=6)
        self.logfile_btn = ttk.Button(btns, text="● Log to file",
                                      command=self._toggle_logfile)
        self.logfile_btn.pack(side="left")
        self.status = ttk.Label(btns, text="ready", foreground="#1FA67A")
        self.status.pack(side="right")

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

    # ---- serial monitor (stty + cat, no pyserial) ---------------------------
    def _toggle_monitor(self):
        if self.monitor_fd is not None:
            self._stop_monitor()
        else:
            self._start_monitor()

    def _start_monitor(self):
        port = self.port.get()
        if not port:
            self._emit("! no serial port selected\n")
            return
        baud = self.baud.get()
        # Open the port FIRST and keep it open, THEN set the baud. On macOS,
        # (re)opening a serial device resets it to the default baud — so the
        # old "stty …; cat" pattern read at the wrong rate (garbled text).
        # Holding this fd open keeps stty's setting from being reset.
        try:
            fd = os.open(port, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
        except OSError as e:
            self._emit(f"! could not open {port}: {e}\n")
            return
        try:
            self._apply_baud(port, baud)
        except (subprocess.CalledProcessError, OSError) as e:
            os.close(fd)
            self._emit(f"! could not configure {port} @ {baud}: {e}\n")
            return
        self.monitor_fd = fd
        self.monitor_stop.clear()
        self.monitor_btn.configure(text="■ Disconnect monitor")
        self._set_status(f"monitor @ {baud}", "#1FA67A")
        self._emit(f"--- monitor connected: {port} @ {baud} ---\n")
        self._emit("(a short gibberish burst at reset is the ESP boot ROM at "
                   "74880 baud; firmware output follows at the selected baud)\n")
        threading.Thread(target=self._read_monitor, args=(fd,),
                         daemon=True).start()

    def _send(self, *_):
        if self.monitor_fd is None:
            self._emit("! connect the monitor first to send\n")
            return
        msg = self.send_entry.get()
        ending = LINE_ENDINGS.get(self.line_ending.get(), "\n")
        try:
            os.write(self.monitor_fd, (msg + ending).encode("utf-8"))
        except OSError as e:
            self._emit(f"! send failed: {e}\n")
            return
        self._emit(f">> {msg}\n")
        self.send_entry.delete(0, "end")

    def _apply_baud(self, port, baud):
        """Set the line baud on the (already open) port via stty."""
        subprocess.run(["stty", "-f", port, baud, "cs8", "-cstopb",
                        "-parenb", "raw", "-echo"], check=True,
                       capture_output=True)

    def _on_baud_change(self, *_):
        """Retune the live monitor without reconnecting. The port stays open, so
        stty changes the rate in place and the reader keeps the same fd."""
        if self.monitor_fd is None:
            return
        baud = self.baud.get()
        try:
            self._apply_baud(self.port.get(), baud)
        except (subprocess.CalledProcessError, OSError) as e:
            self._emit(f"! could not set baud {baud}: {e}\n")
            return
        self._set_status(f"monitor @ {baud}", "#1FA67A")
        self._emit(f"--- baud changed to {baud} ---\n")

    def _read_monitor(self, fd):
        while not self.monitor_stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.2)
            except OSError:
                break
            if not ready:
                continue
            try:
                data = os.read(fd, 512)
            except BlockingIOError:
                continue
            except OSError:
                break
            if not data:  # EOF — device unplugged
                break
            self._emit(data.decode("utf-8", "replace"))

    def _stop_monitor(self):
        self.monitor_stop.set()
        fd = self.monitor_fd
        self.monitor_fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        self.monitor_btn.configure(text="▶ Connect monitor")
        self._set_status("ready")
        self._emit("--- monitor disconnected ---\n")

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

        was_monitoring = self.monitor_fd is not None
        if was_monitoring:
            self._stop_monitor()  # free the port for esptool

        cmd = esptool + ["--port", port, "--baud", self.baud.get(),
                         "write_flash", "-fm", self.mode.get(), "-fs", "detect"]
        if self.erase.get():
            cmd.append("-e")
        cmd += ["0x0", fw]

        self.busy = True
        self.flash_btn.configure(state="disabled")
        self.monitor_btn.configure(state="disabled")
        self._set_status("flashing…", "#e0a800")
        self._emit("\n==> Flashing %s\n    %s\n" % (os.path.basename(fw),
                                                    " ".join(cmd)))
        threading.Thread(target=self._run_flash, args=(cmd, was_monitoring),
                         daemon=True).start()

    def _run_flash(self, cmd, reconnect):
        rc = 1
        try:
            # esptool_env(): bundled pyserial on PYTHONPATH + NO_COLOR (we strip
            # any remaining ANSI ourselves).
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=esptool_env())
            fd = proc.stdout.fileno()
            while True:
                data = os.read(fd, 512)
                if not data:
                    break
                self._emit(data.decode("utf-8", "replace"))
            rc = proc.wait()
        except OSError as e:
            self._emit(f"\n! flash error: {e}\n")
        self.root.after(0, self._flash_done, rc, reconnect)

    def _flash_done(self, rc, reconnect):
        self.busy = False
        self.flash_btn.configure(state="normal")
        self.monitor_btn.configure(state="normal")
        if rc == 0:
            self._emit("\n==> Done. Device reset into the new firmware.\n")
            self._set_status("flashed ✓", "#1FA67A")
            if reconnect:
                self._start_monitor()  # show the boot log, like CoolTerm
        else:
            self._emit(f"\n! flash failed (exit {rc}). "
                       "Free the port (close CoolTerm) and retry.\n")
            self._set_status("flash failed", "#d9534f")

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
