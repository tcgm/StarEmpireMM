# Changelog

## v0.4

- Official game updates are handled automatically when mod support is next
  enabled. The Manager verifies every required loader hook against the updated
  client, saves a fresh vanilla backup, and installs only after the isolated
  rebuild passes.
- Public release numbering is now separate from the older private loader
  capability number, so the Manager no longer rejects its own valid bundled
  compatibility template.
- If bundled compatibility support is missing or damaged, the Manager asks for
  a Manager update instead of asking players to select internal packages.

## v0.3

- The Mods list now notices changes to the loader registry while the Manager
  is open, so it always shows the mods that will load with the game.
- Registry checks run quietly in the background and when the Manager regains
  focus without repeatedly rebuilding an unchanged list.

## v0.2

Manager and public release version are both 0.2.

- Added safe compatibility support for Star Empire 0.4.66's managed turret
  window queue while retaining support for the earlier profiler boundary.
- Missing, duplicate, or mixed foreground boundaries still stop safely instead
  of guessing where loader code belongs.

## v0.1

First public release.

- One-click global mod support with clear enabled and disabled states.
- Drag-and-drop and file-picker installation for keyless `.semod` packages.
- New mods install disabled and load only after the player enables them and starts the game.
- Automatic compatibility preparation, verified vanilla backup and recoverable replacement.
- Game-update detection based on the current installation rather than old executable code.
- Per-mod compatibility warnings and an explicit force-load option.
- GitHub Release update checks for mods that declare an update source.
- Readable live operation output, parsed logs and diagnostics export.
- Mod README and preview-image panel.
- Windows permission recovery through a narrowly scoped elevated helper.
