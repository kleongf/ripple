"""Resumable, concurrency-limited batches of Codex calls (Phase 1, M16, D66).

Each job is identified by (kind, key) and remembers a hash of its input. A job already done
with the same input is not run again, so a batch stopped by a rate limit or quota error
resumes where it left off. Every call's token usage and `codex` version are logged in the
jobs table (a DuckDB file of its own, so batches never contend with the main store).
"""

import hashlib
import json
import logging
import re
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import duckdb

from ripple.codex import CodexError, CodexResult

DEFAULT_JOBS = Path("data/jobs.duckdb")
DEFAULT_CONCURRENCY = 4
TESTED_CODEX_VERSION = "codex-cli 0.155.0-alpha.16.4"
QUOTA_PATTERN = re.compile(r"usage limit|rate.?limit|quota|\b429\b|too many requests", re.I)

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    kind VARCHAR NOT NULL,
    key VARCHAR NOT NULL,
    input_hash VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    attempts INTEGER NOT NULL,
    output JSON,
    usage JSON,
    codex_version VARCHAR,
    error VARCHAR,
    updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (kind, key)
);
"""


class Runner(Protocol):
    def run(self, prompt: str, schema: dict[str, Any], instructions: str) -> CodexResult: ...

    def version(self) -> str: ...


class BatchStopped(Exception):
    """A rate-limit or quota error stopped the batch. Run it again later to resume."""


@dataclass(frozen=True)
class Job:
    key: str
    prompt: str
    schema: dict[str, Any]
    instructions: str

    @property
    def input_hash(self) -> str:
        payload = json.dumps([self.prompt, self.schema, self.instructions], sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


def make_codex_job_runner(db: Path = DEFAULT_JOBS, effort: str = "medium") -> "JobRunner":
    from ripple.codex import CodexRunner, default_config

    return JobRunner(CodexRunner(default_config(effort=effort)), db)


def is_quota_error(message: str) -> bool:
    return bool(QUOTA_PATTERN.search(message))


class JobRunner:
    def __init__(
        self,
        runner: Runner,
        db: Path = DEFAULT_JOBS,
        concurrency: int = DEFAULT_CONCURRENCY,
        tested_version: str = TESTED_CODEX_VERSION,
    ) -> None:
        self.runner = runner
        self.concurrency = concurrency
        self.tested_version = tested_version
        db.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(db))
        self._con.execute(SCHEMA)
        self._version: str | None = None

    def run_batch(self, kind: str, jobs: list[Job]) -> dict[str, CodexResult]:
        """Run the jobs not yet done with the same input; return results for all done jobs."""
        results: dict[str, CodexResult] = {}
        todo: list[Job] = []
        done = self._done(kind)
        for job in jobs:
            stored = done.get(job.key)
            if stored is not None and stored[0] == job.input_hash:
                results[job.key] = stored[1]
            else:
                todo.append(job)
        if not todo:
            return results
        version = self._codex_version()
        stop_reason: str | None = None
        queue = list(todo)
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            running: dict[Future[CodexResult], Job] = {}
            while queue or running:
                while queue and stop_reason is None and len(running) < self.concurrency:
                    job = queue.pop(0)
                    running[
                        pool.submit(self.runner.run, job.prompt, job.schema, job.instructions)
                    ] = job
                if not running:
                    break
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in finished:
                    job = running.pop(future)
                    try:
                        result = future.result()
                    except CodexError as exc:
                        quota = is_quota_error(str(exc))
                        self._record(
                            kind, job, "stopped" if quota else "failed", None, version, str(exc)
                        )
                        if quota and stop_reason is None:
                            stop_reason = str(exc)
                        continue
                    self._record(kind, job, "done", result, version, None)
                    results[job.key] = result
        if stop_reason is not None:
            raise BatchStopped(f"{kind} batch stopped: {stop_reason}")
        return results

    def status(self, kind: str) -> dict[str, tuple[str, int]]:
        rows = self._con.execute(
            "SELECT key, status, attempts FROM jobs WHERE kind = ?", [kind]
        ).fetchall()
        return {key: (status, attempts) for key, status, attempts in rows}

    def usage(self) -> dict[str, dict[str, int]]:
        totals: dict[str, dict[str, int]] = {}
        rows = self._con.execute("SELECT kind, usage FROM jobs WHERE status = 'done'").fetchall()
        for kind, usage in rows:
            entry = totals.setdefault(kind, {"jobs": 0, "input_tokens": 0, "output_tokens": 0})
            entry["jobs"] += 1
            used = json.loads(usage) if usage else {}
            entry["input_tokens"] += int(used.get("input_tokens", 0))
            entry["output_tokens"] += int(used.get("output_tokens", 0))
        return totals

    def versions(self) -> set[str]:
        rows = self._con.execute(
            "SELECT DISTINCT codex_version FROM jobs WHERE codex_version IS NOT NULL"
        ).fetchall()
        return {row[0] for row in rows}

    def close(self) -> None:
        self._con.close()

    def _codex_version(self) -> str:
        if self._version is None:
            self._version = self.runner.version()
            if self._version != self.tested_version:
                log.warning(
                    "codex version %s differs from the last tested version %s; check the "
                    "runner's flags still work (D66)",
                    self._version,
                    self.tested_version,
                )
        return self._version

    def _done(self, kind: str) -> dict[str, tuple[str, CodexResult]]:
        rows = self._con.execute(
            "SELECT key, input_hash, output, usage FROM jobs WHERE kind = ? AND status = 'done'",
            [kind],
        ).fetchall()
        return {
            key: (digest, CodexResult(json.loads(output), json.loads(usage) if usage else {}))
            for key, digest, output, usage in rows
        }

    def _record(
        self,
        kind: str,
        job: Job,
        status: str,
        result: CodexResult | None,
        version: str,
        error: str | None,
    ) -> None:
        previous = self._con.execute(
            "SELECT attempts, input_hash FROM jobs WHERE kind = ? AND key = ?", [kind, job.key]
        ).fetchone()
        attempts = 1
        if previous is not None and previous[1] == job.input_hash:
            attempts = previous[0] + 1
        self._con.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                kind,
                job.key,
                job.input_hash,
                status,
                attempts,
                json.dumps(result.data) if result else None,
                json.dumps(result.usage) if result else None,
                version,
                error,
                datetime.now(UTC).replace(tzinfo=None),
            ],
        )
