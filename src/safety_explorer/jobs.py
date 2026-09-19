"""Background job execution for the UI.

A campaign is 246 model calls; it cannot run inside an HTTP request. So the UI starts
a job on a worker thread and polls its progress.

Deliberately single-slot: one job at a time, no queue. Two concurrent campaigns writing
to the same SQLite file would contend for the write lock and, worse, would make the
progress display ambiguous. A research instrument that quietly runs two experiments at
once is a bug generator, not a feature.
"""

from __future__ import annotations

import threading
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable


class Job:
    """One background job, with cooperative cancellation and a bounded log."""

    def __init__(self, kind: str, label: str) -> None:
        self.kind = kind
        self.label = label
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.finished_at: str | None = None
        self.state = "running"          # running | done | error | cancelled
        self.done = 0
        self.total = 0
        self.ok = 0
        self.errors = 0
        self.skipped = 0
        self.current: str | None = None
        self.error: str | None = None
        self.result: dict[str, Any] | None = None
        self.log: deque[str] = deque(maxlen=200)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()
        self.log.append("cancellation requested — finishing the current cell")

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def note(self, message: str) -> None:
        self.log.append(message)

    def snapshot(self) -> dict[str, Any]:
        pct = round(100 * self.done / self.total, 1) if self.total else 0.0
        return {
            "kind": self.kind, "label": self.label, "state": self.state,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "done": self.done, "total": self.total, "percent": pct,
            "ok": self.ok, "errors": self.errors, "skipped": self.skipped,
            "current": self.current, "error": self.error, "result": self.result,
            "log": list(self.log)[-40:],
            "cancelled": self.cancelled,
        }


class JobRunner:
    """Holds the single active job and the last finished one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active: Job | None = None
        self.last: Job | None = None

    def busy(self) -> bool:
        with self._lock:
            return self.active is not None and self.active.state == "running"

    def start(self, kind: str, label: str, target: Callable[[Job], Any]) -> Job:
        with self._lock:
            if self.active is not None and self.active.state == "running":
                raise RuntimeError(
                    f"a {self.active.kind} job is already running — "
                    f"cancel it or wait for it to finish"
                )
            job = Job(kind, label)
            self.active = job

        def wrapper() -> None:
            try:
                job.result = target(job)
                job.state = "cancelled" if job.cancelled else "done"
            except Exception as exc:  # noqa: BLE001 — surfaced to the UI, not swallowed
                job.state = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.note(job.error)
                traceback.print_exc()
            finally:
                job.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                with self._lock:
                    self.last = job
                    self.active = None

        threading.Thread(target=wrapper, daemon=True, name=f"job-{kind}").start()
        return job

    def status(self) -> dict[str, Any]:
        with self._lock:
            job = self.active or self.last
        return job.snapshot() if job else {"state": "idle"}

    def cancel(self) -> bool:
        with self._lock:
            job = self.active
        if job is None or job.state != "running":
            return False
        job.cancel()
        return True


RUNNER = JobRunner()
