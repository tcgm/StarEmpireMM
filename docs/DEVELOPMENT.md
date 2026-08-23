# Development and release

Use a current Python installation on Windows. The Manager uses the standard library plus the dependencies exercised by the checked-in tests and frozen build specification. PyInstaller is required only for the standalone executable.

Run the public test suite from the repository root:

```powershell
python -B -m unittest discover -s tests -p "test_*.py"
```

Build a standalone Manager into a new output directory:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_manager.ps1 `
  -OutputRoot G:\path\to\new-build `
  -EmbeddedLoaderPackages G:\private\release\current.seloader
```

The internal loader package is version-specific release infrastructure. Keep its private signing material and authorised compatibility workspace outside Git. Never copy an installed game, `Client.exe`, extracted official modules, private diffs, player data, logs or credentials into the public tree.

Before a release:

1. Work on a feature branch and inspect the exact diff.
2. Run the complete public tests.
3. Build into a new empty directory and run the frozen self-test and artifact audit.
4. Scan the tracked tree and release asset for forbidden game material, executable payloads and secrets.
5. Record the executable's SHA-256 value in the release notes or checksum asset.
6. Check the latest public GitHub release and increment that public sequence by one step. Public tags use `v0.1`, `v0.2`, `v0.3` and so on.
7. Keep the public release number separate from the internal Manager version. For example, public release `v0.2` may contain Manager version `0.4.7`. Never derive the GitHub tag or release title from `MANAGER_VERSION`.
8. State both numbers in the release notes, then verify the proposed tag and title against the previous public release before publishing.
9. Open a pull request, merge it, tag the merged commit and upload only the reviewed Manager executable and checksum file.

On 23 August 2026, internal Manager version `0.4.7` was mistakenly used as the public GitHub release tag after `v0.1`. The incorrect release was replaced by public release `v0.2`. This is why checking and recording both version identities is now a mandatory release step.

To roll back a source change, revert its merge commit. To roll back a local game operation, use the Manager's global disabled state so it restores the verified vanilla executable rather than copying files by hand.
