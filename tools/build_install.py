"""Build a mod's .semod from manifest.json + source/ and install it locally.

Usage: python tools/build_install.py <mod-root>

<mod-root> is a mod checkout (e.g. ModDev/ship3d-mod) holding manifest.json
and source/. Manifest file paths look like "mod/<package>/<rel>"; <rel> is
read from <mod-root>/source/<rel>. The package is written to
<mod-root>/<mod_id>.semod, verified, and installed (enabled) into the
manager state dir under %LOCALAPPDATA%/StarEmpireModManager.
"""
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def build(mod_root: Path) -> Path:
    manifest_bytes = (mod_root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    out = mod_root / f"{manifest['mod_id']}.semod"
    payloads = [{"path": "manifest.json", "size": len(manifest_bytes),
                 "sha256": sha256(manifest_bytes)}]
    file_bytes = {}
    for rel in manifest["files"]:
        parts = rel.split("/", 2)
        if len(parts) != 3 or parts[0] != "mod":
            raise ValueError(f"unexpected manifest path: {rel}")
        data = (mod_root / "source" / parts[2]).read_bytes()
        file_bytes[rel] = data
        payloads.append({"path": rel, "size": len(data), "sha256": sha256(data)})
    payloads.sort(key=lambda p: p["path"])
    package_bytes = json.dumps({
        "schema": 2,
        "format": "star-empire-mod-package",
        "manifest_sha256": sha256(manifest_bytes),
        "payloads": payloads,
    }).encode("utf-8")

    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest_bytes)
        zf.writestr("package.json", package_bytes)
        for rel, data in file_bytes.items():
            zf.writestr(rel, data)
    print(f"Built {out} ({out.stat().st_size} bytes)")
    return out


def install(semod: Path) -> None:
    from installer.semod_package import verify_semod_package
    from installer.mod_service import ModService

    state_root = Path(os.environ["LOCALAPPDATA"]) / "StarEmpireModManager"
    verified = verify_semod_package(semod)
    print("Verified:", verified.manifest.mod_id, verified.manifest.version)
    result = ModService(state_root).install(
        verified, source=str(semod), enable=True, require_closed=False)
    print("Install result:", result)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    install(build(Path(sys.argv[1]).resolve()))
