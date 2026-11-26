"""Non-blocking async job store.

Long scripts, model downloads and engine venv installs all return a ``job_id``
immediately and report progress here. Cancellation is cooperative: a job is
handed an ``asyncio.Event`` it must poll between steps.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from .clock import Clock, get_clock
from .schemas import ErrorCode, ErrorResponse, Job, JobStatus

JobKind = Literal["script", "download_model", "install_engine"]


class JobStore:
    def __init__(self, jobs_dir: Path, clock: Clock | None = None) -> None:
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock or get_clock()
        self._jobs: dict[str, Job] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    # -- persistence ----------------------------------------------------
    def _path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _persist(self, job: Job) -> None:
        path = self._path(job.job_id)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(job.model_dump(mode="json"), indent=2), "utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _load(self, job_id: str) -> Job | None:
        if job_id in self._jobs:
            return self._jobs[job_id]
        path = self._path(job_id)
        if not path.is_file():
            return None
        try:
            job = Job(**json.loads(path.read_text("utf-8")))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        self._jobs[job_id] = job
        return job

    # -- lifecycle ------------------------------------------------------
    def create(self, kind: JobKind) -> Job:
        now = self._clock.now()
        job = Job(
            job_id=uuid.uuid4().hex[:16],
            kind=kind,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        self._jobs[job.job_id] = job
        self._cancel_events[job.job_id] = asyncio.Event()
        self._persist(job)
        return job

    def cancel_event(self, job_id: str) -> asyncio.Event:
        return self._cancel_events.setdefault(job_id, asyncio.Event())

    def get(self, job_id: str) -> Job:
        from .schemas import AgentVoiceError

        job = self._load(job_id)
        if job is None:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"unknown job: {job_id}",
                "Jobs are ephemeral; check the job_id returned when you started it.",
            )
        return job

    def update(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        progress: float | None = None,
        message: str | None = None,
        result: dict[str, Any] | None = None,
        error: ErrorResponse | None = None,
    ) -> Job:
        job = self.get(job_id)
        if status is not None:
            job.status = status
        if progress is not None:
            job.progress = max(0.0, min(1.0, progress))
        if message is not None:
            job.message = message
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        job.updated_at = self._clock.now()
        self._persist(job)
        return job

    def succeeded(self, job_id: str, result: dict[str, Any]) -> Job:
        return self.update(job_id, status="succeeded", progress=1.0, result=result)

    def failed(self, job_id: str, error: ErrorResponse) -> Job:
        return self.update(job_id, status="failed", error=error)

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status in {"succeeded", "failed", "cancelled"}:
            return job
        self.cancel_event(job_id).set()
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        return self.update(job_id, status="cancelled", message="cancelled by request")

    # -- task registry --------------------------------------------------
    def register_task(self, job_id: str, task: asyncio.Task[Any]) -> None:
        self._tasks[job_id] = task

    def mark_running(self, job_id: str, message: str = "") -> Job:
        return self.update(job_id, status="running", message=message, progress=0.01)

    def list(self, *, limit: int = 50) -> list[Job]:
        jobs = list(self._jobs.values())
        for path in sorted(self.jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            job_id = path.stem
            if job_id not in self._jobs:
                job = self._load(job_id)
                if job is not None:
                    jobs.append(job)
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def prune(self, *, older_than_seconds: float = 24 * 3600) -> int:
        cutoff = time.time() - older_than_seconds
        removed = 0
        for path in self.jobs_dir.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    self._jobs.pop(path.stem, None)
                    removed += 1
            except OSError:
                continue
        return removed
