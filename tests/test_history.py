"""Unit tests for run-history discovery and run.json issue/session linkage."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from acp_factory.cli import history_cmd
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


def test_history_cmd_json_respects_limit(tmp_path: Path) -> None:
    runs_dir = tmp_path / ".factory" / "runs"
    _write_run(runs_dir, "run-a", "2026-11-13T16:00:00+00:00")
    _write_run(runs_dir, "run-b", "2026-11-13T17:00:00+00:00", stage="review")

    out = StringIO()
    with patch(
        "acp_factory.cli.console",
        new=Console(file=out, force_terminal=False, width=200),
    ):
        history_cmd(workspace=tmp_path, limit=1, json_output=True)

    data = json.loads(out.getvalue())
    assert [r["run_id"] for r in data] == ["run-b"]


def test_history_cmd_table_renders(tmp_path: Path) -> None:
    runs_dir = tmp_path / ".factory" / "runs"
    _write_run(
        runs_dir,
        "run-a",
        "2026-11-13T16:00:00+00:00",
        issue_number=7,
        repo_slug="acme/core",
    )

    out = StringIO()
    with patch(
        "acp_factory.cli.console",
        new=Console(file=out, force_terminal=False, width=200),
    ):
        history_cmd(workspace=tmp_path, limit=10, json_output=False)

    text = out.getvalue()
    assert "acp-factory Run History" in text
    assert "#7" in text
    assert "acme/core" in text
