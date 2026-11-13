"""Unit tests for run-history discovery and run.json issue/session linkage."""

from __future__ import annotations

import json
from pathlib import Path

from acp_factory.config import FactoryConfig
from acp_factory.history import list_run_history
from acp_factory.runner.loop import _persist_run_artifacts


def _write_run(runs_dir: Path, run_id: str, timestamp: str, **extra: object) -> None:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    payload = {
        "run_id": run_id,
        "timestamp": timestamp,
        "stage": "build",
        "succeeded": True,
    }
    payload.update(extra)
    (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")


def test_list_run_history_sorts_newest_first(tmp_path: Path) -> None:
    runs_dir = tmp_path / ".factory" / "runs"
    _write_run(runs_dir, "old", "2026-11-10T10:00:00+00:00")
    _write_run(runs_dir, "new", "2026-11-13T16:00:00+00:00")

    records = list_run_history(tmp_path)
    assert [r["run_id"] for r in records] == ["new", "old"]


def test_list_run_history_skips_latest_and_dry_run(tmp_path: Path) -> None:
    runs_dir = tmp_path / ".factory" / "runs"
    _write_run(runs_dir, "real", "2026-11-13T16:00:00+00:00")
    _write_run(runs_dir, "dry-run", "2026-11-13T17:00:00+00:00")
    # `latest` is normally a symlink to the newest run; simulate a plain dir.
    _write_run(runs_dir, "latest", "2026-11-13T18:00:00+00:00")

    records = list_run_history(tmp_path)
    assert [r["run_id"] for r in records] == ["real"]


def test_list_run_history_ignores_malformed_json(tmp_path: Path) -> None:
    runs_dir = tmp_path / ".factory" / "runs"
    (runs_dir / "broken").mkdir(parents=True)
    (runs_dir / "broken" / "run.json").write_text("{not json", encoding="utf-8")
    _write_run(runs_dir, "ok", "2026-11-13T16:00:00+00:00")

    records = list_run_history(tmp_path)
    assert [r["run_id"] for r in records] == ["ok"]


def test_list_run_history_empty_when_no_runs_dir(tmp_path: Path) -> None:
    assert list_run_history(tmp_path) == []


def test_persist_run_artifacts_records_issue_and_session(tmp_path: Path) -> None:
    (tmp_path / ".factory").mkdir()
    config = FactoryConfig(task="Test task")

    run_dir = _persist_run_artifacts(
        tmp_path,
        "run-1",
        config,
        succeeded=True,
        attempts_used=1,
        total_duration=1.5,
        setup_logs=[],
        turn_transcripts=[],
        check_history=[],
        failure_reason=None,
        issue_number=42,
        issue_title="Fix the thing",
        repo_slug="acme/core",
        branch="factory/issue-42",
        base_ref="origin/main",
        session_name="factory-deadbeef",
    )

    assert run_dir is not None
    data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert data["issue_number"] == 42
    assert data["issue_title"] == "Fix the thing"
    assert data["repo_slug"] == "acme/core"
    assert data["branch"] == "factory/issue-42"
    assert data["base_ref"] == "origin/main"
    assert data["session_name"] == "factory-deadbeef"
