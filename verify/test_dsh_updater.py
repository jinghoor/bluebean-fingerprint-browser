"""Unit checks for the official DSH update manifest and version policy."""

import unittest

from dsh_updater import _version_key, parse_feed


class DshUpdaterTest(unittest.TestCase):
    def test_stable_feed_parser_accepts_official_shape(self):
        release = parse_feed(
            """version: 0.2.2-rc.5
files:
  - url: >-
      https://download.deepseek.com/dsh-desk/bin/win-x64/stable/dsh.exe
    sha512: >-
      abc
    size: 123
path: >-
  https://download.deepseek.com/dsh-desk/bin/win-x64/stable/dsh.exe
sha512: >-
  abc
"""
        )
        self.assertEqual(release["version"], "0.2.2-rc.5")
        self.assertEqual(release["size"], 123)
        self.assertEqual(release["sha512"], "abc")

    def test_versions_order_prereleases(self):
        self.assertLess(_version_key("0.2.2-rc.1"), _version_key("0.2.2-rc.5"))
        self.assertLess(_version_key("0.2.2-rc.5"), _version_key("0.2.3-alpha.1"))
        self.assertLess(_version_key("0.2.3-alpha.1"), _version_key("0.2.3"))


if __name__ == "__main__":
    unittest.main()
