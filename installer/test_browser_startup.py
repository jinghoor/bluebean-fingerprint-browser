"""Startup compatibility and early-exit regressions; no real user data or UI."""
from contextlib import redirect_stdout
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _browser_launch
import _env_driver
import fp_launcher
import open_env_window


class LaunchFlagsTests(unittest.TestCase):
    def test_windows_preserves_features_and_sandbox(self):
        original = ["chrome.exe", "--disable-features=CalculateNativeWinOcclusion,Other",
                    "--enable-features=BrowserDynamicCodeDisabled,Enabled",
                    "--disable-features=Other,Third", "https://example.invalid/"]
        with patch.object(_browser_launch.sys, "platform", "win32"):
            actual = _browser_launch.compatible_browser_argv(original)
            self.assertEqual(actual, _browser_launch.compatible_browser_argv(actual))
        switches = [arg for arg in actual if arg.startswith("--disable-features=")]
        self.assertEqual(switches, ["--disable-features=CalculateNativeWinOcclusion,Other,Third,BrowserDynamicCodeDisabled"])
        self.assertEqual(actual[0], "chrome.exe")
        self.assertIn(original[2], actual)
        self.assertIn(original[-1], actual)
        self.assertNotIn("--no-sandbox", actual)
        self.assertNotIn("--in-process-gpu", actual)
        self.assertEqual(len(original), 5, "input list must not be mutated")

    def test_feature_decorations_and_switch_terminator(self):
        with patch.object(_browser_launch.sys, "platform", "win32"):
            actual = _browser_launch.compatible_browser_argv([
                "chrome.exe", "--disable-features=BrowserDynamicCodeDisabled<Trial:param/value,Other",
                "--", "--disable-features=literal-url"])
            self.assertEqual(actual[1], "--disable-features=Other,BrowserDynamicCodeDisabled")
            self.assertEqual(actual[-2:], ["--", "--disable-features=literal-url"])
            self.assertEqual(_browser_launch.compatible_browser_argv(["chrome.exe", "--", "file"]),
                             ["chrome.exe", "--disable-features=BrowserDynamicCodeDisabled", "--", "file"])

    def test_non_windows_unchanged(self):
        flags = ["chrome", "--disable-features=Other", "about:blank"]
        with patch.object(_browser_launch.sys, "platform", "linux"):
            self.assertEqual(_browser_launch.compatible_browser_argv(flags), flags)

    def test_product_launch_uses_guard_even_with_extra_feature_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            browser = _env_driver.EnvBrowser("fixture-chrome", user_data_dir=home / "Profile",
                log_file=home / "browser.log", cdp_port=12345,
                extra_argv=["--disable-features=ExtraFeature"])
            with patch.object(_browser_launch.sys, "platform", "win32"), \
                 patch.object(_env_driver, "DATA_ROOT", home), \
                 patch.object(_env_driver.subprocess, "Popen") as popen, \
                 patch.object(browser, "wait_cdp", return_value=True), \
                 patch("_runtime_state.process_identity", return_value={}):
                popen.return_value.pid = 1234
                self.assertTrue(browser.start())
            argv = popen.call_args.args[0]
            self.assertIn("--disable-features=CalculateNativeWinOcclusion,ExtraFeature,BrowserDynamicCodeDisabled", argv)
            self.assertNotIn("--no-sandbox", argv)
            self.assertNotIn("--in-process-gpu", argv)

    def test_both_fingerprint_entrypoints_use_guard(self):
        fp = SimpleNamespace(window_size=(1280, 800), ui_language="en-US")
        args = SimpleNamespace(chrome="chrome.exe", headless=False, extra_arg=["--disable-features=Extra"],
                               no_url=False, url="about:blank")
        with patch.object(_browser_launch.sys, "platform", "win32"), \
             patch.object(fp_launcher, "resolve_proxy", return_value=([], "direct")):
            for argv in (fp_launcher.launch_argv(fp, extra=args.extra_arg),
                         fp_launcher.build_argv(args, fp, Path("fixture-profile"))):
                self.assertIn("--disable-features=Extra,BrowserDynamicCodeDisabled", argv)

    def test_packaging_import_closure_includes_guard(self):
        spec = importlib.util.spec_from_file_location("startup_builder", ROOT / "build-installer.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        self.assertIn("_browser_launch.py", builder.local_python_modules())


class ReadinessTests(unittest.TestCase):
    def browser(self):
        browser = _env_driver.EnvBrowser("fixture", user_data_dir=Path("fixture-profile"), cdp_port=12345)
        browser.proc = Mock()
        return browser

    def test_exit_code_three_does_not_wait_for_timeout(self):
        browser = self.browser()
        browser.proc.poll.return_value = 3
        with patch.object(_env_driver.urllib.request, "build_opener") as opener, \
             patch.object(_env_driver.time, "sleep") as sleep:
            self.assertFalse(browser.wait_cdp(timeout=30))
        opener.return_value.open.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(browser.startup_exit_code, 3)
        self.assertIn("0x00000003", browser.startup_error)

    def test_connection_failure_followed_by_exit_is_detected(self):
        browser = self.browser()
        browser.proc.poll.side_effect = [None, -1073741819]
        with patch.object(_env_driver.urllib.request, "build_opener") as opener, \
             patch.object(_env_driver.time, "sleep"):
            opener.return_value.open.side_effect = OSError("connection refused")
            self.assertFalse(browser.wait_cdp(timeout=30))
        self.assertEqual(opener.return_value.open.call_count, 1)
        self.assertIn("0xC0000005", browser.startup_error)

    def test_live_browser_readiness_bypasses_system_proxy(self):
        browser = self.browser()
        browser.proc.poll.return_value = None
        with patch.object(_env_driver.urllib.request, "build_opener") as opener, \
             patch.object(_env_driver.urllib.request, "ProxyHandler") as proxy:
            opener.return_value.open.return_value.__enter__.return_value = io.BytesIO(
                b'{"webSocketDebuggerUrl":"ws://127.0.0.1:12345/devtools/browser/fixture"}')
            self.assertTrue(browser.wait_cdp(timeout=30))
        proxy.assert_called_once_with({})
        self.assertEqual(browser.startup_error, "")

    def test_timeout_has_distinct_diagnostic(self):
        browser = self.browser()
        self.assertFalse(browser.wait_cdp(timeout=0))
        self.assertIsNone(browser.startup_exit_code)
        self.assertIn("timed out", browser.startup_error)


class FailureNoticeTests(unittest.TestCase):
    def test_failure_replaces_stale_success_and_splash_reports_exit_code(self):
        loader = importlib.machinery.SourceFileLoader("startup_notice_test", str(ROOT / "launch_env_window.pyw"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        notice = importlib.util.module_from_spec(spec)
        # Importing this entrypoint configures only this disposable environment.
        import os
        with patch.dict(os.environ):
            loader.exec_module(notice)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            file = home / ".startup-browser.json"
            file.write_text('{"ok":true}', encoding="utf-8")
            browser = SimpleNamespace(startup_error="early exit", startup_exit_code=3, log_file=home / "browser.log")
            with patch.object(open_env_window, "DATA_ROOT", home), \
                 patch.object(open_env_window.time, "time", return_value=100), \
                 redirect_stdout(io.StringIO()):
                open_env_window.report_browser_failure(browser)
            self.assertFalse(json.loads(file.read_text())["ok"])
            with patch.object(notice, "PRODUCT_DATA", home):
                self.assertIn("0x00000003", notice.startup_failure_message(99))
                self.assertNotIn("0x00000003", notice.startup_failure_message(200), "ignore a previous launch's failure")
                with patch.object(notice, "run_target", return_value=2), \
                     patch.object(notice, "show_startup_failure") as show:
                    self.assertEqual(notice.run_without_splash(), 2)
                    show.assert_called_once()
                with patch.object(notice, "run_target", return_value=0), \
                     patch.object(notice, "show_startup_failure") as show:
                    self.assertEqual(notice.run_without_splash(), 0)
                    show.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
