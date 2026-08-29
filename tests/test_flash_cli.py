"""The cross-platform CLI (flash.py): command construction (unit) + end-to-end
behavior via subprocess (help / list / error paths). No tkinter needed."""
import importlib.util
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLASH_PY = os.path.join(ROOT, "flash.py")

# Import flash.py as a module without executing main() (it's guarded by __main__).
_spec = importlib.util.spec_from_file_location("flash_cli", FLASH_PY)
flash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flash)


def _run(*args):
    return subprocess.run([sys.executable, FLASH_PY, *args],
                          capture_output=True, text=True, cwd=ROOT)


class TestParsePart(unittest.TestCase):
    def test_offset_prefix(self):
        self.assertEqual(flash.parse_part("0x1000:boot.bin"),
                         ("0x1000", "boot.bin"))

    def test_decimal_offset(self):
        self.assertEqual(flash.parse_part("4096:app.bin"), ("4096", "app.bin"))

    def test_plain_file_defaults_to_zero(self):
        self.assertEqual(flash.parse_part("app.bin"), ("0x0", "app.bin"))

    def test_windows_drive_is_not_an_offset(self):
        self.assertEqual(flash.parse_part(r"C:\fw\app.bin"),
                         ("0x0", r"C:\fw\app.bin"))

    def test_offset_before_windows_path(self):
        self.assertEqual(flash.parse_part(r"0x10000:C:\fw\app.bin"),
                         ("0x10000", r"C:\fw\app.bin"))


class TestBuildFlashCmd(unittest.TestCase):
    def test_basic_command_shape(self):
        cmd = flash.build_flash_cmd(["esptool"], "COM5", 115200, "dio", False,
                                    [("0x0", "fw.bin")])
        self.assertEqual(cmd, ["esptool", "--port", "COM5", "--baud", "115200",
                               "write_flash", "-fm", "dio", "-fs", "detect",
                               "0x0", "fw.bin"])

    def test_erase_adds_flag_before_offset(self):
        cmd = flash.build_flash_cmd(["esptool"], "p", 460800, "qio", True,
                                    [("0x0", "a.bin")])
        self.assertIn("-e", cmd)
        self.assertEqual(cmd[-2:], ["0x0", "a.bin"])
        self.assertLess(cmd.index("-e"), cmd.index("0x0"))

    def test_multi_part_pairs_in_order(self):
        parts = [("0x1000", "boot.bin"), ("0x8000", "part.bin"),
                 ("0x10000", "app.bin")]
        cmd = flash.build_flash_cmd(["esptool"], "p", 115200, "dio", False, parts)
        self.assertEqual(cmd[-6:], ["0x1000", "boot.bin", "0x8000", "part.bin",
                                    "0x10000", "app.bin"])

    def test_baud_is_stringified(self):
        cmd = flash.build_flash_cmd(["e"], "p", 115200, "dio", False,
                                    [("0x0", "f")])
        self.assertIn("115200", cmd)

    def test_resolve_esptool_finds_bundled(self):
        esptool = flash.resolve_esptool()
        self.assertIsNotNone(esptool)
        r = subprocess.run(esptool + ["version"], capture_output=True,
                           env=flash.tool_env())
        self.assertEqual(r.returncode, 0)


class TestFlashCli(unittest.TestCase):
    def test_help_exits_zero(self):
        r = _run("-h")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage", r.stdout.lower())

    def test_list_exits_zero(self):
        self.assertEqual(_run("-l").returncode, 0)

    def test_invalid_mode_rejected(self):
        r = _run("-m", "bogus")
        self.assertEqual(r.returncode, 2)  # argparse usage error

    def test_missing_firmware_errors(self):
        # Give a port so it reaches the firmware check, but no .bin exists here.
        r = subprocess.run([sys.executable, FLASH_PY, "-p", "/dev/null", "-f",
                            os.path.join(ROOT, "does-not-exist.bin")],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not found", r.stderr.lower())


if __name__ == "__main__":
    unittest.main()
