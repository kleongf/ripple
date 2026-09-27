"""Resumable, concurrency-limited Codex batches (M16). Codex is replaced by fakes."""

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ripple.codex import CodexError, CodexResult
from ripple.jobs import BatchStopped, Job, JobRunner, is_quota_error

SCHEMA = {"type": "object"}


class FakeRunner:
    """Answers {"echo": prompt}; can fail on chosen prompts and records concurrency."""

    def __init__(self, fail: dict[str, str] | None = None, delay: float = 0.0) -> None:
        self.fail = fail or {}
        self.delay = delay
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def version(self) -> str:
        return "codex-cli 9.9.9"

    def run(self, prompt: str, schema: dict[str, Any], instructions: str) -> CodexResult:
        with self.lock:
            self.calls.append(prompt)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            if prompt in self.fail:
                raise CodexError(self.fail[prompt])
            return CodexResult({"echo": prompt}, {"input_tokens": 100, "output_tokens": 10})
        finally:
            with self.lock:
                self.active -= 1


def jobs(*prompts: str) -> list[Job]:
    return [Job(key=f"k/{p}", prompt=p, schema=SCHEMA, instructions="i") for p in prompts]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "jobs.duckdb"


def test_runs_every_job_and_keeps_results(db: Path) -> None:
    runner = FakeRunner()
    results = JobRunner(runner, db).run_batch("extract", jobs("a", "b", "c"))
    assert {k: r.data["echo"] for k, r in results.items()} == {"k/a": "a", "k/b": "b", "k/c": "c"}
    assert sorted(runner.calls) == ["a", "b", "c"]


def test_finished_jobs_are_not_run_again(db: Path) -> None:
    first = FakeRunner()
    JobRunner(first, db).run_batch("extract", jobs("a", "b"))
    second = FakeRunner()
    results = JobRunner(second, db).run_batch("extract", jobs("a", "b", "c"))
    assert second.calls == ["c"]
    assert set(results) == {"k/a", "k/b", "k/c"}


def test_changed_input_is_run_again(db: Path) -> None:
    JobRunner(FakeRunner(), db).run_batch("extract", jobs("a"))
    changed = [Job(key="k/a", prompt="a2", schema=SCHEMA, instructions="i")]
    runner = FakeRunner()
    results = JobRunner(runner, db).run_batch("extract", changed)
    assert runner.calls == ["a2"]
    assert results["k/a"].data == {"echo": "a2"}


def test_kinds_are_separate(db: Path) -> None:
    JobRunner(FakeRunner(), db).run_batch("extract", jobs("a"))
    runner = FakeRunner()
    JobRunner(runner, db).run_batch("judge", jobs("a"))
    assert runner.calls == ["a"]


def test_failed_job_is_recorded_and_the_batch_continues(db: Path) -> None:
    runner = FakeRunner(fail={"b": "Codex did not return valid JSON"})
    job_runner = JobRunner(runner, db)
    results = job_runner.run_batch("extract", jobs("a", "b", "c"))
    assert set(results) == {"k/a", "k/c"}
    status = job_runner.status("extract")
    assert status["k/b"] == ("failed", 1)
    # A failed job is retried on the next run.
    retry = FakeRunner()
    JobRunner(retry, db).run_batch("extract", jobs("a", "b", "c"))
    assert retry.calls == ["b"]


def test_quota_error_stops_the_batch(db: Path) -> None:
    runner = FakeRunner(fail={"b": "codex exited with 1: error: usage limit reached"})
    job_runner = JobRunner(runner, db, concurrency=1)
    with pytest.raises(BatchStopped) as info:
        job_runner.run_batch("extract", jobs("a", "b", "c", "d"))
    assert "usage limit" in str(info.value)
    assert runner.calls == ["a", "b"]  # nothing new starts after the stop
    assert job_runner.status("extract")["k/b"][0] == "stopped"
    resumed = FakeRunner()
    JobRunner(resumed, db).run_batch("extract", jobs("a", "b", "c", "d"))
    assert sorted(resumed.calls) == ["b", "c", "d"]


def test_at_most_four_calls_at_once(db: Path) -> None:
    runner = FakeRunner(delay=0.05)
    JobRunner(runner, db).run_batch("extract", jobs(*"abcdefghij"))
    assert runner.max_active == 4


def test_usage_and_version_are_logged(db: Path) -> None:
    job_runner = JobRunner(FakeRunner(), db)
    job_runner.run_batch("extract", jobs("a", "b"))
    usage = job_runner.usage()
    assert usage["extract"] == {"jobs": 2, "input_tokens": 200, "output_tokens": 20}
    assert job_runner.versions() == {"codex-cli 9.9.9"}


def test_quota_error_detection() -> None:
    assert is_quota_error("error: usage limit reached, try again in 3 hours")
    assert is_quota_error("HTTP 429 Too Many Requests")
    assert is_quota_error("rate limit exceeded")
    assert not is_quota_error("Codex did not return valid JSON")


def test_version_warning_when_codex_changes(db: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.WARNING)
    JobRunner(FakeRunner(), db, tested_version="codex-cli 0.155.0").run_batch("extract", jobs("a"))
    assert "codex-cli 9.9.9" in caplog.text and "0.155.0" in caplog.text


def test_usage_command(db: Path) -> None:
    from typer.testing import CliRunner

    from ripple.cli import app

    job_runner = JobRunner(FakeRunner(), db)
    job_runner.run_batch("extract", jobs("a", "b"))
    job_runner.close()
    result = CliRunner().invoke(app, ["usage", "--jobs", str(db)])
    assert result.exit_code == 0, result.output
    assert "extract" in result.output and "200" in result.output
    assert "codex-cli 9.9.9" in result.output
