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
        self.assertEqual(len(app.action_btns), 5)
        for attr in ("lua_files", "upload_btn", "flash_btn", "monitor_btn"):
            self.assertTrue(hasattr(app, attr))
        # Plain Tk root => drag-and-drop registration must report disabled, never
        # raise (the Add… button is the fallback).
        self.assertFalse(app.dnd_enabled)
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
