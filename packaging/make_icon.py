#!/usr/bin/env python3
"""Render packaging/icon.svg into packaging/icon.icns for the .app bundle.

Rendering each size straight from the SVG keeps every icon crisp (no upscaling). Needs
`cairosvg` (pip, in the `desktop` extra) to rasterise, and macOS `iconutil` to pack the
iconset into a .icns. The .app build works without an icon — this just gives it a real one.

    python packaging/make_icon.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SVG = HERE / "icon.svg"
ICONSET = HERE / "icon.iconset"
ICNS = HERE / "icon.icns"

# The exact names/sizes macOS `iconutil` expects in an .iconset directory.
SIZES = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]


def main() -> int:
    try:
        import cairosvg
    except ImportError:
        sys.exit("cairosvg is not installed. Run: pip install cairosvg  (in the desktop extra)")

    ICONSET.mkdir(exist_ok=True)
    svg_bytes = SVG.read_bytes()
    for name, px in SIZES:
        cairosvg.svg2png(bytestring=svg_bytes, write_to=str(ICONSET / name),
                         output_width=px, output_height=px)
    print(f"rendered {len(SIZES)} sizes into {ICONSET}")

    if sys.platform != "darwin":
        print("not on macOS — the PNGs are ready, but `iconutil` (macOS) is needed to pack")
        print(f"the .icns. On a Mac: iconutil -c icns {ICONSET} -o {ICNS}")
        return 0

    subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)], check=True)
    print(f"wrote {ICNS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
