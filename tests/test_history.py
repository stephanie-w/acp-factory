"""Unit tests for run-history discovery and run.json issue/session linkage."""

from __future__ import annotations

import json
import re
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from acp_factory.cli import history_cmd
from acp_factory.config import FactoryConfig, FleetConfig, resolve_runs_dir
from acp_factory.history import list_fleet_run_history, list_run_history
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


def _history_columns(text: str) -> dict[str, list[str]]:
    """Map each rendered column header to its ordered cell values."""
    rows = [
        line.strip().strip("│┃")
        for line in text.splitlines()
        if "│" in line or "┃" in line
    ]
    header, *data = ([cell.strip() for cell in re.split("[│┃]", row)] for row in rows)
    return {name: [row[index] for row in data] for index, name in enumerate(header)}


def _write_fleet_config(path: Path, repositories: dict[str, str]) -> Path:
    lines = ["[repositories]"]
    lines.extend(f'"{slug}" = "{checkout}"' for slug, checkout in repositories.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_repo_local_fleet(workspace: Path, repositories: dict[str, str]) -> None:
    factory_dir = workspace / ".factory"
    factory_dir.mkdir(parents=True, exist_ok=True)
    lines = ["[fleet.repositories]"]
    lines.extend(f'"{slug}" = "{checkout}"' for slug, checkout in repositories.items())
    (factory_dir / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fleet_config(repositories: dict[str, str]) -> FactoryConfig:
    return FactoryConfig(fleet=FleetConfig(repositories=repositories))


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
        session_id="ses_deadbeef",
    )

    assert run_dir is not None
    data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert data["issue_number"] == 42
    assert data["issue_title"] == "Fix the thing"
    assert data["repo_slug"] == "acme/core"
    assert data["branch"] == "factory/issue-42"
    assert data["base_ref"] == "origin/main"
    assert data["session_id"] == "ses_deadbeef"


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
    assert "run-a" in text
    assert "#7" in text
    assert "acme/core" in text


@pytest.mark.parametrize(
    ("run_id", "extra", "expected_model", "expected_attempts"),
    [
        (
            "run-a",
            {"model": "claude-3-5-sonnet", "attempts_used": 2, "max_attempts": 3},
            "claude-3-5-sonnet",
            "2/3",
        ),
        ("legacy", {}, "-", "-"),
        ("partial", {"attempts_used": 9}, "-", "9"),
        (
            "booly",
            {"model": "", "attempts_used": True, "max_attempts": False},
            "-",
            "-",
        ),
    ],
    ids=[
        "model_and_attempts",
        "legacy_missing_fields",
        "attempts_without_max",
        "rejects_boolean_attempts",
    ],
)
def test_history_cmd_table_columns(
    tmp_path: Path,
    run_id: str,
    extra: dict[str, object],
    expected_model: str,
    expected_attempts: str,
) -> None:
    runs_dir = tmp_path / ".factory" / "runs"
    _write_run(runs_dir, run_id, "2026-11-13T16:00:00+00:00", **extra)

    out = StringIO()
    with patch(
        "acp_factory.cli.console",
        new=Console(file=out, force_terminal=False, width=200),
    ):
        history_cmd(workspace=tmp_path, limit=10, json_output=False)

    columns = _history_columns(out.getvalue())
    assert columns["Model"] == [expected_model]
    assert columns["Attempts"] == [expected_attempts]


def test_list_fleet_run_history_merges_sorts_and_tags(tmp_path: Path) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _write_run(repo_a / ".factory" / "runs", "old", "2026-11-10T10:00:00+00:00")
    _write_run(
        repo_b / ".factory" / "runs",
        "new",
        "2026-11-13T16:00:00+00:00",
        repo_slug="provided/slug",
    )
    _write_run(
        repo_a / ".factory" / "runs",
        "null-slug",
        "2026-11-11T10:00:00+00:00",
        repo_slug=None,
    )

    records = list_fleet_run_history(
        _fleet_config({"demo/repo-a": str(repo_a), "demo/repo-b": str(repo_b)})
    )

    assert [r["run_id"] for r in records] == ["new", "null-slug", "old"]
    by_id = {r["run_id"]: r for r in records}
    assert by_id["old"]["repo_slug"] == "demo/repo-a"
    assert by_id["null-slug"]["repo_slug"] == "demo/repo-a"
    assert by_id["new"]["repo_slug"] == "provided/slug"


def test_list_fleet_run_history_error_isolation(tmp_path: Path) -> None:
    good = tmp_path / "good"
    _write_run(good / ".factory" / "runs", "ok", "2026-11-13T16:00:00+00:00")
    broken = tmp_path / "broken"
    (broken / ".factory" / "runs" / "bad").mkdir(parents=True)
    (broken / ".factory" / "runs" / "bad" / "run.json").write_text(
        "{not json", encoding="utf-8"
    )

    records = list_fleet_run_history(
        _fleet_config(
            {
                "demo/good": str(good),
                "demo/missing": str(tmp_path / "does-not-exist"),
                "demo/broken": str(broken),
                "not-a-valid-slug": str(good),
            }
        )
    )

    assert [r["run_id"] for r in records] == ["ok"]
    assert records[0]["repo_slug"] == "demo/good"


def test_list_fleet_run_history_empty_config() -> None:
    assert list_fleet_run_history(FactoryConfig()) == []
    assert list_fleet_run_history(_fleet_config({})) == []


def test_history_cmd_fleet_table(tmp_path: Path) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _write_run(repo_a / ".factory" / "runs", "run-a", "2026-11-13T16:00:00+00:00")
    _write_run(repo_b / ".factory" / "runs", "run-b", "2026-11-13T17:00:00+00:00")
    fleet_file = _write_fleet_config(
        tmp_path / "fleet.toml",
        {"demo/repo-a": str(repo_a), "demo/repo-b": str(repo_b)},
    )

    out = StringIO()
    with patch(
        "acp_factory.cli.console",
        new=Console(file=out, force_terminal=False, width=200),
    ):
        history_cmd(
            workspace=tmp_path,
            fleet_config_file=fleet_file,
            limit=10,
            json_output=False,
        )

    columns = _history_columns(out.getvalue())
    assert columns["Run ID"] == ["run-b", "run-a"]
    assert columns["Repo"] == ["demo/repo-b", "demo/repo-a"]


def test_history_cmd_fleet_config_override(tmp_path: Path) -> None:
    repo_local = tmp_path / "repo-local"
    _write_run(
        repo_local / ".factory" / "runs", "local-run", "2026-11-13T16:00:00+00:00"
    )
    _write_repo_local_fleet(tmp_path, {"demo/local": str(repo_local)})

    repo_override = tmp_path / "repo-override"
    _write_run(
        repo_override / ".factory" / "runs",
        "override-run",
        "2026-11-13T17:00:00+00:00",
    )
    fleet_file = _write_fleet_config(
        tmp_path / "fleet.toml", {"demo/override": str(repo_override)}
    )

    preserved = StringIO()
    with patch(
        "acp_factory.cli.console",
        new=Console(file=preserved, force_terminal=False, width=200),
    ):
        history_cmd(workspace=tmp_path, limit=10, json_output=True)

    preserved_data = json.loads(preserved.getvalue())
    assert [r["run_id"] for r in preserved_data] == ["local-run"]
    assert preserved_data[0]["repo_slug"] == "demo/local"

    overridden = StringIO()
    with patch(
        "acp_factory.cli.console",
        new=Console(file=overridden, force_terminal=False, width=200),
    ):
        history_cmd(
            workspace=tmp_path,
            fleet_config_file=fleet_file,
            limit=10,
            json_output=True,
        )

    overridden_data = json.loads(overridden.getvalue())
    assert [r["run_id"] for r in overridden_data] == ["override-run"]
    assert overridden_data[0]["repo_slug"] == "demo/override"


def test_history_cmd_no_fleet_overrides_fleet_config(tmp_path: Path) -> None:
    _write_run(tmp_path / ".factory" / "runs", "local-run", "2026-11-13T16:00:00+00:00")
    repo_fleet = tmp_path / "repo-fleet"
    _write_run(
        repo_fleet / ".factory" / "runs", "fleet-run", "2026-11-13T17:00:00+00:00"
    )
    fleet_file = _write_fleet_config(
        tmp_path / "fleet.toml", {"demo/fleet": str(repo_fleet)}
    )

    out = StringIO()
    with patch(
        "acp_factory.cli.console",
        new=Console(file=out, force_terminal=False, width=200),
    ):
        history_cmd(
            workspace=tmp_path,
            fleet=False,
            fleet_config_file=fleet_file,
            limit=10,
            json_output=True,
        )

    data = json.loads(out.getvalue())
    assert [r["run_id"] for r in data] == ["local-run"]


def test_history_cmd_fleet_json(tmp_path: Path) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _write_run(repo_a / ".factory" / "runs", "run-a", "2026-11-13T16:00:00+00:00")
    _write_run(repo_b / ".factory" / "runs", "run-b", "2026-11-13T17:00:00+00:00")
    fleet_file = _write_fleet_config(
        tmp_path / "fleet.toml",
        {"demo/repo-a": str(repo_a), "demo/repo-b": str(repo_b)},
    )

    out = StringIO()
    with patch(
        "acp_factory.cli.console",
        new=Console(file=out, force_terminal=False, width=200),
    ):
        history_cmd(
            workspace=tmp_path,
            fleet_config_file=fleet_file,
            limit=10,
            json_output=True,
        )

    data = json.loads(out.getvalue())
    assert [r["run_id"] for r in data] == ["run-b", "run-a"]
    assert {r["repo_slug"] for r in data} == {"demo/repo-a", "demo/repo-b"}


def test_list_fleet_run_history_limit(tmp_path: Path) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _write_run(repo_a / ".factory" / "runs", "a-old", "2026-11-13T10:00:00+00:00")
    _write_run(repo_a / ".factory" / "runs", "a-new", "2026-11-13T12:00:00+00:00")
    _write_run(repo_b / ".factory" / "runs", "b-mid", "2026-11-13T11:00:00+00:00")
    fleet_file = _write_fleet_config(
        tmp_path / "fleet.toml",
        {"demo/repo-a": str(repo_a), "demo/repo-b": str(repo_b)},
    )

    out = StringIO()
    with patch(
        "acp_factory.cli.console",
        new=Console(file=out, force_terminal=False, width=200),
    ):
        history_cmd(
            workspace=tmp_path,
            fleet_config_file=fleet_file,
            limit=2,
            json_output=True,
        )

    data = json.loads(out.getvalue())
    assert [r["run_id"] for r in data] == ["a-new", "b-mid"]


def test_list_run_history_reads_resolved_runs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: the resolved runs_dir is read even without a legacy `.factory/runs/`."""
    external = tmp_path / "external-runs"
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(external))
    _write_run(external, "resolved", "2026-11-13T16:00:00+00:00", stage="review")

    records = list_run_history(tmp_path)

    assert [r["run_id"] for r in records] == ["resolved"]
    assert not (tmp_path / ".factory" / "runs").exists()


def test_list_run_history_merges_resolved_and_legacy_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: legacy runs still surface, and the resolved copy wins run_id collisions."""
    external = tmp_path / "external-runs"
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(external))
    legacy = tmp_path / ".factory" / "runs"
    _write_run(external, "shared", "2026-11-13T10:00:00+00:00", stage="review")
    _write_run(legacy, "shared", "2026-11-13T10:00:00+00:00", stage="build")
    _write_run(legacy, "legacy-only", "2026-11-13T12:00:00+00:00", stage="triage")

    records = list_run_history(tmp_path)

    assert [r["run_id"] for r in records] == ["legacy-only", "shared"]
    by_id = {r["run_id"]: r for r in records}
    assert by_id["shared"]["stage"] == "review"


def test_list_fleet_run_history_includes_resolved_and_legacy(
    tmp_path: Path,
) -> None:
    """AC-5: fleet aggregation spans resolved runs_dir and legacy `.factory/runs/`."""
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    _write_run(resolve_runs_dir(repo_a), "resolved", "2026-11-13T16:00:00+00:00")
    _write_run(repo_b / ".factory" / "runs", "legacy", "2026-11-13T17:00:00+00:00")

    records = list_fleet_run_history(
        _fleet_config({"demo/repo-a": str(repo_a), "demo/repo-b": str(repo_b)})
    )

    assert [r["run_id"] for r in records] == ["legacy", "resolved"]
    assert {r["run_id"]: r["repo_slug"] for r in records} == {
        "legacy": "demo/repo-b",
        "resolved": "demo/repo-a",
    }


def test_history_cmd_reads_resolved_runs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: `history` reads the resolved runs_dir."""
    external = tmp_path / "external-runs"
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(external))
    _write_run(external, "external-run", "2026-11-13T16:00:00+00:00")

    out = StringIO()
    with patch(
        "acp_factory.cli.console",
        new=Console(file=out, force_terminal=False, width=200),
    ):
        history_cmd(workspace=tmp_path, limit=10, json_output=True)

    data = json.loads(out.getvalue())
    assert [r["run_id"] for r in data] == ["external-run"]
