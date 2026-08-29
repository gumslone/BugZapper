"""GUI module (flashui): pure helpers always; a Tk-construction smoke test when
a display is available. Importing flashui needs _tkinter built into python; if
it isn't, the whole module is skipped (the CLI/vendor tests still cover the
core)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    import flashui
    IMPORT_ERR = None
except Exception as e:  # missing _tkinter, etc.
    flashui = None
    IMPORT_ERR = e


def _has_display():
    if flashui is None:
        return False
    try:
        import tkinter as tk
        r = tk.Tk()
        r.destroy()
        return True
    except Exception:
        return False


HAS_DISPLAY = _has_display()


@unittest.skipIf(flashui is None, f"flashui import failed: {IMPORT_ERR}")
class TestHelpers(unittest.TestCase):
    def test_resolvers_executable(self):
        import subprocess
        esp = flashui.resolve_esptool()
        nmu = flashui.resolve_nodemcu()
        self.assertIsNotNone(esp)
        self.assertIsNotNone(nmu)
        self.assertEqual(subprocess.run(esp + ["version"], capture_output=True,
                                        env=flashui.tool_env()).returncode, 0)

    def test_tool_env(self):
        env = flashui.tool_env()
        self.assertEqual(env["NO_COLOR"], "1")
        self.assertTrue(env["PYTHONPATH"].startswith(flashui.VENDOR))

    def test_list_ports_returns_list(self):
        self.assertIsInstance(flashui.list_ports(), list)

    def test_ansi_stripping(self):
        s = "\x1b[0;32mok\x1b[0m done\x1b[2K"
        self.assertEqual(flashui.ANSI_RE.sub("", s), "ok done")

    def test_nodemcu_upload_flags(self):
        self.assertEqual(flashui.nodemcu_upload_flags(), ["upload"])
        self.assertEqual(
            flashui.nodemcu_upload_flags(compile_lc=True, dofile=True,
                                         restart=True, verify="sha1"),
            ["upload", "-c", "-e", "-r", "-v", "sha1"])
        self.assertNotIn("-v", flashui.nodemcu_upload_flags(verify="none"))

    def test_files_in_folder_by_extension(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            names = ["init.lua", "app.lua", "index.html", "data.json", "readme.md"]
            for n in names:
                open(os.path.join(d, n), "w").close()
            os.mkdir(os.path.join(d, "sub"))  # dirs are ignored
            open(os.path.join(d, "sub", "deep.lua"), "w").close()

            def base(paths):
                return sorted(os.path.basename(p) for p in paths)

            # accepts bare / dotted / globbed extension spellings
            self.assertEqual(base(flashui.files_in_folder(d, "lua")),
                             ["app.lua", "init.lua"])
            self.assertEqual(base(flashui.files_in_folder(d, "lua, html")),
                             ["app.lua", "index.html", "init.lua"])
            self.assertEqual(base(flashui.files_in_folder(d, "*.html .json")),
                             ["data.json", "index.html"])
            # '*' / empty => all top-level files, never recursing into sub/
            self.assertEqual(base(flashui.files_in_folder(d, "*")), sorted(names))
            self.assertEqual(base(flashui.files_in_folder(d, "")), sorted(names))
            self.assertNotIn("deep.lua", base(flashui.files_in_folder(d, "lua")))

    def test_parse_offset(self):
        self.assertEqual(flashui.parse_offset("0x1000"), "0x1000")
        self.assertEqual(flashui.parse_offset(" 4096 "), "4096")
        self.assertEqual(flashui.parse_offset(""), "0x0")
        self.assertEqual(flashui.parse_offset(None), "0x0")
        for bad in ("banana", "0x", "1000h"):
            with self.assertRaises(ValueError):
                flashui.parse_offset(bad)

    def test_suggest_offset(self):
        self.assertEqual(flashui.suggest_offset("bootloader.bin"), "0x1000")
        self.assertEqual(flashui.suggest_offset("BOOTLOADER_dio_40m.bin"), "0x1000")
        self.assertEqual(flashui.suggest_offset("/a/b/partitions.bin"), "0x8000")
        self.assertEqual(flashui.suggest_offset("partition-table.bin"), "0x8000")
        self.assertEqual(flashui.suggest_offset("boot_app0.bin"), "0xe000")
        self.assertEqual(flashui.suggest_offset("ota_data_initial.bin"), "0xe000")
        # generic names must NOT get an ESP32 hint (could be an ESP8266 image)
        self.assertIsNone(flashui.suggest_offset("app.bin"))
        self.assertIsNone(flashui.suggest_offset("firmware-v1.2.bin"))
        self.assertIsNone(flashui.suggest_offset("myapp.bin"))

    def test_scan_esp32_folder(self):
        import os
        import tempfile

        def touch(d, *names):
            for n in names:
                open(os.path.join(d, n), "w").close()

        def scanned(d):
            parts, leftovers = flashui.scan_esp32_folder(d)
            return ([(o, os.path.basename(p)) for o, p in parts],
                    sorted(os.path.basename(p) for p in leftovers))

        # full IDF-style build: parts at conventional offsets, single
        # unrecognized .bin promoted to the app slot, sorted by offset
        with tempfile.TemporaryDirectory() as d:
            touch(d, "bootloader.bin", "partitions.bin", "boot_app0.bin",
                  "myapp.bin", "notes.txt")
            self.assertEqual(scanned(d), ([("0x1000", "bootloader.bin"),
                                           ("0x8000", "partitions.bin"),
                                           ("0xe000", "boot_app0.bin"),
                                           ("0x10000", "myapp.bin")], []))

        # only unrecognizable bins: nothing is guessed (no bogus app slot)
        with tempfile.TemporaryDirectory() as d:
            touch(d, "a.bin", "b.bin")
            self.assertEqual(scanned(d), ([], ["a.bin", "b.bin"]))

        # two unrecognized bins: neither is promoted to the app slot
        with tempfile.TemporaryDirectory() as d:
            touch(d, "bootloader.bin", "x.bin", "y.bin")
            self.assertEqual(scanned(d), ([("0x1000", "bootloader.bin")],
                                          ["x.bin", "y.bin"]))

        # duplicate for a taken offset goes to leftovers, never the app slot
        with tempfile.TemporaryDirectory() as d:
            touch(d, "bootloader.bin", "bootloader_dio.bin", "app-img.bin")
            parts, leftovers = scanned(d)
            self.assertEqual(parts[0], ("0x1000", "bootloader.bin"))
            self.assertIn(("0x10000", "app-img.bin"), parts)
            self.assertEqual(leftovers, ["bootloader_dio.bin"])

    def test_files_in_folder_glob_metachars_in_path(self):
        # A folder whose path contains glob metacharacters must still match its
        # files — glob.escape guards against reading them as a pattern. Use only
        # brackets: *, ?, etc. are glob metachars too but are illegal in Windows
        # filenames, and [ ] alone already exercises the escape.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as parent:
            d = os.path.join(parent, "fw [v2] drafts")
            os.mkdir(d)
            open(os.path.join(d, "init.lua"), "w").close()
            open(os.path.join(d, "app.lua"), "w").close()
            got = sorted(os.path.basename(p) for p in flashui.files_in_folder(d, "lua"))
            self.assertEqual(got, ["app.lua", "init.lua"])

    def test_firmware_dir_env_override(self):
        old_argv, old_env = sys.argv, os.environ.get("BUGZAPPER_FW_DIR")
        try:
            sys.argv = ["flashui.py"]  # no argv[1]
            os.environ["BUGZAPPER_FW_DIR"] = "/tmp/some-fw-dir"
            self.assertEqual(flashui.firmware_dir(), "/tmp/some-fw-dir")
        finally:
            sys.argv = old_argv
            if old_env is None:
                os.environ.pop("BUGZAPPER_FW_DIR", None)
            else:
                os.environ["BUGZAPPER_FW_DIR"] = old_env


@unittest.skipUnless(HAS_DISPLAY, "no Tk display (headless without xvfb)")
class TestGuiSmoke(unittest.TestCase):
    def test_app_builds_and_tears_down(self):
        import tkinter as tk
        root = tk.Tk()
        app = flashui.FlasherApp(root)
        root.update_idletasks()
        self.assertIsNone(app.monitor_ser)
        self.assertEqual(len(app.action_btns), 6)
        for attr in ("lua_files", "upload_btn", "flash_btn", "monitor_btn",
                     "chipinfo_btn"):
            self.assertTrue(hasattr(app, attr))
        root.destroy()

    def test_add_remove_flash_parts(self):
        import os
        import tempfile
        import tkinter as tk
        root = tk.Tk()
        app = flashui.FlasherApp(root)
        with tempfile.TemporaryDirectory() as d:
            f1 = os.path.join(d, "boot.bin")
            f2 = os.path.join(d, "app.bin")
            open(f1, "w").close()
            open(f2, "w").close()
            app.firmware.set(f1)
            app.fw_offset.delete(0, "end")
            app.fw_offset.insert(0, "0x1000")
            app._add_part()
            app.firmware.set(f2)
            app.fw_offset.delete(0, "end")
            app.fw_offset.insert(0, "0x10000")
            app._add_part()
            self.assertEqual(app._parts, [("0x1000", f1), ("0x10000", f2)])
            self.assertEqual(app.parts_box.size(), 2)
            # duplicate offset is rejected
            app._add_part()
            self.assertEqual(len(app._parts), 2)
            # remove keeps list and box in sync
            app.parts_box.selection_set(0)
            app._remove_part()
            self.assertEqual(app._parts, [("0x10000", f2)])
            app._clear_parts()
            self.assertEqual(app._parts, [])
            self.assertEqual(app.parts_box.size(), 0)
            # a recognizable ESP32 part at an odd offset logs a hint but is
            # still queued at the offset the user chose
            bl = os.path.join(d, "bootloader.bin")
            open(bl, "w").close()
            app.firmware.set(bl)
            app.fw_offset.delete(0, "end")
            app.fw_offset.insert(0, "0x0")
            while not app.q.empty():  # drop earlier messages
                app.q.get_nowait()
            app._add_part()
            msgs = []
            while not app.q.empty():
                msgs.append(app.q.get_nowait())
            self.assertTrue(any("hint:" in m and "0x1000" in m for m in msgs))
            self.assertEqual(app._parts, [("0x0", bl)])
        root.destroy()

    def test_lua_add_paths_dedupes_and_skips_non_files(self):
        import os
        import tempfile
        import tkinter as tk
        root = tk.Tk()
        app = flashui.FlasherApp(root)
        with tempfile.TemporaryDirectory() as d:
            f1 = os.path.join(d, "init.lua")
            f2 = os.path.join(d, "app.lua")
            open(f1, "w").close()
            open(f2, "w").close()
            missing = os.path.join(d, "nope.lua")
            # f1 twice (dupe), a real file, a directory, a missing path.
            app._lua_add_paths([f1, f1, f2, d, missing])
            got = list(app.lua_files.get(0, "end"))
        root.destroy()
        self.assertEqual(got, [f1, f2])  # deduped; dir + missing skipped


if __name__ == "__main__":
    unittest.main()
