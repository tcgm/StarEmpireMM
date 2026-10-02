# Development and release

Use Python 3.14 x64 on Windows. The Manager uses the standard library plus the dependencies exercised by the checked-in tests and frozen build specification. PyInstaller is required only for the standalone executable. Build scripts check dependencies but never install them automatically.

Run the public test suite from the repository root:

```powershell
py -3.14 -B -m unittest discover -s tests -p "test_*.py"
```

Build a standalone Manager into a new output directory:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_manager.ps1 `
  -OutputRoot G:\path\to\new-build `
  -EmbeddedLoaderPackages G:\private\release\current.seloader
```

If `-EmbeddedLoaderPackages` is omitted, the build script auto-discovers every
`*.seloader` file under `.private-release\` (gitignored, repository-local) and
embeds those instead. The build stops if none are found or if any package fails
signature or compatibility verification. Keep a copy of the current signed
`.seloader` in `.private-release\` so an ordinary `build.ps1` run without flags
still produces a usable Manager.

For a pre-alpha test build, use a new build directory and label the handoff
clearly as testing-only. A public test release must be marked as a GitHub
pre-release, with unverified gameplay called out; do not present it as stable.

The internal loader package is version-specific release infrastructure. Keep its private signing material and authorised compatibility workspace outside Git. Never copy an installed game, `Client.exe`, extracted official modules, private diffs, player data, logs or credentials into the public tree.

Before a release:

1. Work on a feature branch and inspect the exact diff.
2. Run the complete public tests.
3. Build into a new empty directory and run the frozen self-test and artifact audit.
4. Scan the tracked tree and release asset for forbidden game material, executable payloads and secrets.
5. Record the executable's SHA-256 value in the release notes or checksum asset.
6. Check the latest public GitHub release and increment that sequence by one step. Public tags use `v0.1`, `v0.2`, `v0.3` and so on.
7. Use that exact same number for `MANAGER_VERSION`, the build folder, release ledger, GitHub tag, release title and release description.
8. Verify every version-bearing file and asset before publishing. Do not maintain a separate internal version sequence.
9. Open a pull request, merge it, tag the merged commit and upload only the reviewed Manager executable and checksum file.

On 23 August 2026, internal Manager version `0.4.7` did not match public release `v0.2`. The internal version was changed to `0.2`, and exact version alignment became a mandatory release step.

To roll back a source change, revert its merge commit. To roll back a local game operation, use the Manager's global disabled state so it restores the verified vanilla executable rather than copying files by hand.

## Compatibility verification for new game builds

`tools/build_version_binding.py` proves a game build is safe to integrate
before any hook offsets are computed. It has two baseline probes:

- **Strict** (`verify_clean_client_archive`, the default) requires the
  entire loose `Client.py` shipped with the game to match the frozen
  bytecode inside `Client.exe` byte-for-byte. This is the right default: it
  catches anything unexpected anywhere in the file.
- **Targeted** (`verify_targeted_client_archive`) only requires (1) the
  module's own top-level structure -- imports, class/function layout, the
  `__main__` guard -- to match, and (2) each individual anchor *statement*
  a release policy's hooks actually insert relative to (see
  `_client_run_anchor_nodes` / `_render_mixin_anchor_nodes`) to appear,
  unambiguously, somewhere in the corresponding frozen function. Everything
  else -- including the rest of that same function -- may differ freely.

The targeted probe exists because a strict byte-for-byte match fails
whenever a game update changes *anything*, even code no mod hooks into.
This happened twice in practice: 0.5.293 rewrote its networking layer
(`App._start_network`), and 0.5.296 added a new mission-detail-popup
scroll handler inside `SolarSystemWindow.run` itself -- the same giant
(~70,000-instruction) function every UI-mod hook anchors against, just in
an unrelated branch of its event loop. Strict verification correctly
refused both builds.

The first version of the targeted probe only got the 0.5.293 case right:
it required an entire *named function* (`SolarSystemWindow.run`,
`RenderMixin._draw_station_overlay`) to match byte-for-byte, which happened
to work when the only change was elsewhere in the file, but failed again
on 0.5.296 since the change landed inside `run` itself, far from any real
anchor. The fix was to verify at **statement** granularity instead:

1. Re-run the same AST anchor-finders `locate_release_offsets` already
   uses, but keep the anchor *node* instead of only its byte offset.
2. Compile the loose source and disassemble the frozen function with `dis`.
3. Build a normalized instruction pattern for just the anchor statement's
   own line span -- jump targets and nested code objects are normalized
   away first, since they drift with the length of *any* preceding code in
   the function even when nothing semantically changed.
4. Search for that pattern as a contiguous subsequence of the frozen
   function's full instruction stream. If it's not found, or found more
   than once, the anchor is treated as missing/ambiguous and the probe
   fails closed. A short pattern that's genuinely ambiguous (occurs several
   times) is retried with a few more source lines of surrounding context on
   each side before giving up.
5. For an `ast.If` anchor (the turret/quit-event guard), only `node.body` is
   used, never `node.orelse` -- `elif` is represented as a nested `If` in
   `orelse`, so the naive node span would swallow every later branch of the
   same pygame event-dispatch chain, including whatever unrelated branch
   just changed.

This is the same precision-targeting model Minecraft Forge coremods use:
locate a specific instruction pattern at the injection point, not a whole
method or a whole jar.

`candidate_builder.py`'s compatibility-test path (the Manager's "Prepare
Game" / isolated compatibility test flow) tries strict first and falls
back to targeted automatically only if strict fails, recording which mode
succeeded (`BuiltCandidate.verification_mode`) so the UI can disclose the
weaker guarantee instead of hiding it. The standalone CLI also accepts
`--verification strict|targeted` directly.

Targeted verification does not weaken what gets installed -- the inserted
hook bytes are identical either way, and anything outside the reviewed
anchors was never read to build the candidate. It only widens what counts
as "this build is safe to hook," for code that is provably irrelevant to
where the hooks go.

## Building a self-signed loader for an unlisted game build

A `.seloader`/`.semod` package is only trusted if its `key_id` is deployed
in `installer/trusted_keys.py`'s `BUILTIN_TRUSTED_KEYS` -- the official
signer, currently dezgard's alpha key. For a game build nobody has
officially signed a package for yet (e.g. a brand-new patch), you have two
options that don't involve waiting:

1. **Sign it yourself with a personal, local key.** `verify_mod_package()`
   has no special case for this -- a package is either trusted or it
   isn't -- but your own Manager can be told to trust your own key, and
   only yours, by adding its public half to the *local* trust file
   (`load_trust_configuration()` reads `trusted-public-keys.json` next to
   the Manager's state directory, merged with `BUILTIN_TRUSTED_KEYS`). This
   never touches the repository and never affects anyone else's Manager.
2. **Install it unsigned.** `verify_mod_package(..., allow_unsigned=True)`
   accepts a package whose `key_id` isn't trusted at all, running every
   other check (structure, hashes, forbidden content, embedded recipe
   parsing) and marking the result `insecure=True`. The Manager's settings
   screen has a confirm-gated "allow unsigned package" toggle, and the UI
   shows an unverified-authorship warning before Stage 1 ever touches such
   a package. Nothing about the package's *content* is skipped -- only the
   Ed25519 signature that proves who signed it.

`.private-release/work/build_mod_loader_v1_0_5_293.py` is a worked example
of option 1 for Star Empire 0.5.293, built after 0.5.293's networking
rewrite required targeted verification (see above) to pass the
compatibility check at all. It:

1. Runs `build_version_binding()` against the live game install with
   `baseline_probe=verify_targeted_client_archive`, producing a real
   reviewed binding + hook recipe via the normal structural locator --
   nothing here hand-computes byte offsets.
2. Generates a fresh, disposable Ed25519 keypair and writes the private key
   under `.private-release/` (already gitignored; never commit it).
3. Signs and self-verifies the `.seloader` via `build_mod_package()`.
4. Registers the new public key in this machine's local
   `trusted-public-keys.json` only.

Copy that script as a starting point for a different game version or
release policy: change `GAME_VERSION`, `KEY_ID`, and the release-policy
import, then rerun. If the target policy's Client-side hooks anchor against
statements outside `_client_run_anchor_nodes` / `_render_mixin_anchor_nodes`
(`tools/build_version_binding.py`), add them there first or the targeted
probe will have nothing to verify those anchors against.

A build produced this way is real, self-verified, and installs cleanly --
but it is not reviewed by anyone else. Treat it as a personal development
aid, not a release candidate.
