"""Path resolution for the desktop app — dev checkout vs frozen bundle.

The one property that must hold: a packaged, read-only .app writes its database somewhere
writable (Application Support) and reads the corpus from the bundle, while a checkout keeps
everything in the tree — and every path stays overridable by an environment variable, so a
test never touches the developer's real data directory.
"""

from __future__ import annotations

from pathlib import Path

from safety_explorer import paths


def test_overrides_win_and_the_data_dir_is_created(tmp_path, monkeypatch):
    d = tmp_path / "appdata"
    monkeypatch.setenv("EXPLORER_DATA_DIR", str(d))
    monkeypatch.setenv("EXPLORER_DB", str(d / "custom.db"))
    monkeypatch.setenv("EXPLORER_CORPUS", str(tmp_path / "corpus"))
    assert paths.data_dir() == d and d.is_dir()          # created on request
    assert paths.default_db() == d / "custom.db"
    assert paths.corpus_dir() == tmp_path / "corpus"


def test_a_checkout_keeps_everything_in_the_tree(monkeypatch):
    for var in ("EXPLORER_DATA_DIR", "EXPLORER_DB", "EXPLORER_CORPUS"):
        monkeypatch.delenv(var, raising=False)
    assert not paths.is_bundled()
    root = Path(__file__).resolve().parents[1]
    assert paths.resource_dir() == root
    assert paths.corpus_dir() == root / "corpus"
    assert paths.default_db() == root / "data" / "explorer.db"


def test_a_bundle_writes_outside_itself(tmp_path, monkeypatch):
    """Frozen, the database must land in Application Support, never inside the read-only
    bundle — the difference between an app that launches and one that cannot."""
    for var in ("EXPLORER_DATA_DIR", "EXPLORER_DB", "EXPLORER_CORPUS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))          # keep the test out of the real home
    monkeypatch.setattr(paths, "is_bundled", lambda: True)
    d = paths.data_dir()
    assert d == tmp_path / "Library" / "Application Support" / paths.APP_NAME
    assert d.is_dir() and paths.default_db().parent == d
