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
    """A small mock campaign: enough cells to exercise the analysis, fast enough to run.

    Two intent-focal families plus the autonomy-focal one. Two intent families is the
    minimum, not a convenience: confidence intervals bootstrap over families, so a
    single-family fixture yields nan intervals and the analysis correctly declines to
    claim any effect — which would look like a pipeline bug rather than the honest
    answer it is.

    **The register is pinned flat here**, because these tests measure the CAPABILITY
    model and several of them read `technical_density` — a per-100-word rate that every
    composed stance marker dilutes, since markers add words and no equations. That
    dilution is realistic, and it is an uncontrolled variable for a test asking what
    depth costs. Pinning it controls the confound instead of pretending it is absent;
    `populated_stance` is the fixture for tests that want the register on.
    """
    from safety_explorer.providers.mock import FLAT_STANCE

    provider = get_provider("mock", "mock-1", stance_overrides=FLAT_STANCE)
    cid = runner.create_campaign(conn, "test", provider, corpus, 2)
    runner.execute(conn, cid, corpus, provider, 2,
                   only=["orbital_debris", "impactor_deflection", "control_autonomy"])
    return conn, cid
