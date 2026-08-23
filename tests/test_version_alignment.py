from __future__ import annotations

import unittest

from installer.mod_manifest import version_core
from installer.version import MANAGER_VERSION, version_tuple


class ReleaseVersionAlignmentTests(unittest.TestCase):
    def test_v01_uses_one_public_version(self):
        self.assertEqual("0.1", MANAGER_VERSION)
        self.assertEqual((0, 1, 0), version_tuple(MANAGER_VERSION))
        self.assertEqual((0, 1, 0), version_core("0.1"))


if __name__ == "__main__":
    unittest.main()
