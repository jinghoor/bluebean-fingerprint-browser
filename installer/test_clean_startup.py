"""Fresh-user startup and packaging privacy regressions; disposable data only."""
from contextlib import ExitStack, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from installer.payload_policy import assert_clean_payload, audit_payload, startup_violations
import open_env_window


class StartupTests(unittest.TestCase):
    def check_startup(self, *, product=False, existing=False, saved=False, legacy_skip=False, initialized=True):
        with tempfile.TemporaryDirectory(prefix='bluebean-clean-startup-') as directory:
            data = Path(directory)
            profile = data / 'BrowserProfile'
            if initialized:
                (profile / 'Default').mkdir(parents=True)
            state = {'active': 'fb-008' if saved else '', 'envs': [
                {'env_id': 'fb-008', 'display_name': '用户自己的同名环境', 'proxy': 'direct', 'notes': 'preserve'}
            ] if saved else []}
            if initialized:
                (profile / 'Default/envs.json').write_text(json.dumps(state), encoding='utf-8')
                (profile / 'Default/env-account-vault.dpapi.json').write_bytes(b'opaque user account vault')
            (data / 'proxies.json').write_bytes(b'{"user-proxy":"preserve"}')
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in data.rglob('*') if p.is_file()}
            calls = []
            def ensure_service(path):
                self.assertTrue((Path(path) / 'Default').is_dir(), 'native profile must exist before any service resolves paths')
                import env_cli
                self.assertEqual(env_cli.resolve_profile_dir(Path(path)), Path(path) / 'Default')
                return {'port': 12347}

            class Browser:
                def __init__(self, *args, **kwargs):
                    self.chrome_pid = 12345
                    self.log_file = data / 'browser.log'

                def start(self, **kwargs):
                    calls.append(('start',))
                    return True

                def cli(self, *args):
                    calls.append(args)
                    return {'ok': True}

                def state(self):
                    return state

                def activate(self, *args):
                    calls.append(('activate', *args))
                    return {'ok': True}

            argv = ['open_env_window.py', '--profile', str(profile), '--chrome', 'fixture-chrome.exe', '--no-automation']
            if product:
                argv.append('--product-startup')
            if legacy_skip:
                argv.append('--skip-envs')
            with ExitStack() as stack:
                for target, kwargs in (
                    ('sys.argv', {'new': argv}),
                    ('open_env_window.DATA_ROOT', {'new': data}),
                    ('open_env_window.EnvBrowser', {'new': Browser}),
                    ('open_env_window.running_browser_for', {'return_value': 12345 if existing else None}),
                    ('open_env_window.recorded_cdp_port', {'return_value': 12346}),
                    ('open_env_window.windows_of_process', {'return_value': []}),
                    ('open_env_window.ensure_proxies_bound', {}),
                    ('open_env_window.subprocess.Popen', {}),
                    ('env_startup_check.ensure_service', {'side_effect': ensure_service}),
                    ('_ensure_relays.ensure_relays', {}),
                    ('_ensure_bridge.ensure_bridge', {'return_value': {'ok': True}}),
                    ('_dsh_web.ensure', {'return_value': {'ok': True}}),
                ):
                    stack.enter_context(patch(target, **kwargs))
                stack.enter_context(redirect_stdout(io.StringIO()))
                self.assertEqual(open_env_window.main(), 0)
            self.assertFalse([call for call in calls if call and call[0] in ('create', 'activate')], calls)
            self.assertTrue(all(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in before.items()))
            if initialized:
                self.assertEqual(json.loads((profile / 'Default/envs.json').read_text())['envs'], state['envs'])
            else:
                self.assertFalse((profile / 'Default/envs.json').exists(), 'directory preparation must not seed environment definitions')

    def test_first_normal_start_stays_empty(self):
        self.check_startup()

    def test_first_product_start_stays_empty(self):
        self.check_startup(product=True)

    def test_completely_missing_profile_is_ready_before_services_start(self):
        for product in (False, True):
            with self.subTest(product=product):
                self.check_startup(product=product, initialized=False)

    def test_empty_profile_reuse_stays_empty(self):
        for product in (False, True):
            with self.subTest(product=product):
                self.check_startup(product=product, existing=True)

    def test_user_data_and_legitimate_demo_named_environment_survive(self):
        for product in (False, True):
            for existing in (False, True):
                with self.subTest(product=product, existing=existing):
                    self.check_startup(product=product, existing=existing, saved=True)

    def test_legacy_skip_flag_is_still_accepted(self):
        self.check_startup(legacy_skip=True)

    def test_startup_contains_no_automatic_fixtures(self):
        self.assertEqual(startup_violations(ROOT / 'open_env_window.py'), [])


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bluebean-payload-policy-')
        self.app = Path(self.temp.name) / 'App'
        (self.app / 'lan_seats/lib').mkdir(parents=True)
        shutil.copy2(ROOT / 'open_env_window.py', self.app / 'open_env_window.py')
        shutil.copy2(ROOT / 'lan_seats/lib/client.js', self.app / 'lan_seats/lib/client.js')

    def tearDown(self):
        self.temp.cleanup()

    def test_clean_application_passes(self):
        self.assertTrue(assert_clean_payload(self.app)['passed'])

    def test_personal_files_anywhere_in_runtime_are_rejected_without_deletion(self):
        for name in ('envs.json', 'proxies.json', 'Cookies', 'Login Data', 'env-accounts-v2.dpapi.json',
                     'env-management.json', '.deepseek-key', '.lan-bridge.json', '.session-browser-scopes.json'):
            with self.subTest(name=name):
                file = self.app / 'dsh/resources/accidental-copy' / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('must not ship', encoding='utf-8')
                self.assertFalse(audit_payload(self.app)['passed'])
                with self.assertRaises(RuntimeError):
                    assert_clean_payload(self.app)
                self.assertEqual(file.read_text(), 'must not ship')
                file.unlink()

    def test_developer_profiles_and_data_directories_are_rejected(self):
        for relative in ('Data', 'BrowserProfile', '.work', 'config', 'dsh/profiles', 'browser/Default'):
            with self.subTest(relative=relative):
                folder = self.app / relative
                folder.mkdir(parents=True, exist_ok=True)
                self.assertFalse(audit_payload(self.app)['passed'])
                folder.rmdir()

    def test_real_bridge_token_is_rejected_and_never_printed(self):
        client = self.app / 'lan_seats/lib/client.js'
        client.write_text("const TOKEN = 'fixture-private-token-do-not-ship';", encoding='utf-8')
        report = audit_payload(self.app)
        self.assertFalse(report['passed'])
        self.assertNotIn('fixture-private-token-do-not-ship', json.dumps(report))

    def test_unknown_root_data_and_test_screenshots_are_rejected(self):
        for name in ('personal-settings.json', 'env-shot.png', '.unrecognized-api-key'):
            with self.subTest(name=name):
                file = self.app / name
                file.write_text('private fixture', encoding='utf-8')
                self.assertFalse(audit_payload(self.app)['passed'])
                file.unlink()

    def test_legacy_automatic_demo_startup_is_rejected(self):
        file = self.app / 'open_env_window.py'
        file.write_text("DEFAULT_ENVS = [('demo', 'demo', 'direct')]\nfor env in DEFAULT_ENVS:\n    browser.cli('create', env[0])\n", encoding='utf-8')
        self.assertFalse(audit_payload(self.app)['passed'])

    def test_packaging_entry_enforces_policy_before_compiler(self):
        spec = importlib.util.spec_from_file_location('bluebean_installer_builder', ROOT / 'build-installer.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        (self.app / 'envs.json').write_text('{}', encoding='utf-8')
        with patch.object(builder.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                builder.build_windows_installer(self.app.parent)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
