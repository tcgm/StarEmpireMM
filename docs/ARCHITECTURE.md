# Architecture

Star Empire Mod Manager keeps three responsibilities separate.

The desktop Manager owns package installation, mod state, updates, diagnostics and recoverable game-file transactions. The small loader owns the stable runtime event API used by enabled mods. Each `.semod` owns its own code, assets, manifest and update declaration outside the game folder.

External mods are keyless. A package is accepted only when its manifest is valid, every archive path is safe, the inventory is exact, every payload hash matches and forbidden executable or game content is absent.

Compatibility infrastructure is internal to the Manager. A version-specific loader recipe contains a narrow authored hook description rather than a copy of the game's source. Candidate preparation happens outside the installation, checks loose and frozen module agreement where available, audits the result, and performs an atomic replacement only after validation. The original executable is retained as a verified recovery point.

Runtime state, installed mods, logs, downloads and backups live under the user's local application-data area rather than in this repository. Temporary work is created beside its managed destination so Windows permissions remain consistent, then removed after success or controlled recovery.

The public source boundary includes `installer`, `mod_loader`, generic build tools, documentation and tests. It excludes official game files, extracted game modules, private integration captures, signing keys, built packages and player data.
