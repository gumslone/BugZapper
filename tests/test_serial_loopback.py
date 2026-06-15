"""Exercise the pyserial read/write path that the monitor relies on, without
hardware, using a pty as a fake serial device. Unix-only (Windows has no pty);
skipped if the platform/pyserial can't open one."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(ROOT, "vendor")
if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

try:
    import pty
    HAS_PTY = hasattr(pty, "openpty") and sys.platform != "win32"
except ImportError:
    HAS_PTY = False


@unittest.skipUnless(HAS_PTY, "no pty (Windows or unsupported)")
class TestSerialLoopback(unittest.TestCase):
    def setUp(self):
        import serial
        self.serial = serial
        try:
            self.master, slave = pty.openpty()
            self.ser = serial.Serial(os.ttyname(slave), 115200, timeout=1)
        except Exception as e:  # some CI ptys reject termios baud changes
            self.skipTest(f"pty/pyserial unavailable: {e}")

    def tearDown(self):
        try:
            self.ser.close()
            os.close(self.master)
        except OSError:
            pass

    def test_read_from_device(self):
        # Bytes written to the pty master are read back through pyserial — the
        # same read path _read_monitor() uses (ser.read + decode).
        os.write(self.master, b"boot ok\r\n")
        got = self.ser.read(9)
        self.assertEqual(got, b"boot ok\r\n")
        self.assertEqual(got.decode("utf-8", "replace"), "boot ok\r\n")

    def test_write_to_device(self):
        # Mirrors _send(): write text + line ending to the port.
        self.ser.write(b"reset\n")
        self.assertEqual(os.read(self.master, 6), b"reset\n")

    def test_live_baud_change(self):
        # _on_baud_change sets ser.baudrate in place on the open port. Use a
        # standard rate: non-standard ones (e.g. 74880) go through a special
        # ioctl that ptys reject — a pty quirk, not our code.
        try:
            self.ser.baudrate = 9600
        except OSError as e:
            self.skipTest(f"pty rejects baud change: {e}")
        self.assertEqual(self.ser.baudrate, 9600)


if __name__ == "__main__":
    unittest.main()
