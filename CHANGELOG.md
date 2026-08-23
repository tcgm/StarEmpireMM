# Changelog

## v0.4.7

- Added safe compatibility support for Star Empire 0.4.66's managed turret
  window queue while retaining support for the earlier profiler boundary.
- Missing, duplicate, or mixed foreground boundaries still stop safely instead
  of guessing where loader code belongs.

## v0.1

First public release, built from Mod Manager 0.4.6.

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
