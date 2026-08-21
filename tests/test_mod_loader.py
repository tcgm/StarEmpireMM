from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mod_loader import (LOADER_API_VERSION, ModEventBus, ModApiError,
                        ScopedModApi)
from mod_loader.runtime import (ExternalModLoader, GameIdentity,
                                ModLoaderError, detect_game_identity)


def _install_mod(
        state_root: Path, mod_id: str, source: str, *,
        version: str = "1.0.0", loader_api: int = 1, enabled: bool = True,
        entrypoint: str | None = None, compatibility=None,
        receipt_schema: int = 2):
    namespace = mod_id.replace("-", "_")
    module_path = namespace.replace(".", "/")
    install_path = state_root / "mods" / mod_id / version
    module = install_path / "mod" / (module_path + ".py")
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(source, encoding="utf-8")
    manifest = {
        "schema": 1,
        "format": "star-empire-mod",
        "mod_id": mod_id,
        "name": mod_id.title(),
        "version": version,
        "author": "Test",
        "description": "",
        "loader_api": loader_api,
        "entrypoint": entrypoint or f"{namespace}:register",
        "files": [f"mod/{module_path}.py"],
        "dependencies": [],
        "conflicts": [],
        "load_after": [],
        "permissions": [],
    }
    if compatibility is not None:
        manifest["compatibility"] = compatibility
    (install_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    payloads = []
    for relative in ("manifest.json", f"mod/{module_path}.py"):
        payload = (install_path / Path(*relative.split("/"))).read_bytes()
        payloads.append({
            "path": relative,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest().upper(),
        })
    receipt = {
        "schema": receipt_schema,
        "package_sha256": "A" * 64,
        "package_manifest_sha256": "B" * 64,
        "payloads": payloads,
    }
    if receipt_schema == 1:
        receipt["key_id"] = "keyless-external-mod"
    (install_path / ".semod-install.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return {
        "manifest": manifest,
        "install_path": str(install_path.resolve()),
        "package_sha256": "A" * 64,
        "enabled": enabled,
        "source": "test",
        "installed_at": "2026-08-20T00:00:00+00:00",
    }


def _write_registry(state_root: Path, mods, *, schema: int = 1):
    state_root.mkdir(parents=True, exist_ok=True)
    stored = []
    for item in mods:
        entry = dict(item)
        if schema == 2:
            entry.setdefault("force_load", False)
        stored.append(entry)
    path = state_root / "mods.json"
    path.write_text(json.dumps({
        "schema": schema,
        "mods": stored,
        "load_order": [item["manifest"]["mod_id"] for item in mods],
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class ModApiTests(unittest.TestCase):
    def test_inline_region_contract_is_loader_api_two(self):
        api = ScopedModApi(
            ModEventBus(), "test.mod", "1.0.0", Path("."), Path("."))
        self.assertEqual(2, LOADER_API_VERSION)
        self.assertEqual(2, api.loader_api_version)

    def test_priority_order_unsubscribe_and_callback_failure_isolation(self):
        bus = ModEventBus()
        calls = []
        low = ScopedModApi(bus, "low.mod", "1.0.0", Path("."), Path("."))
        high = ScopedModApi(bus, "high.mod", "1.0.0", Path("."), Path("."))
        low.on("client.draw", lambda: calls.append("low"), priority=0)
        unsubscribe = high.on(
            "client.draw", lambda: calls.append("high"), priority=10)
        low.on("client.draw", lambda: 1 / 0, priority=5)

        bus.emit("client.draw")
        self.assertEqual(["high", "low"], calls)
        self.assertEqual("low.mod", bus.diagnostics[0].mod_id)
        unsubscribe()
        calls.clear()
        bus.emit("client.draw")
        self.assertEqual(["low"], calls)
        with self.assertRaises(ModApiError):
            low.on("x", lambda: None)


class ExternalModLoaderTests(unittest.TestCase):
    def tearDown(self):
        for prefix in ("alpha", "beta", "bad", "future", "escaped"):
            for name in tuple(sys.modules):
                if name == prefix or name.startswith(prefix + "."):
                    sys.modules.pop(name, None)

    def test_two_external_mods_load_in_registry_order_and_receive_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            alpha = _install_mod(state, "alpha.mod", '''
def register(api):
    api.on("client.startup", lambda host: host.append("alpha"))
''')
            beta = _install_mod(state, "beta.mod", '''
def register(api):
    api.on("client.startup", lambda host: host.append("beta"))
''')
            loader = ExternalModLoader(_write_registry(state, (alpha, beta)))
            loaded = loader.load_enabled()
            calls = []
            loader.emit("client.startup", calls)

            self.assertEqual(("alpha.mod", "beta.mod"),
                             tuple(item.mod_id for item in loaded))
            self.assertEqual(["alpha", "beta"], calls)
            self.assertEqual((), loader.diagnostics)
            loader.shutdown()
            self.assertEqual((), loader.loaded)
            self.assertNotIn(str((Path(alpha["install_path"]) / "mod")), sys.path)

    def test_event_payload_can_use_event_keyword(self):
        with tempfile.TemporaryDirectory() as temporary:
            loader = ExternalModLoader(
                Path(temporary) / "state" / "mods.json")
            payload = object()
            received = []
            loader.bus.register(
                "alpha.mod", "client.event",
                lambda *, event: received.append(event) or True)

            self.assertEqual(
                (True,), loader.emit("client.event", event=payload))
            self.assertEqual([payload], received)

    def test_exact_compatible_game_build_loads(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            digest = "A" * 64
            compatible = _install_mod(
                state, "alpha.mod", "def register(api): pass\n",
                compatibility={
                    "mode": "exact-game-build",
                    "game_builds": [{
                        "game_version": "0.4.61",
                        "client_sha256": digest.lower(),
                    }],
                })
            loader = ExternalModLoader(
                _write_registry(state, (compatible,)),
                game_identity=GameIdentity("0.4.61", digest))

            self.assertEqual(1, len(loader.load_enabled()))
            self.assertEqual((), loader.diagnostics)
            loader.shutdown()

    def test_game_version_compatibility_loads_regardless_of_executable_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for label, identity, expected in (
                    ("vanilla", GameIdentity("0.4.61", "A" * 64), 1),
                    ("loader-patched", GameIdentity("0.4.61", "B" * 64), 1),
                    ("new-version", GameIdentity("0.4.62", "B" * 64), 0)):
                with self.subTest(label=label):
                    state = root / label
                    compatible = _install_mod(
                        state, "alpha.mod", "def register(api): pass\n",
                        compatibility={
                            "mode": "game-version",
                            "game_versions": ["0.4.61"],
                        })
                    loader = ExternalModLoader(
                        _write_registry(state, (compatible,)),
                        game_identity=identity)
                    self.assertEqual(expected, len(loader.load_enabled()))
                    if expected:
                        self.assertEqual((), loader.diagnostics)
                    else:
                        self.assertIn(
                            "does not support", loader.diagnostics[0].message)
                    loader.shutdown()

    def test_force_load_bypasses_only_valid_version_mismatch_and_warns(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            forced = _install_mod(
                state, "alpha.mod", "def register(api): pass\n",
                compatibility={
                    "mode": "game-version",
                    "game_versions": ["0.4.62"],
                })
            forced["force_load"] = True
            loader = ExternalModLoader(
                _write_registry(state, (forced,), schema=2),
                game_identity=GameIdentity("0.4.63", "A" * 64))

            with self.assertLogs("mod_loader.runtime", "WARNING") as captured:
                loaded = loader.load_enabled()

            self.assertEqual(1, len(loaded))
            self.assertIn("MOD_COMPATIBILITY_FORCE_LOAD", captured.output[0])
            self.assertIn("running_game_version=0.4.63", captured.output[0])
            loader.shutdown()

    def test_force_load_does_not_bypass_malformed_compatibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            forced = _install_mod(
                state, "alpha.mod", "def register(api): pass\n",
                compatibility={
                    "mode": "game-version",
                    "game_versions": [],
                })
            forced["force_load"] = True
            loader = ExternalModLoader(
                _write_registry(state, (forced,), schema=2),
                game_identity=GameIdentity("0.4.63", "A" * 64))

            self.assertEqual((), loader.load_enabled())
            self.assertIn("no supported game versions", loader.diagnostics[0].message)

    def test_manager_approved_version_bridge_loads_version_based_mod(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            game = root / "game"
            game.mkdir()
            digest = "B" * 64
            compatible = _install_mod(
                state, "alpha.mod", "def register(api): pass\n",
                compatibility={
                    "mode": "game-version",
                    "game_versions": ["0.4.61"],
                })
            _write_registry(state, (compatible,))
            (state / "install-state.json").write_text(json.dumps({
                "schema": 3,
                "game_root": str(game.resolve()),
                "original_client": str(root / "backup" / "Client.exe"),
                "original_sha256": "A" * 64,
                "installed_sha256": digest,
                "pack_id": "loader-0.4.61",
                "created_at": "now",
                "pack_digest": "C" * 64,
                "key_id": "test",
                "mod_version": "0.4.61",
                "game_version": "0.4.62",
                "compatibility_source_game_version": "0.4.61",
            }), encoding="utf-8")
            loader = ExternalModLoader(
                state / "mods.json",
                game_identity=GameIdentity("0.4.62", digest, game))

            self.assertEqual(1, len(loader.load_enabled()))
            self.assertEqual((), loader.diagnostics)
            loader.shutdown()

    def test_version_bridge_fails_closed_when_manager_evidence_differs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for label, change in (
                    ("hash", {"installed_sha256": "D" * 64}),
                    ("running-version", {"game_version": "0.4.63"}),
                    ("source-version", {
                        "compatibility_source_game_version": "0.4.60"}),
                    ("game-root", {"game_root": str(root / "other")}),
                    ("legacy-state", {"schema": 2})):
                with self.subTest(label=label):
                    state = root / label / "state"
                    game = root / label / "game"
                    game.mkdir(parents=True)
                    compatible = _install_mod(
                        state, "alpha.mod", "def register(api): pass\n",
                        compatibility={
                            "mode": "game-version",
                            "game_versions": ["0.4.61"],
                        })
                    _write_registry(state, (compatible,))
                    approval = {
                        "schema": 3,
                        "game_root": str(game.resolve()),
                        "original_client": str(root / "backup" / "Client.exe"),
                        "original_sha256": "A" * 64,
                        "installed_sha256": "B" * 64,
                        "pack_id": "loader-0.4.61",
                        "created_at": "now",
                        "pack_digest": "C" * 64,
                        "key_id": "test",
                        "mod_version": "0.4.61",
                        "game_version": "0.4.62",
                        "compatibility_source_game_version": "0.4.61",
                    }
                    approval.update(change)
                    (state / "install-state.json").write_text(
                        json.dumps(approval), encoding="utf-8")
                    loader = ExternalModLoader(
                        state / "mods.json",
                        game_identity=GameIdentity(
                            "0.4.62", "B" * 64, game))

                    self.assertEqual((), loader.load_enabled())
                    self.assertIn(
                        "does not support", loader.diagnostics[0].message)

    def test_exact_build_mod_never_uses_manager_version_bridge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            game = root / "game"
            game.mkdir()
            restricted = _install_mod(
                state, "alpha.mod", "def register(api): pass\n",
                compatibility={
                    "mode": "exact-game-build",
                    "game_builds": [{
                        "game_version": "0.4.61",
                        "client_sha256": "A" * 64,
                    }],
                })
            _write_registry(state, (restricted,))
            (state / "install-state.json").write_text(json.dumps({
                "schema": 3,
                "game_root": str(game.resolve()),
                "original_client": str(root / "backup" / "Client.exe"),
                "original_sha256": "A" * 64,
                "installed_sha256": "B" * 64,
                "pack_id": "loader",
                "created_at": "now",
                "pack_digest": "C" * 64,
                "key_id": "test",
                "mod_version": "1",
                "game_version": "0.4.62",
                "compatibility_source_game_version": "0.4.61",
            }), encoding="utf-8")
            loader = ExternalModLoader(
                state / "mods.json",
                game_identity=GameIdentity("0.4.62", "B" * 64, game))

            self.assertEqual((), loader.load_enabled())
            self.assertIn("does not support", loader.diagnostics[0].message)

    def test_wrong_or_unapproved_game_build_is_isolated_before_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for label, builds, identity, message in (
                    ("wrong-version", [{
                        "game_version": "0.4.60",
                        "client_sha256": "A" * 64,
                    }], GameIdentity("0.4.61", "A" * 64), "does not support"),
                    ("wrong-hash", [{
                        "game_version": "0.4.61",
                        "client_sha256": "B" * 64,
                    }], GameIdentity("0.4.61", "A" * 64), "does not support"),
                    ("unapproved", [],
                     GameIdentity("0.4.61", "A" * 64), "no approved")):
                with self.subTest(label=label):
                    state = root / label
                    blocked = _install_mod(
                        state, "bad.mod",
                        "raise RuntimeError('must not import')\n",
                        compatibility={
                            "mode": "exact-game-build",
                            "game_builds": builds,
                        })
                    loader = ExternalModLoader(
                        _write_registry(state, (blocked,)),
                        game_identity=identity)
                    self.assertEqual((), loader.load_enabled())
                    self.assertIn(message, loader.diagnostics[0].message)

    def test_identity_probe_is_lazy_and_failure_blocks_only_restricted_mods(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def unavailable():
                raise ModLoaderError("test identity unavailable")

            legacy_state = root / "legacy"
            legacy = _install_mod(
                legacy_state, "alpha.mod", "def register(api): pass\n")
            legacy_loader = ExternalModLoader(
                _write_registry(legacy_state, (legacy,)),
                identity_loader=lambda: (_ for _ in ()).throw(
                    AssertionError("legacy mod must not inspect game identity")))
            self.assertEqual(1, len(legacy_loader.load_enabled()))
            legacy_loader.shutdown()

            restricted_state = root / "restricted"
            restricted = _install_mod(
                restricted_state, "beta.mod", "def register(api): pass\n",
                compatibility={
                    "mode": "exact-game-build",
                    "game_builds": [{
                        "game_version": "0.4.61",
                        "client_sha256": "A" * 64,
                    }],
                })
            restricted_loader = ExternalModLoader(
                _write_registry(restricted_state, (restricted,)),
                identity_loader=unavailable)
            self.assertEqual((), restricted_loader.load_enabled())
            self.assertIn(
                "cannot verify compatible game build",
                restricted_loader.diagnostics[0].message)

    def test_game_identity_reads_json_version_and_hashes_exact_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "Client.exe"
            executable.write_bytes(b"clean-client")
            (root / "version.txt").write_text(
                '"0.4.61"\n', encoding="utf-8")

            identity = detect_game_identity(executable)

            self.assertEqual("0.4.61", identity.game_version)
            self.assertEqual(
                hashlib.sha256(b"clean-client").hexdigest().upper(),
                identity.client_sha256)

    def test_disabled_future_and_crashing_mods_do_not_block_healthy_mod(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            disabled = _install_mod(
                state, "bad.disabled", "raise RuntimeError('must not import')\n",
                enabled=False)
            future = _install_mod(
                state, "future.mod", "def register(api): pass\n", loader_api=99)
            broken = _install_mod(
                state, "bad.mod",
                "def register(api):\n    raise RuntimeError('broken mod')\n")
            healthy = _install_mod(
                state, "alpha.mod",
                "def register(api):\n    api.on('client.draw', lambda: 'ok')\n")
            loader = ExternalModLoader(
                _write_registry(state, (disabled, future, broken, healthy)))
            loaded = loader.load_enabled()

            self.assertEqual(("alpha.mod",), tuple(item.mod_id for item in loaded))
            self.assertEqual(("ok",), loader.emit("client.draw"))
            self.assertEqual(
                {"future.mod", "bad.mod"},
                {item.mod_id for item in loader.diagnostics})
            loader.shutdown()

    def test_registry_path_escape_namespace_collision_and_manifest_change_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            escaped = _install_mod(
                state, "escaped.mod", "def register(api): pass\n")
            escaped["install_path"] = str((root / "outside").resolve())
            loader = ExternalModLoader(_write_registry(state, (escaped,)))
            self.assertEqual((), loader.load_enabled())
            self.assertIn("escapes", loader.diagnostics[0].message)

            state2 = root / "state2"
            collision = _install_mod(
                state2, "alpha.mod", "def register(api): pass\n",
                entrypoint="other.module:register")
            loader = ExternalModLoader(_write_registry(state2, (collision,)))
            self.assertEqual((), loader.load_enabled())
            self.assertIn("namespace", loader.diagnostics[0].message)

            state3 = root / "state3"
            changed = _install_mod(
                state3, "alpha.mod", "def register(api): pass\n")
            manifest = Path(changed["install_path"]) / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            loader = ExternalModLoader(_write_registry(state3, (changed,)))
            self.assertEqual((), loader.load_enabled())
            self.assertIn("differs", loader.diagnostics[0].message)

    def test_malformed_registry_blocks_loader_without_importing_anything(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            path = state / "mods.json"
            path.write_text('{"schema":1,"mods":[],"load_order":["ghost"]}',
                            encoding="utf-8")
            loader = ExternalModLoader(path)
            with self.assertRaisesRegex(ModLoaderError, "load order"):
                loader.load_enabled()

    def test_current_and_loader_v4_keyless_receipts_both_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for schema in (1, 2):
                with self.subTest(schema=schema):
                    state = root / f"schema-{schema}"
                    installed = _install_mod(
                        state, "alpha.mod", "def register(api): pass\n",
                        receipt_schema=schema)
                    loader = ExternalModLoader(
                        _write_registry(state, (installed,)))
                    self.assertEqual(1, len(loader.load_enabled()))
                    self.assertEqual((), loader.diagnostics)
                    loader.shutdown()

    def test_changed_or_extra_installed_payload_is_isolated_before_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changed_state = root / "changed"
            changed = _install_mod(
                changed_state, "alpha.mod",
                "def register(api):\n    raise RuntimeError('must not import')\n")
            module = (Path(changed["install_path"]) / "mod" / "alpha" / "mod.py")
            module.write_text("raise RuntimeError('tampered')\n", encoding="utf-8")
            loader = ExternalModLoader(_write_registry(changed_state, (changed,)))
            self.assertEqual((), loader.load_enabled())
            self.assertIn("integrity", loader.diagnostics[0].message)

            extra_state = root / "extra"
            extra = _install_mod(
                extra_state, "beta.mod", "def register(api): pass\n")
            (Path(extra["install_path"]) / "mod" / "surprise.py").write_text(
                "raise RuntimeError('must not import')\n", encoding="utf-8")
            loader = ExternalModLoader(_write_registry(extra_state, (extra,)))
            self.assertEqual((), loader.load_enabled())
            self.assertIn("extra files", loader.diagnostics[0].message)

    def test_import_does_not_leave_bytecode_that_breaks_next_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            installed = _install_mod(
                state, "alpha.mod", "def register(api): pass\n")
            loader = ExternalModLoader(_write_registry(state, (installed,)))
            self.assertEqual(1, len(loader.load_enabled()))
            loader.shutdown()
            self.assertFalse(any(
                path.name == "__pycache__"
                for path in Path(installed["install_path"]).rglob("*")))


if __name__ == "__main__":
    unittest.main()
