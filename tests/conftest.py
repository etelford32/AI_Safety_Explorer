import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

from safety_explorer import corpus as corpus_mod, db, runner
from safety_explorer.providers import get_provider


@pytest.fixture(scope="session")
def corpus():
    return corpus_mod.load(ROOT / "corpus")


@pytest.fixture
def conn(tmp_path, corpus):
    c = db.init_db(tmp_path / "t.db")
    runner.snapshot_corpus(conn=c, corpus=corpus, lint_clean=True)
    return c


@pytest.fixture
def populated(conn, corpus):
    """A small mock campaign: enough cells to exercise the analysis, fast enough to run."""
    provider = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "test", provider, corpus, 2)
    runner.execute(conn, cid, corpus, provider, 2,
                   only=["orbital_debris", "control_autonomy"])
    return conn, cid
