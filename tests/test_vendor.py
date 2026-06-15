"""Vendor integrity: the bundled tools must run with no install and stay
portable (pure-python, no compiled binaries). Independent of tkinter."""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(ROOT, "vendor")


def _env():
    pp = VENDOR
    if os.environ.get("PYTHONPATH"):
        pp += os.pathsep + os.environ["PYTHONPATH"]
    return dict(os.environ, PYTHONPATH=pp)


class TestVendor(unittest.TestCase):
    def test_esptool_runs(self):
        r = subprocess.run([sys.executable, os.path.join(VENDOR, "esptool.py"),
                            "version"], capture_output=True, env=_env())
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_nodemcu_uploader_runs(self):
        r = subprocess.run([sys.executable, "-m", "nodemcu_uploader", "--version"],
                           capture_output=True, env=_env())
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_pyserial_imports_and_comports(self):
        if VENDOR not in sys.path:
            sys.path.insert(0, VENDOR)
        import serial
        from serial.tools.list_ports import comports
        self.assertGreaterEqual(tuple(int(x) for x in serial.VERSION.split(".")[:2]),
                                (3, 4))
        self.assertIsInstance(list(comports()), list)  # callable, no crash

    def test_no_compiled_binaries(self):
        bad = []
        for dirpath, _dirs, files in os.walk(VENDOR):
            for f in files:
                if f.endswith((".so", ".dylib", ".pyd")):
                    bad.append(os.path.join(dirpath, f))
        self.assertEqual(bad, [], f"compiled binaries break portability: {bad}")


if __name__ == "__main__":
    unittest.main()
