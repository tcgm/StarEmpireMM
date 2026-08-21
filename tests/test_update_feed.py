from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from installer.update_feed import (
    FeedError,
    UpdateFeed,
    UpdateFeedIndex,
    accept_update_feed_sequence,
    download_update_package,
    fetch_update_feed,
    package_filename,
    select_update_release,
    _exclusive_sequence_state,
)


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, url: str):
        super().__init__(payload)
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class UpdateFeedTests(unittest.TestCase):
    def test_network_identity_uses_general_manager_name(self):
        source = Path("installer/update_feed.py").read_text(encoding="utf-8")
        self.assertIn("StarEmpireModManager/", source)
        self.assertNotIn("StarEmpireUiModManager/", source)

    def _feed(self, private, *, expires=None, package_url="https://example.test/mod.seuimod"):
        expires = expires or datetime.now(timezone.utc) + timedelta(days=1)
        data = {
            "schema": 1, "format": "star-empire-ui-mod-feed",
            "channel": "stable", "key_id": "test", "pack_id": "pack/id",
            "mod_version": "1.2.3", "game_version": "0.4.42",
            "manager_version_min": "0.2.0",
            "expires_at": expires.isoformat(), "package_url": package_url,
            "package_size": 123, "package_sha256": "A" * 64,
        }
        payload = (json.dumps(data, sort_keys=True) + "\n").encode()
        return payload, private.sign(payload)

    def test_exact_signed_unexpired_https_feed_is_accepted(self) -> None:
        private = Ed25519PrivateKey.generate()
        payload, signature = self._feed(private)

        def opener(request, timeout):
            url = request.full_url
            return _Response(signature if url.endswith(".sig") else payload, url)

        feed = fetch_update_feed(
            "https://example.test/stable.json",
            {"test": private.public_key().public_bytes_raw()}, opener=opener)
        self.assertEqual("pack/id", feed.pack_id)
        self.assertEqual(
            "pack-id-1.2.3-aaaaaaaaaaaa.seuimod", package_filename(feed))

    def _feed_v2(self, private, *, releases=None):
        expires = datetime.now(timezone.utc) + timedelta(days=1)
        releases = releases or [{
            "package_key_id": "package", "pack_id": "alpha-045",
            "mod_version": "0.2.0", "game_version": "0.4.45",
            "official_client_sha256": "C" * 64,
            "manager_version_min": "0.2.0",
            "package_url": "https://example.test/045.seuimod",
            "package_size": 456, "package_sha256": "D" * 64,
        }, {
            "package_key_id": "package", "pack_id": "alpha-046",
            "mod_version": "0.2.1", "game_version": "0.4.46",
            "official_client_sha256": "E" * 64,
            "manager_version_min": "99.0.0",
            "package_url": "https://example.test/046.seuimod",
            "package_size": 789, "package_sha256": "F" * 64,
        }]
        data = {
            "schema": 2, "format": "star-empire-ui-mod-feed",
            "channel": "alpha", "key_id": "feed", "sequence": 7,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires.isoformat(),
            "manager_version_min": "0.2.0", "releases": releases,
        }
        payload = (json.dumps(
            data, sort_keys=True, separators=(",", ":")) + "\n").encode()
        return payload, private.sign(payload)

    def test_dynamic_feed_selects_only_exact_version_and_official_hash(self):
        private = Ed25519PrivateKey.generate()
        payload, signature = self._feed_v2(private)

        def opener(request, timeout):
            return _Response(
                signature if request.full_url.endswith(".sig") else payload,
                request.full_url)

        feed = fetch_update_feed(
            "https://example.test/alpha.json",
            {
                "feed": private.public_key().public_bytes_raw(),
                "package": b"P" * 32,
            }, opener=opener)
        self.assertIsInstance(feed, UpdateFeedIndex)
        self.assertEqual(7, feed.sequence)
        selected = select_update_release(feed, "0.4.45", "c" * 64)
        self.assertEqual("alpha-045", selected.pack_id)
        self.assertEqual("C" * 64, selected.official_client_sha256)
        self.assertIsNone(select_update_release(feed, "0.4.45", "A" * 64))
        self.assertIsNone(select_update_release(feed, "0.4.47", "C" * 64))
        with self.assertRaisesRegex(FeedError, "newer Manager"):
            select_update_release(feed, "0.4.46", "E" * 64)

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "feed-sequences.json"
            self.assertTrue(accept_update_feed_sequence(feed, state))
            self.assertFalse(accept_update_feed_sequence(feed, state))
            older = UpdateFeedIndex(
                feed.channel, feed.key_id, feed.sequence - 1,
                feed.published_at, feed.expires_at, feed.manager_version_min,
                feed.releases, "A" * 64)
            with self.assertRaisesRegex(FeedError, "older"):
                accept_update_feed_sequence(older, state)
            changed = UpdateFeedIndex(
                feed.channel, feed.key_id, feed.sequence,
                feed.published_at, feed.expires_at, feed.manager_version_min,
                feed.releases, "B" * 64)
            with self.assertRaisesRegex(FeedError, "different bytes"):
                accept_update_feed_sequence(changed, state)

    def test_sequence_state_is_locked_across_manager_processes(self):
        private = Ed25519PrivateKey.generate()
        payload, signature = self._feed_v2(private)

        def opener(request, timeout):
            return _Response(
                signature if request.full_url.endswith(".sig") else payload,
                request.full_url)

        feed = fetch_update_feed(
            "https://example.test/alpha.json",
            {"feed": private.public_key().public_bytes_raw()}, opener=opener)
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "feed-sequences.json"
            with _exclusive_sequence_state(state):
                with self.assertRaisesRegex(FeedError, "another Manager"):
                    accept_update_feed_sequence(feed, state)
            self.assertTrue(accept_update_feed_sequence(feed, state))
            self.assertFalse(accept_update_feed_sequence(feed, state))

    def test_dynamic_feed_rejects_ambiguous_or_duplicate_key_metadata(self):
        private = Ed25519PrivateKey.generate()
        release = {
            "package_key_id": "package", "pack_id": "same",
            "mod_version": "0.2.0", "game_version": "0.4.45",
            "official_client_sha256": "C" * 64,
            "manager_version_min": "0.2.0",
            "package_url": "https://example.test/045.seuimod",
            "package_size": 456, "package_sha256": "D" * 64,
        }
        payload, signature = self._feed_v2(private, releases=[release, release])

        def opener(request, timeout):
            return _Response(
                signature if request.full_url.endswith(".sig") else payload,
                request.full_url)

        with self.assertRaisesRegex(FeedError, "ambiguous"):
            fetch_update_feed(
                "https://example.test/alpha.json",
                {"feed": private.public_key().public_bytes_raw(),
                 "package": b"P" * 32}, opener=opener)

        duplicate = payload.replace(b'"channel":"alpha"',
                                    b'"channel":"alpha","channel":"beta"')
        duplicate_signature = private.sign(duplicate)

        def duplicate_opener(request, timeout):
            return _Response(
                duplicate_signature if request.full_url.endswith(".sig")
                else duplicate, request.full_url)

        with self.assertRaisesRegex(FeedError, "duplicate"):
            fetch_update_feed(
                "https://example.test/alpha.json",
                {"feed": private.public_key().public_bytes_raw(),
                 "package": b"P" * 32}, opener=duplicate_opener)

    def test_tamper_expiry_and_non_https_are_blocked(self) -> None:
        private = Ed25519PrivateKey.generate()
        expired, signature = self._feed(
            private, expires=datetime.now(timezone.utc) - timedelta(seconds=1))

        def opener(request, timeout):
            return _Response(signature if request.full_url.endswith(".sig")
                             else expired, request.full_url)

        with self.assertRaisesRegex(FeedError, "expired"):
            fetch_update_feed(
                "https://example.test/feed", {"test": private.public_key().public_bytes_raw()},
                opener=opener)
        with self.assertRaisesRegex(FeedError, "HTTPS"):
            fetch_update_feed("http://example.test/feed", {})

        valid, signature = self._feed(private)
        with self.assertRaisesRegex(FeedError, "signature"):
            fetch_update_feed(
                "https://example.test/feed",
                {"test": private.public_key().public_bytes_raw()},
                opener=lambda request, timeout: _Response(
                    signature if request.full_url.endswith(".sig")
                    else valid.replace(b'"stable"', b'"beta"'),
                    request.full_url))

    def test_package_download_is_atomic_hash_checked_and_metadata_bound(self) -> None:
        payload = b"signed package bytes"
        feed = UpdateFeed(
            "stable", "test", "pack", "1.2.3", "0.4.42", "0.2.0",
            datetime.now(timezone.utc) + timedelta(days=1),
            "https://example.test/mod.seuimod", len(payload),
            hashlib.sha256(payload).hexdigest().upper())
        package = SimpleNamespace(compatibility=SimpleNamespace(
            pack_id="pack", mod_version="1.2.3", game_version="0.4.42",
            key_id="test"))
        with tempfile.TemporaryDirectory() as temporary, patch(
                "installer.update_feed.verify_mod_package", return_value=package):
            destination = Path(temporary) / "mod.seuimod"
            result = download_update_package(
                feed, destination, {"test": b"K" * 32},
                opener=lambda request, timeout: _Response(
                    payload, request.full_url))
            self.assertIs(package, result)
            self.assertEqual(payload, destination.read_bytes())

        bad_feed = UpdateFeed(
            feed.channel, feed.key_id, feed.pack_id, feed.mod_version,
            feed.game_version, feed.manager_version_min, feed.expires_at,
            feed.package_url, feed.package_size, "F" * 64)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "bad.seuimod"
            with self.assertRaisesRegex(FeedError, "hash"):
                download_update_package(
                    bad_feed, destination, {},
                    opener=lambda request, timeout: _Response(
                        payload, request.full_url))
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name("bad.seuimod.partial").exists())


if __name__ == "__main__":
    unittest.main()
