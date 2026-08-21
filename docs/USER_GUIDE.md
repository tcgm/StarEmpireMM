# User guide

## Manager status

The wide button near the top controls global mod support.

- Green: the loader is prepared and enabled.
- Red: the verified vanilla executable is active.

Clicking the green state restores vanilla mode. Clicking the red state prepares mod support from the selected installation. Individual mod choices are kept when changing the global state.

## Installing a mod

Drag one or more `.semod` files anywhere over the Manager window, or click Install Mod. A new mod is stored in the Manager's data folder and begins in the OFF state. Select it and click Enable when you want it loaded on the next Star Empire start.

The Manager refuses malformed archives, unsafe paths, undeclared files, changed payload hashes, executables and other forbidden content. Mod authors do not need to distribute signing keys.

## Compatibility

The mod list shows each mod's declared game compatibility. A mismatch is a warning, not permission to ignore package safety. Force load anyway bypasses only the declared game-version mismatch; it cannot bypass damaged packages, unsupported loader APIs or integrity failures.

When Star Empire updates, the Manager compares the current installation with its recorded vanilla and modded states. An unfamiliar but internally consistent build is prepared in an isolated temporary folder, checked, and then installed. It never restores code from an older game build.

## Logs and recovery

The operation window shows the current action in plain language. The Logs page shows parsed Manager and loader messages. Export Diagnostics creates a bounded archive suitable for a bug report; inspect it before sharing.

If Windows denies replacement inside a protected game folder, approve the Manager's restricted administrator helper. The main application remains unelevated. Interrupted operations can be repaired from the recorded transaction and verified backup.
