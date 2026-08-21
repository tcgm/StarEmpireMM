# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the game-code-free standalone Mod Manager."""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


# PyInstaller exposes SPECPATH as the directory containing this spec file.
repository = Path(SPECPATH).parent
hiddenimports = sorted(set(
    collect_submodules("PyInstaller.archive")
    + collect_submodules("mod_loader")
    + collect_submodules("tkinterdnd2")
    + ["tools.build_version_binding", "tools.repack_client"]
))
datas = collect_data_files("PyInstaller")
datas.extend(
    (source, destination)
    for source, destination in collect_data_files("tkinterdnd2")
    if Path(destination).name in {"win-x64", "win-x64-tcl9"}
)
embedded_loader_value = os.environ.get(
    "STAR_EMPIRE_MANAGER_EMBEDDED_LOADERS", "")
embedded_loader_names = set()
for raw_path in filter(None, embedded_loader_value.split(os.pathsep)):
    loader_path = Path(raw_path).resolve(strict=True)
    folded_name = loader_path.name.casefold()
    if (not loader_path.is_file()
            or loader_path.suffix.casefold() != ".seloader"):
        raise ValueError(
            f"embedded compatibility input must be a .seloader file: {loader_path}")
    if folded_name in embedded_loader_names:
        raise ValueError(f"duplicate embedded compatibility filename: {loader_path.name}")
    embedded_loader_names.add(folded_name)
    datas.append((str(loader_path), "embedded_loaders"))

analysis = Analysis(
    [str(repository / "installer" / "manager_entry.py")],
    pathex=[str(repository)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "unittest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="StarEmpireModManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=False,
)
