"""Background job execution for the UI."""

import threading
import time

import pytest

from safety_explorer.jobs import JobRunner


def _wait(runner: JobRunner, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while runner.busy() and time.time() < deadline:
        time.sleep(0.01)


def test_job_runs_and_reports_result():
    r = JobRunner()
    r.start("test", "x", lambda job: {"answer": 42})
    _wait(r)
    s = r.status()
    assert s["state"] == "done" and s["result"] == {"answer": 42}


def test_only_one_job_at_a_time():
    """Two campaigns writing the same SQLite file would contend and make progress ambiguous."""
    r = JobRunner()
    gate = threading.Event()
    r.start("test", "first", lambda job: gate.wait(5))
    with pytest.raises(RuntimeError, match="already running"):
        r.start("test", "second", lambda job: None)
    gate.set()
    _wait(r)


def test_cancellation_is_cooperative():
    r = JobRunner()
    seen = []

    def work(job):
        for i in range(1000):
            if job.cancelled:
                return {"stopped_at": i}
            seen.append(i)
            time.sleep(0.002)
        return {"stopped_at": None}

    r.start("test", "x", work)
    time.sleep(0.05)
    assert r.cancel() is True
    _wait(r)
    s = r.status()
    assert s["state"] == "cancelled"
    assert s["result"]["stopped_at"] is not None, "job ignored cancellation"


def test_failure_is_surfaced_not_swallowed():
    r = JobRunner()
    r.start("test", "x", lambda job: 1 / 0)
    _wait(r)
    s = r.status()
    assert s["state"] == "error" and "ZeroDivisionError" in s["error"]


def test_cancel_when_idle_is_a_no_op():
    assert JobRunner().cancel() is False


def test_log_is_bounded():
    """A long campaign must not grow an unbounded in-memory log."""
    r = JobRunner()

    def work(job):
        for i in range(500):
            job.note(f"line {i}")
        return None

    r.start("test", "x", work)
    _wait(r)
    assert len(r.status()["log"]) <= 40


def test_runner_execute_honours_should_stop(conn, corpus):
    from safety_explorer import db, runner
    from safety_explorer.providers import get_provider

    provider = get_provider("mock", "mock-1")
    cid = runner.create_campaign(conn, "stoppable", provider, corpus, 1)
    calls = {"n": 0}

    def stop_after_three() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    stats = runner.execute(conn, cid, corpus, provider, 1,
                           only=["orbital_debris"], should_stop=stop_after_three)
    assert stats["cancelled"] is True
    stored = db.query_one(conn, "SELECT COUNT(*) AS n FROM run WHERE campaign_id = ?", (cid,))
    # Cancellation is checked between cells, so completed work is always written.
    assert 0 < stored["n"] < 9
