"""Unit checks for the Agent cursor event bridge."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from browser_backend import BrowserBackend, Observation


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

    def test_cursor_event_waits_for_native_motion_before_next_action(self):
        backend = BrowserBackend()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend.KERNEL_PORT_FILE = root / "port"
            backend.KERNEL_TOKEN_FILE = root / "token"
            backend.KERNEL_PORT_FILE.write_text("1234", encoding="utf-8")
            backend.KERNEL_TOKEN_FILE.write_text("test", encoding="utf-8")
            backend.kernel_available = lambda: True
            backend.kernel_task = lambda *args, **kwargs: {
                "ok": True, "cursor_ms": 180, "wall_ms": 35}
            with patch("browser_backend.time.sleep") as sleep:
                self.assertTrue(backend.cursor_event("env-1", 80, 90, "click"))
            sleep.assert_called_once_with(0.145)

    def test_typing_waits_for_cursor_event_before_input(self):
        backend = BrowserBackend()
        events = []

        class FakeCdp:
            def send(self, method, params=None, timeout=10.0):
                events.append(("input", method))
                return {"result": {}}

        backend.observations["env-1"] = Observation(
            env="env-1", epoch=1,
            items=[{"id": "e1", "bbox": [20, 30, 120, 32],
                    "name": "email", "selector": "#email"}],
        )
        backend._cdp = lambda env: FakeCdp()

        def cursor_event(env, x, y, action):
            events.extend([("cursor_start", action), ("cursor_done", action)])
            return True

        backend.cursor_event = cursor_event
        result = backend.type_text("env-1", "e1", "ab")
        self.assertEqual(result["typed"], "ab")
        self.assertEqual(events[0], ("cursor_start", "type"))
        self.assertEqual(events[1], ("cursor_done", "type"))
        self.assertEqual(events[2], ("input", "Input.dispatchMouseEvent"))

    def test_navigation_moves_cursor_before_page_navigate(self):
        backend = BrowserBackend()
        events = []

        class FakeCdp:
            def evaluate(self, expression, timeout=10.0, await_promise=False):
                events.append(("evaluate", expression))
                return '{"x": 400, "y": 300}'

            def send(self, method, params=None, timeout=10.0):
                events.append(("send", method))
                return {"result": {"loaderId": "loader"}}

        backend._cdp = lambda env: FakeCdp()
        backend.env_state = lambda: {"active": "env-1"}
        backend.cursor_event = lambda env, x, y, action: events.append(
            ("cursor", action, x, y)) or True
        result = backend.goto("env-1", "https://example.test", wait_ms=0)
        self.assertEqual(result["waited_ms"], 0)
        self.assertEqual(events[1], ("cursor", "navigate", 400.0, 300.0))
        self.assertEqual(events[2], ("send", "Page.navigate"))


if __name__ == "__main__":
    unittest.main()
