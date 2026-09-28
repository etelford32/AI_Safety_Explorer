"""The app's packaging helpers: Mach-O architectures, fusing thin wheels, and the sign order.

These run on any OS against synthetic Mach-O files. On macOS, CI also checks the real app
with `lipo` and runs its x86_64 slice under Rosetta (.github/workflows/app.yml).
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import io
import os
import struct
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("macho", ROOT / "packaging" / "macho.py")
macho = importlib.util.module_from_spec(spec)
sys.modules["macho"] = macho
spec.loader.exec_module(macho)

ARM, X86 = 0x0100000C, 0x01000007


def thin(cputype: int, filetype: int = 8, body: bytes = b"") -> bytes:
    """A minimal thin 64-bit Mach-O: header, then an arbitrary body standing in for code."""
    sub = 0 if cputype == ARM else 3
    return macho.MH_MAGIC_64 + struct.pack("<IIIIIII", cputype, sub, filetype, 0, 0, 0, 0) + (
        body or bytes(range(256)) * 4)


# -- reading architectures --------------------------------------------------------------------

def test_thin_and_universal_files_are_read(tmp_path):
    (tmp_path / "a.so").write_bytes(thin(ARM))
    (tmp_path / "b.so").write_bytes(macho.fat([thin(ARM), thin(X86)]))
    (tmp_path / "c.txt").write_text("not a binary")
    assert macho.archs_of(tmp_path / "a.so") == ("arm64",)
    assert macho.archs_of(tmp_path / "b.so") == ("arm64", "x86_64")
    assert macho.archs_of(tmp_path / "c.txt") is None


def test_a_java_class_file_is_not_mistaken_for_a_universal_binary(tmp_path):
    # Same magic (CAFEBABE); what follows is a class-file version, not a slice count.
    (tmp_path / "A.class").write_bytes(b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\0" * 64)
    assert macho.archs_of(tmp_path / "A.class") is None


def test_lacking_reports_thin_binaries_and_skips_symlinks(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "thin.so").write_bytes(thin(ARM))
    (tmp_path / "lib" / "fat.so").write_bytes(macho.fat([thin(ARM), thin(X86)]))
    os.symlink(tmp_path / "lib" / "thin.so", tmp_path / "link.so")
    found = macho.lacking(tmp_path)
    assert [(p.name, a) for p, a in found] == [("thin.so", ("arm64",))]
    assert macho.main(["check", str(tmp_path)]) == 1
    (tmp_path / "lib" / "thin.so").write_bytes(macho.fat([thin(ARM), thin(X86)]))
    assert macho.main(["check", str(tmp_path)]) == 0


# -- writing universal binaries ---------------------------------------------------------------

def test_fat_lays_out_slices_as_lipo_does():
    arm, x86 = thin(ARM, body=b"A" * 5000), thin(X86, body=b"X" * 7000)
    data = macho.fat([arm, x86])                       # given arm64 first
    found = macho.slices(data)
    assert [c for c, *_ in found] == [X86, ARM]         # written x86_64 first
    assert [o % (1 << 14) for _, _, o, _ in found] == [0, 0]
    assert found[0][2] == 1 << 14                       # first slice after the header page
    assert macho.thin_slice(data, "arm64") == arm       # byte for byte
    assert macho.thin_slice(data, "x86_64") == x86
    assert [s for _, s, _, _ in found] == [3, 0]        # subtypes copied from each slice
    assert struct.unpack(">I", data[8 + 16: 8 + 20])[0] == 14   # align field


def test_fat_refuses_what_it_cannot_combine():
    with pytest.raises(ValueError):
        macho.fat([thin(ARM), thin(ARM)])
    with pytest.raises(ValueError):
        macho.fat([macho.fat([thin(ARM), thin(X86)])])
    with pytest.raises(ValueError):
        macho.fat([b"plain bytes"])


# -- fusing a build environment ---------------------------------------------------------------

def make_site(tmp_path: Path, files: dict[str, bytes], name="fastlib", version="1.2.3",
              tag="cp312-cp312-macosx_11_0_arm64") -> Path:
    site = tmp_path / "site"
    info = site / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    (info / "WHEEL").write_text(f"Wheel-Version: 1.0\nTag: {tag}\n")
    record = []
    for rel, data in files.items():
        (site / rel).parent.mkdir(parents=True, exist_ok=True)
        (site / rel).write_bytes(data)
        os.chmod(site / rel, 0o755)
        record.append(f"{rel},,")
    record.append(f"{info.name}/RECORD,,")
    (info / "RECORD").write_text("\n".join(record) + "\n")
    return site


def fake_wheel(files: dict[str, bytes]):
    calls = []

    def download(name, version, arch, dest, tag=None):
        calls.append((name, version, arch, tag))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for rel, data in files.items():
                z.writestr(rel, data)
        wheel = dest / f"{name}-{version}-x.whl"
        wheel.write_bytes(buf.getvalue())
        return wheel
    download.calls = calls
    return download


def test_fuse_adds_the_missing_slice_from_the_same_versions_wheel(tmp_path):
    arm, x86 = thin(ARM, body=b"a" * 3000), thin(X86, body=b"x" * 3000)
    site = make_site(tmp_path, {"fastlib/_core.cpython-312-darwin.so": arm,
                                "fastlib/__init__.py": b""})
    dl = fake_wheel({"fastlib/_core.cpython-312-darwin.so": x86})
    changed = macho.fuse(site, download=dl, log=lambda *_: None)
    so = site / "fastlib" / "_core.cpython-312-darwin.so"
    assert changed == [so.resolve()]
    assert macho.archs_of(so) == ("arm64", "x86_64")
    assert macho.thin_slice(so.read_bytes(), "arm64") == arm
    assert macho.thin_slice(so.read_bytes(), "x86_64") == x86
    assert os.stat(so).st_mode & 0o777 == 0o755
    # The wheel asked for is the same build: name, version, missing arch, interpreter and abi.
    assert dl.calls == [("fastlib", "1.2.3", "x86_64", ("3.12", "cp312"))]
    # Already universal: nothing to do, nothing downloaded.
    assert macho.fuse(site, download=dl, log=lambda *_: None) == []
    assert len(dl.calls) == 1


def test_fuse_says_what_it_could_not_fix_and_leaves_it_for_the_check(tmp_path):
    site = make_site(tmp_path, {"fastlib/_core.so": thin(ARM)})
    (site / "stray.so").write_bytes(thin(ARM))           # owned by no distribution

    def offline(*_a, **_k):
        raise OSError("no network")
    said = []
    assert macho.fuse(site, download=offline, log=said.append) == []
    assert any("no installed distribution owns" in s and "stray.so" in s for s in said)
    assert any("could not add x86_64 to fastlib 1.2.3" in s for s in said)
    assert macho.archs_of(site / "fastlib" / "_core.so") == ("arm64",)


def test_fuse_refuses_a_wheel_whose_file_is_the_wrong_architecture(tmp_path):
    site = make_site(tmp_path, {"fastlib/_core.so": thin(ARM)})
    said = []
    dl = fake_wheel({"fastlib/_core.so": thin(ARM)})      # "x86_64" wheel with arm64 inside
    assert macho.fuse(site, download=dl, log=said.append) == []
    assert any("has no x86_64 slice" in s for s in said)
    assert macho.archs_of(site / "fastlib" / "_core.so") == ("arm64",)


def test_the_wheel_tag_comes_from_the_installed_metadata(tmp_path):
    site = make_site(tmp_path, {"x/_a.so": thin(ARM)}, tag="cp311-abi3-macosx_11_0_arm64")
    dist = next(iter(importlib.metadata.distributions(path=[str(site)])))
    assert macho.wheel_tag(dist) == ("3.11", "abi3")


# -- what codesign signs, and in what order ---------------------------------------------------

def test_sign_order_is_inside_out_and_marks_executables(tmp_path):
    app = tmp_path / "X.app"
    exe = app / "Contents" / "MacOS" / "X"
    fw = app / "Contents" / "Frameworks" / "Python.framework"
    lib = fw / "Versions" / "3.12" / "Python"
    ext = app / "Contents" / "Resources" / "lib" / "python3.12" / "lib-dynload" / "m" / "m.so"
    for p, data in ((exe, thin(ARM, filetype=2)), (lib, thin(ARM, filetype=6)),
                    (ext, macho.fat([thin(ARM), thin(X86)]))):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    os.symlink("3.12", fw / "Versions" / "Current")
    order = [(k, p.relative_to(app).as_posix()) for k, p in macho.sign_order(app)]
    assert ("exe", "Contents/MacOS/X") in order
    assert ("lib", "Contents/Resources/lib/python3.12/lib-dynload/m/m.so") in order
    names = [p for _, p in order]
    # The framework's binary before the framework version it belongs to; the symlink never.
    assert names.index("Contents/Frameworks/Python.framework/Versions/3.12/Python") < \
        names.index("Contents/Frameworks/Python.framework/Versions/3.12")
    assert not any(p.endswith("Versions/Current") for p in names)
    assert "." not in names and "" not in names             # never the app itself
