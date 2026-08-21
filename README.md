# Star Empire Mod Manager

Star Empire Mod Manager installs and controls community mods without asking players to edit game files by hand.

The Manager has one large status button. Green means mod support is enabled; red means the original game executable is active. Installed mods have their own ON or OFF state and are loaded the next time Star Empire starts.

## What it does

- Installs `.semod` files from the Install button or by dragging them into the window.
- Installs new mods disabled so the player chooses when each one becomes active.
- Enables and disables mods for the next game launch.
- Prepares the small game-side loader automatically and keeps a verified vanilla backup.
- Detects a changed game build and rebuilds compatibility from the updated installation instead of copying code from an older executable.
- Checks mod compatibility and allows an explicit force-load choice for version mismatches.
- Checks GitHub Releases for mod updates when a mod declares an update source.
- Shows readable logs and exports a privacy-conscious diagnostics archive.
- Restores vanilla mode through the same main status button.

External `.semod` packages do not need signing keys. The Manager checks their manifest, declared inventory, paths and SHA-256 hashes before installation. Its version-specific internal loader is separate infrastructure and is not shown as a mod.

## Install and use

1. Download `StarEmpireModManager.exe` from this repository's Releases page.
2. Run it and select the folder containing the official `Client.exe` if the game is not found automatically.
3. Drag a `.semod` file into the Mods page, or use Install Mod.
4. Select the installed mod and click Enable.
5. Start Star Empire normally.

Use Disable to keep an installed mod from loading on the next start. Use Uninstall to remove it from the Manager. Click the large green Manager button to restore the verified vanilla executable; click the red button to prepare mod support again.

The Manager normally runs without administrator rights. If Windows protects the selected game folder, it requests elevation only for the restricted file replacement step.

## Source layout

- `installer` contains the desktop Manager, package handling, compatibility transactions and diagnostics.
- `mod_loader` contains the small external-mod runtime.
- `tools` contains the repeatable package, compatibility and Manager build tools.
- `tests` contains the public manager and loader tests.
- `docs` contains the user, architecture and development guides.

No game executable, extracted game source, private compatibility patch, mod package, signing key, player state or diagnostic log belongs in this repository.

Star Empire Mod Manager is a community project and is not affiliated with or endorsed by the Star Empire developers.
