"""Write app.json, the file the app's own updater reads from a release.

    python packaging/manifest.py dist            # writes dist/app.json

It names the launcher version the release's app contains, and the size and SHA-256 of each
download. An installed app compares that launcher version with its own. If the release's is
newer, it downloads the zip, checks it against these numbers, and replaces itself
(src/explorer_loader/appupdate.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from explorer_loader import LOADER_VERSION, MIN_MACOS  # noqa: E402
from explorer_loader.appupdate import DMG, MANIFEST, ZIP  # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(dist: Path) -> dict:
    files = {name: {"sha256": sha256(dist / name), "size": (dist / name).stat().st_size}
             for name in (ZIP, DMG) if (dist / name).exists()}
    if ZIP not in files:
        raise SystemExit(f"no {ZIP} in {dist}: the updater installs from the zip")
    return {
        "loader_version": LOADER_VERSION,
        "min_macos": MIN_MACOS,
        "archs": ["arm64", "x86_64"],          # CI refuses to publish an app without both
        "tag": os.environ.get("TAG", ""),
        "commit": os.environ.get("GITHUB_SHA", ""),
        "signed": "developer-id" if os.environ.get("SIGNING_IDENTITY") else "ad-hoc",
        "files": files,
    }


def main(argv: list[str] | None = None) -> int:
    dist = Path((argv or sys.argv[1:] or ["dist"])[0])
    manifest = build(dist)
    (dist / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
