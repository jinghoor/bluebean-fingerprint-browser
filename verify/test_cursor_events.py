"""Unit checks for the Agent cursor event bridge."""

import tempfile
import unittest
from pathlib import Path

from browser_backend import BrowserBackend


class CursorEventTest(unittest.TestCase):
    def test_cursor_event_is_best_effort_and_keeps_coordinates(self):
        backend = BrowserBackend()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend.KERNEL_PORT_FILE = root / "port"
            backend.KERNEL_TOKEN_FILE = root / "token"
            backend.KERNEL_PORT_FILE.write_text("1234", encoding="utf-8")
            backend.KERNEL_TOKEN_FILE.write_text("test", encoding="utf-8")
            calls = []

            def fake_kernel(env, ops, timeout=10.0, cursor=None):
                calls.append((env, ops, cursor))
                return {"ok": True}

            backend.kernel_task = fake_kernel
            backend.kernel_available = lambda: True
            self.assertTrue(backend.cursor_event("env-1", 12.5, 44.0, "click"))
            self.assertEqual(calls, [("env-1", [],
                                      {"x": 12.5, "y": 44.0, "action": "click"})])

    def test_cursor_event_does_not_fail_when_native_channel_is_absent(self):
        backend = BrowserBackend()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend.KERNEL_PORT_FILE = root / "missing-port"
            backend.KERNEL_TOKEN_FILE = root / "missing-token"
            self.assertFalse(backend.cursor_event("env-1", 1, 2))

    def test_cursor_event_skips_stale_native_channel(self):
        backend = BrowserBackend()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend.KERNEL_PORT_FILE = root / "port"
            backend.KERNEL_TOKEN_FILE = root / "token"
            backend.KERNEL_PORT_FILE.write_text("1234", encoding="utf-8")
            backend.KERNEL_TOKEN_FILE.write_text("test", encoding="utf-8")
            backend.kernel_available = lambda: False
            backend.kernel_task = lambda *args, **kwargs: self.fail("stale port must not be retried")
            self.assertFalse(backend.cursor_event("env-1", 1, 2))


if __name__ == "__main__":
    unittest.main()
