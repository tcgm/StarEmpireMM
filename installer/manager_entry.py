"""Absolute-import entry point and headless smoke test for the Mod Manager."""

import sys
from pathlib import Path


def self_test() -> int:
    """Verify dynamic release imports and the native process safety boundary."""
    from installer.candidate_builder import build_candidate
    from installer.manager_core import running_game_processes
    from installer.manager_transaction import ManagerTransaction
    from installer.elevated_copy import (handle_elevated_copy_request,
                                         request_elevated_atomic_copy)
    from installer.manager_mods import ModManagerController
    from installer.manager_settings import load_manager_settings
    from installer.diagnostics import export_diagnostics
    from installer.mod_package import verify_mod_package
    from installer.mod_updates import acquire_github_update
    from installer.semod_package import verify_semod_package
    from installer.update_feed import fetch_update_feed
    from mod_loader.runtime import ExternalModLoader
    from tools.build_version_binding import locate_target_vitals_offsets
    from tools.repack_client import repack_client

    required = (build_candidate, ManagerTransaction, verify_mod_package,
                fetch_update_feed, locate_target_vitals_offsets,
                repack_client, ModManagerController, load_manager_settings,
                export_diagnostics, acquire_github_update,
                verify_semod_package, ExternalModLoader)
    required += (handle_elevated_copy_request, request_elevated_atomic_copy)
    if not all(callable(value) for value in required):
        return 2
    return 0 if running_game_processes().verified else 3


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments[:1] == ["--elevated-copy-request"]:
        if len(arguments) != 2:
            raise SystemExit(64)
        from installer.elevated_copy import handle_elevated_copy_request
        raise SystemExit(handle_elevated_copy_request(Path(arguments[1])))
    if "--self-test" in sys.argv[1:]:
        raise SystemExit(self_test())
    from installer.manager_mods import default_mod_state_root
    from installer.version import record_startup
    try:
        record_startup(default_mod_state_root())
    except OSError:
        # Diagnostics must never prevent the recovery Manager from opening.
        pass
    from installer.manager_ui import main
    main()
