"""Verify DSH Web restarts rebind the authenticated URL to the browser overlay."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _dsh_web  # noqa: E402


class OverlayBindingTests(unittest.TestCase):
    def test_binding_is_skipped_before_browser_start(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(_dsh_web, 'PROFILE_DIR', Path(folder)):
                result = _dsh_web.bind_overlay('http://127.0.0.1:1234/?token=test')
        self.assertFalse(result['ok'])
        self.assertEqual(result['skipped'], 'browser not running')

    def test_binding_refreshes_the_running_panel(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)
            (profile / 'DevToolsActivePort').write_text('9333\n', encoding='utf-8')
            with patch.object(_dsh_web, 'PROFILE_DIR', profile), \
                 patch('env_cli.resolve_profile_dir', return_value=profile) as resolve, \
                 patch('env_cli.send', return_value={'ok': True, 'visible': True}) as send:
                result = _dsh_web.bind_overlay('http://127.0.0.1:1234/?token=test')
        self.assertTrue(result['ok'])
        resolve.assert_called_once_with(profile)
        send.assert_called_once_with(
            profile, {'command': 'overlay', 'on': True,
                      'url': 'http://127.0.0.1:1234/?token=test'}, 2.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
