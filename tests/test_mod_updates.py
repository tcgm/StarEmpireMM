from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from installer.mod_manifest import GithubUpdateSource
from installer.mod_updates import (
    ModUpdateError, acquire_github_update, discover_github_release,
)


class _Response:
    def __init__(self, payload: bytes, url: str, length: str | None = None):
        self.payload = payload
        self.url = url
        self.offset = 0
        self.headers = {} if length is None else {"Content-Length": length}

    def read(self, size=-1):
        if size < 0:
            size = len(self.payload) - self.offset
        result = self.payload[self.offset:self.offset + size]
        self.offset += len(result)
        return result

    def geturl(self):
        return self.url

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _release(repository="owner/repo", assets=None):
    assets = assets or [{
        "name": "example-2.0.0.semod",
        "browser_download_url": (
            f"https://github.com/{repository}/releases/download/v2.0.0/"
            "example-2.0.0.semod"),
    }]
    return json.dumps({"tag_name": "v2.0.0", "assets": assets}).encode()


class ModUpdateTests(unittest.TestCase):
    def test_discovers_exact_configured_github_asset(self):
        response = _Response(
            _release(), "https://api.github.com/repos/owner/repo/releases/latest")
        result = discover_github_release(
            GithubUpdateSource("owner/repo", "example-*.semod"),
            opener=lambda _request, timeout: response)
        self.assertEqual("example-2.0.0.semod", result.name)
        self.assertEqual("owner/repo", result.repository)

    def test_ambiguous_asset_or_untrusted_url_is_rejected(self):
        duplicate = _release(assets=[
            {"name": "example-a.semod", "browser_download_url":
             "https://github.com/owner/repo/releases/download/v2/a.semod"},
            {"name": "example-b.semod", "browser_download_url":
             "https://github.com/owner/repo/releases/download/v2/b.semod"},
        ])
        with self.assertRaisesRegex(ModUpdateError, "exactly one"):
            discover_github_release(
                GithubUpdateSource("owner/repo", "example-*.semod"),
                opener=lambda _request, timeout: _Response(
                    duplicate, "https://api.github.com/x"))
        with self.assertRaisesRegex(ModUpdateError, "not trusted"):
            discover_github_release(
                GithubUpdateSource("owner/repo", "example-*.semod"),
                opener=lambda _request, timeout: _Response(
                    _release(assets=[{
                        "name": "example-a.semod",
                        "browser_download_url": "https://evil.test/a.semod",
                    }]), "https://api.github.com/x"))

    def test_newer_verified_package_is_retained_and_old_or_wrong_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = GithubUpdateSource("owner/repo", "example-*.semod")
            installed = SimpleNamespace(manifest=SimpleNamespace(
                mod_id="example.mod", version="1.0.0", update=source),
                package_sha256="A" * 64)
            package_bytes = b"package bytes"
            responses = iter((
                _Response(_release(), "https://api.github.com/latest"),
                _Response(package_bytes,
                          "https://github.com/owner/repo/releases/download/v2/x",
                          str(len(package_bytes))),
            ))
            verified = SimpleNamespace(
                manifest=SimpleNamespace(mod_id="example.mod", version="2.0.0"),
                package_sha256=__import__("hashlib").sha256(
                    package_bytes).hexdigest().upper())
            with patch("installer.mod_updates.verify_semod_package",
                       return_value=verified):
                result = acquire_github_update(
                    installed, root,
                    opener=lambda _request, timeout: next(responses))
            self.assertIs(verified, result.package)
            self.assertTrue(result.path.is_file())
            self.assertEqual(package_bytes, result.path.read_bytes())
            self.assertFalse(tuple(root.glob("*.partial.semod")))

            responses = iter((
                _Response(_release(), "https://api.github.com/latest"),
                _Response(package_bytes,
                          "https://github.com/owner/repo/releases/download/v2/x"),
            ))
            wrong = SimpleNamespace(
                manifest=SimpleNamespace(mod_id="other.mod", version="2.0.0"),
                package_sha256=verified.package_sha256)
            with patch("installer.mod_updates.verify_semod_package",
                       return_value=wrong):
                with self.assertRaisesRegex(ModUpdateError, "different mod"):
                    acquire_github_update(
                        installed, root / "wrong",
                        opener=lambda _request, timeout: next(responses))

    def test_same_or_older_version_is_not_offered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_bytes = b"package bytes"
            source = GithubUpdateSource("owner/repo", "example-*.semod")
            installed = SimpleNamespace(manifest=SimpleNamespace(
                mod_id="example.mod", version="2.0.0", update=source),
                package_sha256="A" * 64)
            responses = iter((
                _Response(_release(), "https://api.github.com/latest"),
                _Response(package_bytes,
                          "https://github.com/owner/repo/releases/download/v2/x"),
            ))
            verified = SimpleNamespace(
                manifest=SimpleNamespace(mod_id="example.mod", version="2.0.0"),
                package_sha256=__import__("hashlib").sha256(
                    package_bytes).hexdigest().upper())
            with patch("installer.mod_updates.verify_semod_package",
                       return_value=verified):
                self.assertIsNone(acquire_github_update(
                    installed, root,
                    opener=lambda _request, timeout: next(responses)))
            self.assertEqual((), tuple(root.glob("*.semod")))


if __name__ == "__main__":
    unittest.main()
