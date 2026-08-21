from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from installer.trusted_keys import BUILTIN_TRUSTED_KEYS
from installer.update_feed import fetch_update_feed, select_update_release
from tools.build_update_feed import FeedBuildError, build_update_feed, main


class _Response:
    def __init__(self, payload: bytes, url: str):
        self.payload = payload
        self.url = url

    def __enter__(self):
        from io import BytesIO
        self.stream = BytesIO(self.payload)
        self.stream.geturl = lambda: self.url
        return self.stream

    def __exit__(self, *_args):
        self.stream.close()


class BuildUpdateFeedTests(unittest.TestCase):
    @staticmethod
    def _package(path: Path, *, version: str, official: str, pack: str):
        path.write_bytes(f"package-{version}-{official}".encode())
        compatibility = SimpleNamespace(
            pack_id=pack, mod_version="0.2.0", game_version=version,
            official_client_sha256=official, key_id="alpha-key")
        return SimpleNamespace(
            path=path, compatibility=compatibility,
            manifest={"manager_version_min": "0.2.0"})

    def test_builds_deterministic_exact_build_index_without_game_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = Ed25519PrivateKey.generate()
            first_path = root / "045.seuimod"
            second_path = root / "046.seuimod"
            first = self._package(
                first_path, version="0.4.45", official="A" * 64,
                pack="pack-045")
            second = self._package(
                second_path, version="0.4.46", official="B" * 64,
                pack="pack-046")
            packages = {first_path: first, second_path: second}
            published = datetime(2026, 8, 15, tzinfo=timezone.utc)
            kwargs = dict(
                releases=[
                    (second_path, "https://example.test/046.seuimod"),
                    (first_path, "https://example.test/045.seuimod"),
                ],
                private_key=private, feed_key_id="alpha-key",
                channel="alpha", sequence=1, published_at=published,
                expires_at=published + timedelta(days=7),
                trusted_feed_keys={"alpha-key": private.public_key().public_bytes_raw()},
                trusted_package_keys={"alpha-key": private.public_key().public_bytes_raw()},
                genesis=True,
            )
            with patch("tools.build_update_feed._package_key_id",
                       return_value="alpha-key"), patch(
                    "tools.build_update_feed.verify_mod_package",
                    side_effect=lambda path, keys: packages[
                        first_path if b"0.4.45" in Path(path).read_bytes()
                        else second_path]):
                one = build_update_feed(output_dir=root / "one", **kwargs)
                two = build_update_feed(output_dir=root / "two", **kwargs)
            self.assertEqual(
                (one / "feed-v2.json").read_bytes(),
                (two / "feed-v2.json").read_bytes())
            self.assertEqual(
                (one / "feed-v2.json.sig").read_bytes(),
                (two / "feed-v2.json.sig").read_bytes())
            self.assertNotIn(b"package-0.4.45", (one / "feed-v2.json").read_bytes())

            payload = (one / "feed-v2.json").read_bytes()
            signature = (one / "feed-v2.json.sig").read_bytes()

            def opener(request, timeout):
                return _Response(
                    signature if request.full_url.endswith(".sig") else payload,
                    request.full_url)

            feed = fetch_update_feed(
                "https://example.test/feed-v2.json",
                {"alpha-key": private.public_key().public_bytes_raw()},
                opener=opener, now=published)
            selected = select_update_release(feed, "0.4.45", "A" * 64)
            self.assertEqual("pack-045", selected.pack_id)

    def test_duplicate_build_or_non_https_release_never_publishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = Ed25519PrivateKey.generate()
            package_path = root / "one.seuimod"
            package = self._package(
                package_path, version="0.4.45", official="A" * 64,
                pack="pack-045")
            published = datetime(2026, 8, 15, tzinfo=timezone.utc)
            base = dict(
                private_key=private, feed_key_id="alpha-key", channel="alpha",
                sequence=1, published_at=published,
                expires_at=published + timedelta(days=7),
                trusted_feed_keys={"alpha-key": private.public_key().public_bytes_raw()},
                trusted_package_keys={"alpha-key": private.public_key().public_bytes_raw()},
                genesis=True,
            )
            with patch("tools.build_update_feed._package_key_id",
                       return_value="alpha-key"), patch(
                    "tools.build_update_feed.verify_mod_package",
                    return_value=package):
                with self.assertRaisesRegex(FeedBuildError, "duplicate"):
                    build_update_feed(
                        [(package_path, "https://example.test/one"),
                         (package_path, "https://example.test/two")],
                        output_dir=root / "duplicate", **base)
                with self.assertRaisesRegex(FeedBuildError, "HTTPS"):
                    build_update_feed(
                        [(package_path, "http://example.test/one")],
                        output_dir=root / "http", **base)
            self.assertFalse((root / "duplicate").exists())
            self.assertFalse((root / "http").exists())

    def test_non_genesis_requires_authenticated_immediate_predecessor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = Ed25519PrivateKey.generate()
            published = datetime(2026, 8, 15, tzinfo=timezone.utc)
            package_path = root / "one.seuimod"
            package = self._package(
                package_path, version="0.4.45", official="A" * 64,
                pack="pack-045")
            common = dict(
                releases=[(package_path, "https://example.test/one")],
                private_key=private, feed_key_id="alpha-key", channel="alpha",
                expires_at=published + timedelta(days=7),
                trusted_feed_keys={
                    "alpha-key": private.public_key().public_bytes_raw()},
                trusted_package_keys={
                    "alpha-key": private.public_key().public_bytes_raw()},
            )
            with patch("tools.build_update_feed._package_key_id",
                       return_value="alpha-key"), patch(
                    "tools.build_update_feed.verify_mod_package",
                    return_value=package):
                prior = build_update_feed(
                    sequence=1, published_at=published,
                    output_dir=root / "prior", genesis=True, **common)
                with self.assertRaisesRegex(FeedBuildError, "prior signed feed"):
                    build_update_feed(
                        sequence=2,
                        published_at=published + timedelta(minutes=1),
                        output_dir=root / "missing-prior", **common)
                with self.assertRaisesRegex(FeedBuildError, "exactly one"):
                    build_update_feed(
                        sequence=3,
                        published_at=published + timedelta(minutes=1),
                        output_dir=root / "skipped", prior_feed_dir=prior,
                        **common)
                with self.assertRaisesRegex(FeedBuildError, "genesis"):
                    build_update_feed(
                        sequence=2,
                        published_at=published + timedelta(minutes=1),
                        output_dir=root / "bad-genesis", genesis=True,
                        **common)

            (prior / "feed-v2.json.sig").write_bytes(b"tampered")
            with self.assertRaisesRegex(FeedBuildError, "authentication"):
                build_update_feed(
                    sequence=2,
                    published_at=published + timedelta(minutes=1),
                    output_dir=root / "tampered-prior", prior_feed_dir=prior,
                    **common)
            self.assertFalse((root / "missing-prior").exists())
            self.assertFalse((root / "skipped").exists())
            self.assertFalse((root / "bad-genesis").exists())
            self.assertFalse((root / "tampered-prior").exists())

    def test_prior_history_is_carried_and_pack_identity_cannot_be_rebound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = Ed25519PrivateKey.generate()
            published = datetime(2026, 8, 15, tzinfo=timezone.utc)
            trusted = {"alpha-key": private.public_key().public_bytes_raw()}
            first_path = root / "045.seuimod"
            second_path = root / "046.seuimod"
            first = self._package(
                first_path, version="0.4.45", official="A" * 64,
                pack="pack-045")
            second = self._package(
                second_path, version="0.4.46", official="B" * 64,
                pack="pack-046")
            packages = {
                first_path.read_bytes(): first,
                second_path.read_bytes(): second,
            }

            def verify(path, keys):
                return packages[Path(path).read_bytes()]

            base = dict(
                private_key=private, feed_key_id="alpha-key", channel="alpha",
                expires_at=published + timedelta(days=7),
                trusted_feed_keys=trusted,
                trusted_package_keys=trusted,
            )
            with patch("tools.build_update_feed._package_key_id",
                       return_value="alpha-key"), patch(
                    "tools.build_update_feed.verify_mod_package",
                    side_effect=verify):
                prior = build_update_feed(
                    [(first_path, "https://example.test/045")],
                    sequence=1, published_at=published,
                    output_dir=root / "prior", genesis=True, **base)
                current = build_update_feed(
                    [(second_path, "https://example.test/046")],
                    sequence=2,
                    published_at=published + timedelta(minutes=1),
                    output_dir=root / "current", prior_feed_dir=prior,
                    **base)
            rows = json.loads(
                (current / "feed-v2.json").read_text(encoding="utf-8"))[
                    "releases"]
            self.assertEqual(["pack-045", "pack-046"], [
                row["pack_id"] for row in rows])

            rebound_path = root / "rebound.seuimod"
            rebound = self._package(
                rebound_path, version="0.4.45", official="A" * 64,
                pack="different-pack")
            reused_path = root / "reused.seuimod"
            reused = self._package(
                reused_path, version="0.4.47", official="C" * 64,
                pack="pack-045")
            with patch("tools.build_update_feed._package_key_id",
                       return_value="alpha-key"), patch(
                    "tools.build_update_feed.verify_mod_package",
                    return_value=rebound):
                updated = build_update_feed(
                    [(rebound_path, "https://example.test/rebound")],
                    sequence=3,
                    published_at=published + timedelta(minutes=2),
                    output_dir=root / "updated", prior_feed_dir=current,
                    **base)
            updated_rows = json.loads(
                (updated / "feed-v2.json").read_text(encoding="utf-8"))[
                    "releases"]
            self.assertEqual("different-pack", next(
                row["pack_id"] for row in updated_rows
                if row["game_version"] == "0.4.45"))
            history = json.loads(
                (updated / "publisher-history.json").read_text(
                    encoding="utf-8"))["pack_identities"]
            self.assertEqual(
                {"pack-045", "different-pack", "pack-046"}, set(history))

            with patch("tools.build_update_feed._package_key_id",
                       return_value="alpha-key"), patch(
                    "tools.build_update_feed.verify_mod_package",
                    return_value=reused):
                with self.assertRaisesRegex(FeedBuildError, "pack ID"):
                    build_update_feed(
                        [(reused_path, "https://example.test/reused")],
                        sequence=4,
                        published_at=published + timedelta(minutes=3),
                        output_dir=root / "reused", prior_feed_dir=updated,
                        **base)
            self.assertFalse((root / "reused").exists())

    def test_package_metadata_uses_one_authenticated_source_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = Ed25519PrivateKey.generate()
            published = datetime(2026, 8, 15, tzinfo=timezone.utc)
            package_path = root / "one.seuimod"
            package = self._package(
                package_path, version="0.4.45", official="A" * 64,
                pack="pack-045")
            original = package_path.read_bytes()
            replacement = b"changed-after-authentication"

            def verify(snapshot_path, keys):
                snapshot = Path(snapshot_path)
                self.assertNotEqual(package_path.resolve(), snapshot.resolve())
                self.assertEqual(original, snapshot.read_bytes())
                package_path.write_bytes(replacement)
                return package

            with patch("tools.build_update_feed._package_key_id",
                       return_value="alpha-key"), patch(
                    "tools.build_update_feed.verify_mod_package",
                    side_effect=verify):
                output = build_update_feed(
                    [(package_path, "https://example.test/one")],
                    private_key=private, feed_key_id="alpha-key",
                    channel="alpha", sequence=1, published_at=published,
                    expires_at=published + timedelta(days=7),
                    output_dir=root / "feed", genesis=True,
                    trusted_feed_keys={
                        "alpha-key": private.public_key().public_bytes_raw()},
                    trusted_package_keys={
                        "alpha-key": private.public_key().public_bytes_raw()})
            row = json.loads(
                (output / "feed-v2.json").read_text(encoding="utf-8"))[
                    "releases"][0]
            self.assertEqual(len(original), row["package_size"])
            self.assertEqual(
                hashlib.sha256(original).hexdigest().upper(),
                row["package_sha256"])
            self.assertNotEqual(
                hashlib.sha256(replacement).hexdigest().upper(),
                row["package_sha256"])

    def test_feed_signer_must_match_deployed_trust_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            signer = Ed25519PrivateKey.generate()
            different = Ed25519PrivateKey.generate()
            package_path = root / "one.seuimod"
            package = self._package(
                package_path, version="0.4.45", official="A" * 64,
                pack="pack-045")
            published = datetime(2026, 8, 15, tzinfo=timezone.utc)
            kwargs = dict(
                releases=[(package_path, "https://example.test/one")],
                private_key=signer, feed_key_id="alpha-key", channel="alpha",
                sequence=1, published_at=published,
                expires_at=published + timedelta(days=7), genesis=True,
                trusted_package_keys={
                    "alpha-key": signer.public_key().public_bytes_raw()})

            with patch("tools.build_update_feed._package_key_id",
                       return_value="alpha-key"), patch(
                    "tools.build_update_feed.verify_mod_package",
                    return_value=package):
                with self.assertRaisesRegex(FeedBuildError, "not deployed"):
                    build_update_feed(
                        output_dir=root / "unknown", trusted_feed_keys={},
                        **kwargs)
                with self.assertRaisesRegex(FeedBuildError, "does not match"):
                    build_update_feed(
                        output_dir=root / "mismatch",
                        trusted_feed_keys={
                            "alpha-key": different.public_key().public_bytes_raw()},
                        **kwargs)
            self.assertFalse((root / "unknown").exists())
            self.assertFalse((root / "mismatch").exists())

    def test_cli_uses_manager_deployed_keys_for_feed_and_packages(self):
        private = Ed25519PrivateKey.generate()
        output = Path("feed")
        arguments = SimpleNamespace(
            release=[(Path("package.seuimod"), "https://example.test/package")],
            private_key=Path("private.pem"), feed_key_id="deployed",
            channel="alpha", sequence=1, genesis=True,
            prior_feed_dir=None, valid_days=7, output_dir=output)
        with patch("tools.build_update_feed._arguments",
                   return_value=arguments), patch(
                       "tools.build_update_feed._load_private_key",
                       return_value=private), patch(
                       "tools.build_update_feed.build_update_feed",
                       return_value=output) as builder, patch("builtins.print"):
            main()

        self.assertIs(
            BUILTIN_TRUSTED_KEYS,
            builder.call_args.kwargs["trusted_feed_keys"])
        self.assertIs(
            BUILTIN_TRUSTED_KEYS,
            builder.call_args.kwargs["trusted_package_keys"])


if __name__ == "__main__":
    unittest.main()
