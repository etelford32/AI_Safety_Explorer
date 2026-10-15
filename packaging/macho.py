"""Mach-O architectures in the app: report them, require both, and fuse the ones that lack one.

The app runs on Intel and on Apple Silicon only if every native binary it carries has both
slices. The Python runtime, pyobjc and the launcher arrive universal. A package that
publishes separate x86_64 and arm64 wheels, rather than one universal2 wheel, installs only
the build machine's slice. The Anthropic and OpenAI SDKs' `jiter` and `pydantic_core` are
examples. On an Intel Mac the app would start, and then fail the first time a campaign
imported one of them.

    python packaging/macho.py report  PATH     every Mach-O file under PATH and its slices
    python packaging/macho.py check   PATH     exit 1 if any lacks arm64 or x86_64
    python packaging/macho.py fuse             make the build environment's packages universal
    python packaging/macho.py sign-order APP   what codesign must sign, inside out (sign.sh)

`fuse` looks at the site-packages of the running interpreter. For each distribution with a
binary missing a slice, it downloads the same version's wheel for the missing architecture
from PyPI and writes a universal file holding both slices, exactly as `lipo -create` would.
It is pure Python, so it is tested on any OS. CI checks the result with `lipo` and runs the
x86_64 slice under Rosetta.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import struct
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path

ARCHS = ("arm64", "x86_64")
CPU = {0x0100000C: "arm64", 0x01000007: "x86_64", 0x0000000C: "arm", 0x00000007: "i386"}
CPU_BY_NAME = {v: k for k, v in CPU.items()}
PLATFORM = {"x86_64": "macosx_11_0_x86_64", "arm64": "macosx_11_0_arm64"}

MH_MAGIC_64 = b"\xcf\xfa\xed\xfe"   # thin 64-bit, little-endian (both archs we ship)
MH_MAGIC = b"\xce\xfa\xed\xfe"      # thin 32-bit, little-endian
FAT_MAGIC = b"\xca\xfe\xba\xbe"
FAT_MAGIC_64 = b"\xca\xfe\xba\xbf"
ALIGN = 14                          # 2**14: what lipo uses for arm64 and x86_64 alike


def slices(data: bytes) -> list[tuple[int, int, int, int]] | None:
    """(cputype, cpusubtype, offset, size) of each slice, or None if `data` is not Mach-O."""
    head = data[:4]
    if head in (MH_MAGIC_64, MH_MAGIC) and len(data) >= 12:
        cputype, cpusubtype = struct.unpack("<II", data[4:12])
        return [(cputype, cpusubtype, 0, len(data))]
    if head in (FAT_MAGIC, FAT_MAGIC_64) and len(data) >= 8:
        n = struct.unpack(">I", data[4:8])[0]
        # A Java class file shares FAT_MAGIC; its "count" is a version number (>= 45).
        if not 0 < n < 20:
            return None
        wide = head == FAT_MAGIC_64
        size = 32 if wide else 20
        if len(data) < 8 + n * size:
            return None
        out = []
        for i in range(n):
            entry = data[8 + i * size: 8 + (i + 1) * size]
            if wide:
                cputype, cpusubtype, offset, length = struct.unpack(">IIQQ", entry[:24])
            else:
                cputype, cpusubtype, offset, length = struct.unpack(">IIII", entry[:16])
            out.append((cputype, cpusubtype, offset, length))
        return out
    return None


def archs_of(path: Path) -> tuple[str, ...] | None:
    """The architectures a file carries, or None if it is not a Mach-O binary."""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            if head not in (MH_MAGIC_64, MH_MAGIC, FAT_MAGIC, FAT_MAGIC_64):
                return None
            data = head + f.read(8 + 19 * 32)   # enough for any fat header we accept
    except OSError:
        return None
    found = slices(data)
    if found is None:
        return None
    return tuple(sorted(CPU.get(c, hex(c)) for c, *_ in found))


def binaries(root: Path):
    """(path, archs) for every Mach-O file under root. Symlinks are skipped (their targets
    are visited in their own right)."""
    root = Path(root)
    candidates = [root] if root.is_file() else (
        Path(d) / f for d, _, files in os.walk(root) for f in files)
    for p in candidates:
        if p.is_symlink() or not p.is_file():
            continue
        a = archs_of(p)
        if a is not None:
            yield p, a


MH_EXECUTE = 2
BUNDLE_SUFFIXES = (".app", ".framework", ".bundle", ".plugin", ".xpc", ".appex")


def filetype(path: Path) -> int | None:
    """The Mach-O file type (MH_EXECUTE, MH_DYLIB, MH_BUNDLE...) of a file's first slice."""
    with open(path, "rb") as f:
        data = f.read(8 + 19 * 32)
        found = slices(data)
        if not found:
            return None
        f.seek(found[0][2] + 12)
        raw = f.read(4)
    return struct.unpack("<I", raw)[0] if len(raw) == 4 else None


def sign_order(app: Path) -> list[tuple[str, Path]]:
    """Everything inside `app` that codesign must sign before the app itself, deepest first:
    ("exe", path) for executables (which get the app's entitlements), ("lib", path) for
    other Mach-O files, ("bundle", path) for nested bundles, after their contents. A
    framework is signed by its version directories; its `Current` symlink is skipped.
    Notarization rejects an app with any unsigned Mach-O file, and the loose extension
    modules under Resources are not reached by `codesign --deep`."""
    app = Path(app)
    items: list[tuple[str, Path]] = []
    for p, _ in binaries(app):
        items.append(("exe" if filetype(p) == MH_EXECUTE else "lib", p))
    for d, dirs, _ in os.walk(app):
        for name in dirs:
            q = Path(d) / name
            if q.is_symlink():
                continue
            if q.parent.name == "Versions" and q.parent.parent.suffix == ".framework":
                items.append(("bundle", q))
            elif q.suffix in BUNDLE_SUFFIXES and q.suffix != ".framework":
                items.append(("bundle", q))
    return sorted(items, key=lambda it: (-len(it[1].relative_to(app).parts), str(it[1])))


def lacking(root: Path, need=ARCHS) -> list[tuple[Path, tuple[str, ...]]]:
    return [(p, a) for p, a in binaries(root) if not set(need) <= set(a)]


def fat(thin: list[bytes]) -> bytes:
    """A universal binary holding each thin Mach-O slice, laid out as `lipo -create` does:
    ordered by CPU type (x86_64 before arm64), each slice aligned to 16 KiB."""
    entries = []
    for data in thin:
        found = slices(data)
        if found is None or len(found) != 1 or data[:4] not in (MH_MAGIC_64, MH_MAGIC):
            raise ValueError("fat() takes thin Mach-O slices")
        entries.append((found[0][0], found[0][1], data))
    if len({c for c, _, _ in entries}) != len(entries):
        raise ValueError("two slices for the same architecture")
    entries.sort(key=lambda e: e[0])
    step = 1 << ALIGN
    offset = 8 + 20 * len(entries)
    header = bytearray(FAT_MAGIC + struct.pack(">I", len(entries)))
    layout = []
    for cputype, cpusubtype, data in entries:
        offset = (offset + step - 1) // step * step
        layout.append((cputype, cpusubtype, offset, len(data)))
        offset += len(data)
    for cputype, cpusubtype, off, length in layout:
        header += struct.pack(">IIIII", cputype, cpusubtype, off, length, ALIGN)
    out = bytearray(header)
    for (_, _, off, _), (_, _, data) in zip(layout, entries):
        out += b"\0" * (off - len(out))
        out += data
    return bytes(out)


def thin_slice(data: bytes, arch: str) -> bytes | None:
    """The slice for `arch` from a thin or universal Mach-O file, as a thin file."""
    want = CPU_BY_NAME[arch]
    for cputype, _, offset, length in slices(data) or []:
        if cputype == want:
            return data[offset:offset + length]
    return None


def wheel_tag(dist) -> tuple[str, str]:
    """(python version, abi) of the wheel a distribution was installed from, e.g. ("3.12",
    "cp312"), so the other architecture's wheel is the same build. Falls back to this
    interpreter."""
    here = (f"{sys.version_info.major}.{sys.version_info.minor}",
            f"cp{sys.version_info.major}{sys.version_info.minor}")
    try:
        text = dist.read_text("WHEEL") or ""
    except OSError:
        return here
    for line in text.splitlines():
        if line.startswith("Tag:"):
            interp, abi, _ = line.split(":", 1)[1].strip().split("-", 2)
            if interp.startswith("cp") and interp[2:].isdigit():
                return f"{interp[2]}.{interp[3:]}", abi
    return here


def pip_download(name: str, version: str, arch: str, dest: Path,
                 tag: tuple[str, str] | None = None) -> Path:
    """The wheel for one distribution at one version, for the other Mac architecture."""
    py, abi = tag or (f"{sys.version_info.major}.{sys.version_info.minor}",
                      f"cp{sys.version_info.major}{sys.version_info.minor}")
    abis = [abi] + [a for a in (f"cp{py.replace('.', '')}", "abi3", "none") if a != abi]
    cmd = [sys.executable, "-m", "pip", "download", "--quiet", "--no-deps",
           "--only-binary=:all:", "--platform", PLATFORM[arch], "--python-version", py,
           "--implementation", "cp", "-d", str(dest), f"{name}=={version}"]
    for a in abis:
        cmd += ["--abi", a]
    subprocess.run(cmd, check=True)
    wheels = sorted(dest.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one wheel for {name}=={version} ({arch}), got {wheels}")
    return wheels[0]


def fuse(site: Path, need=ARCHS, download=pip_download, log=print) -> list[Path]:
    """Give every binary under `site` owned by an installed distribution the slices it lacks,
    taken from that distribution's wheel for the missing architecture. Returns what changed.

    Best effort, and it says so. A binary it cannot fix is reported, not fatal: the build
    environment holds packages the app does not carry. `check`, run on the built app, is
    the gate."""
    site = Path(site)
    todo = {p.resolve(): a for p, a in lacking(site, need)}
    if not todo:
        return []
    owners: dict[str, tuple[str, tuple[str, str], list[tuple[Path, str]]]] = {}
    for dist in importlib.metadata.distributions(path=[str(site)]):
        for f in dist.files or []:
            p = (site / f).resolve()
            if p in todo:
                owners.setdefault(dist.metadata["Name"],
                                  (dist.version, wheel_tag(dist), []))[2].append((p, f.as_posix()))
    owned = {p for _, _, files in owners.values() for p, _ in files}
    for p in sorted(set(todo) - owned):
        log(f"warning: no installed distribution owns {p}; left as {' + '.join(todo[p])}")
    changed = []
    for name, (version, tag, files) in sorted(owners.items()):
        for arch in sorted({a for p, _ in files for a in need if a not in todo[p]}):
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    wheel = download(name, version, arch, Path(tmp), tag)
                    changed += _fuse_from(wheel, files, arch, f"{name} {version}", log)
            except (OSError, RuntimeError, ValueError, KeyError, zipfile.BadZipFile,
                    subprocess.CalledProcessError) as e:
                log(f"warning: could not add {arch} to {name} {version}: {e}")
    return changed


def _fuse_from(wheel: Path, files: list[tuple[Path, str]], arch: str, label: str,
               log) -> list[Path]:
    changed = []
    with zipfile.ZipFile(wheel) as z:
        for p, rel in files:
            if arch in (archs_of(p) or ()):
                continue
            other = thin_slice(z.read(rel), arch)
            if other is None:
                raise RuntimeError(f"{rel} in {wheel.name} has no {arch} slice")
            mine = p.read_bytes()
            parts = [thin_slice(mine, a) for a in (archs_of(p) or ())]
            mode = p.stat().st_mode
            p.write_bytes(fat([s for s in parts if s] + [other]))
            os.chmod(p, mode)
            changed.append(p)
            log(f"fused {rel} ({label}): + {arch}")
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="macho.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report").add_argument("path", type=Path)
    sub.add_parser("check").add_argument("path", type=Path)
    sub.add_parser("sign-order").add_argument("app", type=Path)
    f = sub.add_parser("fuse")
    f.add_argument("--site", type=Path, default=Path(sysconfig.get_paths()["platlib"]))
    a = ap.parse_args(argv)

    if a.cmd == "report":
        counts: dict[tuple[str, ...], int] = {}
        for p, archs in binaries(a.path):
            counts[archs] = counts.get(archs, 0) + 1
        for archs, n in sorted(counts.items()):
            print(f"{n:6d}  {' + '.join(archs)}")
        return 0
    if a.cmd == "check":
        bad = lacking(a.path)
        total = sum(1 for _ in binaries(a.path))
        for p, archs in bad:
            print(f"only {' + '.join(archs)}: {p}")
        if bad:
            print(f"{len(bad)} of {total} binaries do not run on both Intel and Apple Silicon.")
            return 1
        print(f"all {total} binaries carry arm64 and x86_64.")
        return 0
    if a.cmd == "sign-order":
        for kind, p in sign_order(a.app):
            print(f"{kind}\t{p}")
        return 0
    changed = fuse(a.site)
    print(f"{len(changed)} binaries made universal." if changed else "nothing to fuse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
