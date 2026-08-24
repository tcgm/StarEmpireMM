from __future__ import annotations

import unittest

from installer.mod_manifest import ModManifestError, version_core
from installer.version import (MANAGER_VERSION, package_requirement_version,
                               version_tuple)


class ReleaseVersionAlignmentTests(unittest.TestCase):
    def test_manager_uses_the_public_github_version(self):
        self.assertEqual("0.5", MANAGER_VERSION)
        self.assertEqual((0, 5, 0), version_tuple(MANAGER_VERSION))
        self.assertEqual((0, 5, 1), version_tuple("0.5.1"))

    def test_legacy_package_requirement_maps_to_public_release(self):
        self.assertEqual("0.4", package_requirement_version("0.4.6"))
        self.assertEqual("0.5", package_requirement_version("0.5"))

    def test_mod_versions_accept_the_public_github_format(self):
        self.assertEqual((0, 1, 0), version_core("0.1"))
        self.assertEqual((0, 1, 1), version_core("0.1.1"))
        self.assertEqual((0, 1, 1), version_core("0.1.1-alpha.1"))
        with self.assertRaises(ModManifestError):
            version_core("0.1.1.1")


if __name__ == "__main__":
    unittest.main()
