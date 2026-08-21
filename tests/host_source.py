"""Resolve game-owned host source for live-baseline or staging tests."""

from __future__ import annotations

import os
from pathlib import Path


def host_internal(repo_root: Path) -> Path:
    override = os.environ.get("STAR_EMPIRE_UI_GAME_INTERNAL")
    if override:
        return Path(override).resolve()
    return repo_root / "game" / "_internal"
