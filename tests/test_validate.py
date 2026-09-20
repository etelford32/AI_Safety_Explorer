"""The self-check itself: does it pass a sound instrument and fail a broken one?

A validation suite that only ever passes is decoration. These drive it both ways.
"""

import pytest

from safety_explorer import validate as validate_mod


@pytest.fixture
def checked(populated, corpus):
    conn, cid = populated
    return validate_mod.run(conn, corpus, campaign_id=cid), conn, corpus


def test_a_sound_instrument_passes_every_control_that_can_run(checked):
    report, _conn, _corpus = checked
    assert report.sound, [f"{c.name}: {c.detail}" for c in report.failures]


def test_a_check_that_cannot_run_is_not_counted_as_healthy(checked):
    """The distinction that matters when a database is thin: a warn is missing
    evidence, not evidence of health."""
    report, _conn, _corpus = checked
    for check in report.warnings:
        assert check.verdict == validate_mod.WARN
        assert check.ok, "a warn must not be treated as a failure either"
    assert "not evidence of health" in validate_mod.format_report(report) \
        or not report.warnings


def test_a_broken_control_is_reported_not_raised(populated, corpus, monkeypatch):
    """A suite that dies on the first exception tells you about one problem when it
    could have told you about eight."""
    from safety_explorer import groundtruth as gt

    def explode(*_a, **_k):
        raise RuntimeError("extractor is on fire")

    monkeypatch.setattr(gt, "calibrate", explode)
    conn, cid = populated
    report = validate_mod.run(conn, corpus, campaign_id=cid)

    assert not report.sound
    failed = [c for c in report.failures if "language" in c.name]
    assert failed and "extractor is on fire" in failed[0].detail
    # and the rest of the suite still ran
    assert len(report.checks) > 5


def test_a_wrong_answer_key_fails_the_confidently_wrong_control(populated, corpus,
                                                                monkeypatch):
    """If the scorer stopped penalising wrong numbers, this is the check that says so."""
    from safety_explorer import groundtruth as gt

    monkeypatch.setattr(gt, "score",
                        lambda *a, **k: {"accuracy": 1.0, "graded_accuracy": 1.0})
    conn, cid = populated
    report = validate_mod.run(conn, corpus, campaign_id=cid)
    names = [c.name for c in report.failures]
    assert any("confidently wrong" in n for n in names), names


def test_the_report_names_what_was_expected_on_a_failure(populated, corpus, monkeypatch):
    from safety_explorer import groundtruth as gt

    monkeypatch.setattr(gt, "score",
                        lambda *a, **k: {"accuracy": 1.0, "graded_accuracy": 1.0})
    conn, cid = populated
    text = validate_mod.format_report(validate_mod.run(conn, corpus, campaign_id=cid))
    assert "not to be believed" in text
    assert "expected" in text
