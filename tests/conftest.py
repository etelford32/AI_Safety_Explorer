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

    **Both registers are pinned flat here** — stance AND agency — because these tests
    measure the CAPABILITY model and several of them read `technical_density`, a per-100-word
    rate that every composed marker dilutes, since decorations add words and no equations.
    That dilution is realistic, and it is an uncontrolled variable for a test asking what
    depth costs. Pinning controls the confound instead of pretending it is absent; a test
    that wants a register on builds its own provider without the flat override.
    """
    from safety_explorer.providers.mock import FLAT_POWER, FLAT_STANCE

    provider = get_provider("mock", "mock-1", stance_overrides=FLAT_STANCE,
                            power_overrides=FLAT_POWER)
    cid = runner.create_campaign(conn, "test", provider, corpus, 2)
    runner.execute(conn, cid, corpus, provider, 2,
                   only=["orbital_debris", "impactor_deflection", "control_autonomy"])
    return conn, cid


@pytest.fixture
def populated_power(conn, corpus):
    """A campaign with the AGENCY register ON, for the power-seeking analytics.

    The register is left on precisely because these tests measure it — the expressed-vs-
    granted reach and the alarming-benign null control. It spans the two autonomy families
    (which carry a range of granted autonomy), an intent family (for benign baselines at
    various grants), and the alarming-benign control arm (the null control's alarming pool).
    """
    provider = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "power", provider, corpus, 3)
    runner.execute(conn, cid, corpus, provider, 3,
                   only=["physiological_limits", "control_autonomy", "orbital_debris",
                         "alarming_benign"])
    return conn, cid, corpus
