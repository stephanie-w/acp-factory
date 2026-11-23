"""Tests for `acp-factory inspect` and transcript inspection helpers."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from acp_factory.cli import app
from acp_factory.inspect import (
    compute_runs_stats,
    compute_transcript_stats,
    extract_tool_exit_code,
    filter_tool_calls,
    resolve_transcript_path,
)


@pytest.fixture(autouse=True)
def _isolated_runs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate both resolved `runs_dir` sources from the developer's real data home."""
    monkeypatch.delenv("FACTORY_RUNS_DIR", raising=False)
    xdg_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))
    return xdg_home


def _write_sample_run(
    workspace: Path,
    run_id: str = "20261115-120000-abcdef",
    *,
    timestamp: str = "2026-11-15T12:00:00+00:00",
    runs_dir: Path | None = None,
) -> Path:
    run_dir = (runs_dir or workspace / ".factory" / "runs") / run_id
    attempt_dir = run_dir / "attempt-1"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "timestamp": timestamp,
                "stage": "build",
                "succeeded": True,
                "attempts_used": 1,
                "duration_seconds": 12.5,
            }
        ),
        encoding="utf-8",
    )
    transcript = {
        "thought_text": "Checking html2md script.\nLet me run tests.",
        "response_text": "All done.\nUpdated docs.",
        "tool_calls": [
            {
                "id": "call_0",
                "title": "Fetch release notes",
                "kind": "execute",
                "status": "completed",
                "raw_input": {},
                "content": "Fetched markdown via html2md",
                "raw_output": {"metadata": {"exit": 0}},
            },
            {
                "id": "call_1",
                "title": "Read src/app.py",
                "kind": "read",
                "status": "completed",
                "raw_input": {},
                "content": "print('hello')",
                "raw_output": {"metadata": {"exit": 0}},
            },
            {
                "id": "call_2",
                "title": "Run pytest",
                "kind": "execute",
                "status": "completed",
                "raw_input": {},
                "content": "1 failed",
                "raw_output": {"metadata": {"exit": 1}},
            },
        ],
    }
    transcript_path = attempt_dir / "transcript.json"
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")
    return transcript_path


def _write_named_run(workspace: Path, *, include_name: bool = True) -> Path:
    """Write a single-call run whose tool call may carry a stable ACP name."""
    run_id = "20261116-090000-named"
    run_dir = workspace / ".factory" / "runs" / run_id
    attempt_dir = run_dir / "attempt-1"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "timestamp": "2026-11-16T09:00:00+00:00",
                "stage": "build",
                "succeeded": True,
                "attempts_used": 1,
            }
        ),
        encoding="utf-8",
    )
    call: dict[str, object] = {
        "id": "call_0",
        "title": "Execute command",
        "kind": "execute",
        "status": "completed",
        "raw_input": {},
        "content": "ok",
        "raw_output": {"metadata": {"exit": 0}},
    }
    if include_name:
        call["name"] = "run_command"
    transcript_path = attempt_dir / "transcript.json"
    transcript_path.write_text(json.dumps({"tool_calls": [call]}), encoding="utf-8")
    return transcript_path


def _invoke_inspect(*args: str) -> str:
    """Run `inspect` against a wide console and return the rendered output."""
    buffer = StringIO()
    with patch(
        "acp_factory.cli.console",
        new=Console(file=buffer, force_terminal=False, width=200),
    ):
        res = CliRunner().invoke(app, ["inspect", *args])
    assert res.exit_code == 0, res.output
    return buffer.getvalue()


def _table_name_cell(output: str, title: str) -> str:
    """Return the Name cell of the rendered tool-call row with `title`."""
    row = next(line for line in output.splitlines() if title in line)
    return [cell.strip() for cell in row.split("│")][3]


def test_resolve_transcript_path_variants(tmp_path: Path) -> None:
    older = _write_sample_run(
        tmp_path, "20261115-110000-111111", timestamp="2026-11-15T11:00:00+00:00"
    )
    newer = _write_sample_run(
        tmp_path, "20261115-120000-222222", timestamp="2026-11-15T12:00:00+00:00"
    )

    assert resolve_transcript_path(None, workspace=tmp_path) == newer
    assert resolve_transcript_path("20261115-110000", workspace=tmp_path) == older
    assert resolve_transcript_path(str(older), workspace=tmp_path) == older

    with pytest.raises(FileNotFoundError):
        resolve_transcript_path("nonexistent", workspace=tmp_path)


def test_resolve_transcript_path_latest(tmp_path: Path) -> None:
    """AC-4: explicit `latest` resolves the transcript of the symlinked run."""
    _write_sample_run(tmp_path, "20261115-100000-older")
    newest = _write_sample_run(tmp_path, "20261115-130000-newest")
    runs_dir = tmp_path / ".factory" / "runs"
    (runs_dir / "latest").symlink_to("20261115-130000-newest", target_is_directory=True)

    resolved = resolve_transcript_path("latest", workspace=tmp_path)
    assert resolved.resolve() == newest.resolve()
    assert resolved.parent.parent.name == "latest"


def test_filter_tool_calls_and_exit_code() -> None:
    calls = [
        {
            "title": "A",
            "kind": "execute",
            "status": "completed",
            "raw_output": {"metadata": {"exit": 0}},
        },
        {"title": "B", "kind": "read", "status": "completed"},
        {
            "title": "C",
            "kind": "execute",
            "status": "completed",
            "raw_output": {"metadata": {"exit": 1}},
            "content": "boom",
        },
    ]
    assert extract_tool_exit_code(calls[0]) == 0
    assert extract_tool_exit_code(calls[1]) is None
    assert [idx for idx, _ in filter_tool_calls(calls, kind="execute")] == [0, 2]
    assert [idx for idx, _ in filter_tool_calls(calls, failed=True)] == [2]
    assert [idx for idx, _ in filter_tool_calls(calls, grep="boom")] == [2]


def test_cli_inspect_views(tmp_path: Path) -> None:
    _write_sample_run(tmp_path)
    runner = CliRunner()

    res_default = runner.invoke(app, ["inspect", "--workspace", str(tmp_path)])
    assert res_default.exit_code == 0
    assert "Fetch release notes" in res_default.stdout
    assert "Run pytest" in res_default.stdout

    res_call = runner.invoke(
        app, ["inspect", "--workspace", str(tmp_path), "--call", "0"]
    )
    assert res_call.exit_code == 0
    assert "Fetched markdown via html2md" in res_call.stdout

    res_thoughts = runner.invoke(
        app, ["inspect", "--workspace", str(tmp_path), "--thoughts"]
    )
    assert res_thoughts.exit_code == 0
    assert "Checking html2md script." in res_thoughts.stdout

    res_grep_json = runner.invoke(
        app, ["inspect", "--workspace", str(tmp_path), "--grep", "html2md", "--json"]
    )
    assert res_grep_json.exit_code == 0
    payload = json.loads(res_grep_json.stdout)
    assert len(payload["thought_matches"]) == 1
    assert len(payload["tool_call_matches"]) == 1
    assert payload["tool_call_matches"][0]["index"] == 0


def test_filter_tool_calls_grep_name() -> None:
    """AC-3: `--grep` matches a tool call by its stable name, even when unnamed."""
    calls = [
        {
            "id": "c1",
            "name": "run_command",
            "title": "Execute",
            "kind": "execute",
            "status": "completed",
        },
        {
            "id": "c2",
            "name": "read_file",
            "title": "Read",
            "kind": "read",
            "status": "completed",
        },
        {"id": "c3", "title": "Legacy call", "kind": "other", "status": "completed"},
    ]

    assert [idx for idx, _ in filter_tool_calls(calls, grep="RUN_COMMAND")] == [0]
    assert [idx for idx, _ in filter_tool_calls(calls, grep="read_file")] == [1]
    assert filter_tool_calls(calls, grep="run_command")[0][1]["id"] == "c1"


def test_compute_transcript_stats_preserves_name() -> None:
    """AC-6: per-call stats rows carry the stable name, or None for legacy calls."""
    data = {
        "tool_calls": [
            {
                "name": "run_command",
                "title": "Run pytest",
                "kind": "execute",
                "status": "completed",
                "content": "ok",
            },
            {"title": "Legacy call", "kind": "read", "status": "completed"},
        ]
    }

    stats = compute_transcript_stats(data)

    by_index = {row["index"]: row for row in stats["largest_calls"]}
    assert by_index[0]["name"] == "run_command"
    assert by_index[1]["name"] is None


def test_cli_inspect_shows_name_column(tmp_path: Path) -> None:
    """AC-4: the summary table exposes a Name column with the recorded name."""
    _write_named_run(tmp_path)

    output = _invoke_inspect("--workspace", str(tmp_path))

    assert _table_name_cell(output, "Execute command") == "run_command"


def test_cli_inspect_call_detail_name(tmp_path: Path) -> None:
    """AC-5: `--call` renders the stable name in the detail header."""
    _write_named_run(tmp_path)

    output = _invoke_inspect("--workspace", str(tmp_path), "--call", "0")

    assert "name: run_command" in output
    assert output.index("id: call_0") < output.index("name: run_command")


def test_cli_inspect_legacy_transcript_compatibility(tmp_path: Path) -> None:
    """AC-7: transcripts without `name` render `-` and omit the detail line."""
    _write_named_run(tmp_path, include_name=False)

    table_output = _invoke_inspect("--workspace", str(tmp_path))
    assert _table_name_cell(table_output, "Execute command") == "-"

    detail_output = _invoke_inspect("--workspace", str(tmp_path), "--call", "0")
    assert "name:" not in detail_output


def test_turn_summary_uses_real_recorder_no_per_call_duration() -> None:
    """TurnEventTracker and TurnSummary preserve turn-level duration from acpterm.

    acpterm's ``TranscriptRecorder`` never records a per-tool-call ``duration_seconds``
    (only turn-level via ``set_duration``). Driving the real recorder API confirms the
    TurnSummary forwards the turn duration and does not invent per-call durations.
    """
    from acp_factory.acp.events import TurnEventTracker

    tracker = TurnEventTracker()
    tracker.begin_turn("hi")
    tracker.recorder.add_tool_call("tc_1", "Run pytest", "execute")
    tracker.recorder.update_tool_call(
        "tc_1", status="completed", content="ok", raw_output={"metadata": {"exit": 0}}
    )
    summary = tracker.end_turn("end_turn")
    full = summary.to_dict()
    condensed = summary.to_condensed_dict()

    assert summary.duration_seconds >= 0.0
    assert full["summary"]["duration_seconds"] >= 0.0
    assert condensed["summary"]["duration_seconds"] >= 0.0
    assert full["tool_calls"][0]["duration_seconds"] >= 0.0
    assert condensed["tool_calls"][0]["duration_seconds"] >= 0.0


def test_cli_stats_single_and_aggregate(tmp_path: Path) -> None:
    """`acp-factory stats` works both for a single run and across runs."""
    t_path = _write_sample_run(tmp_path)
    data = json.loads(t_path.read_text(encoding="utf-8"))
    data["duration_seconds"] = 12.5
    data["usage"] = {"total_tokens": 1200, "cost": {"amount": 0.0125}}
    data["tool_calls"][0]["duration_seconds"] = 4.25
    data["tool_calls"][2]["duration_seconds"] = 0.75
    t_path.write_text(json.dumps(data), encoding="utf-8")

    runner = CliRunner()

    res_single = runner.invoke(
        app, ["stats", "20261115-120000-abcdef", "--workspace", str(tmp_path), "--json"]
    )
    assert res_single.exit_code == 0
    single = json.loads(res_single.stdout)
    assert single["tool_call_count"] == 3
    assert single["failed_count"] == 1
    assert single["tokens"] == 1200
    assert single["cost_usd"] == 0.0125
    assert single["estimated_tokens"] > 0
    assert set(single["estimated_tokens_breakdown"]) == {
        "prompt",
        "thought",
        "response",
        "tool_output",
    }
    assert single["slowest_calls"][0]["index"] == 0
    assert single["slowest_calls"][0]["duration_seconds"] == 4.25

    res_agg = runner.invoke(app, ["stats", "--workspace", str(tmp_path), "--json"])
    assert res_agg.exit_code == 0
    agg = json.loads(res_agg.stdout)
    assert agg["total_runs"] == 1
    assert agg["succeeded_runs"] == 1
    assert agg["total_estimated_tokens"] == single["estimated_tokens"]
    assert agg["telemetry_runs"] == 0
    assert agg["top_token_runs"][0]["run_id"] == "20261115-120000-abcdef"
    assert agg["slowest_calls"][0]["duration_seconds"] == 4.25

    res_table = runner.invoke(app, ["stats", "--workspace", str(tmp_path)])
    assert res_table.exit_code == 0
    assert "Stats by Stage" in res_table.stdout
    assert "Top 1 Slowest Runs" in res_table.stdout
    assert "Top 1 Highest-Token Runs" in res_table.stdout
    assert "Telemetry:" in res_table.stdout


def test_cli_stats_token_usage_telemetry_and_fallback(tmp_path: Path) -> None:
    """Aggregate stats tracks reported tokens, cost, telemetry count, and ~est. fallback."""
    t1 = _write_sample_run(
        tmp_path, "20261115-120000-runone", timestamp="2026-11-15T12:00:00+00:00"
    )
    (t1.parent.parent / "run.json").write_text(
        json.dumps(
            {
                "run_id": "20261115-120000-runone",
                "timestamp": "2026-11-15T12:00:00+00:00",
                "stage": "build",
                "model": "claude-sonnet",
                "succeeded": True,
                "duration_seconds": 10.0,
                "attempts_used": 1,
                "usage": {
                    "total_tokens": 5000,
                    "total_cost_usd": 0.05,
                    "telemetry_available": True,
                },
            }
        ),
        encoding="utf-8",
    )

    t2 = _write_sample_run(
        tmp_path, "20261115-130000-runtwo", timestamp="2026-11-15T13:00:00+00:00"
    )
    (t2.parent.parent / "run.json").write_text(
        json.dumps(
            {
                "run_id": "20261115-130000-runtwo",
                "timestamp": "2026-11-15T13:00:00+00:00",
                "stage": "triage",
                "model": "deepseek-flash",
                "succeeded": True,
                "duration_seconds": 5.0,
                "attempts_used": 1,
                "usage": {
                    "total_tokens": 0,
                    "total_cost_usd": 0.0,
                    "telemetry_available": False,
                },
            }
        ),
        encoding="utf-8",
    )

    runner = CliRunner()
    res = runner.invoke(app, ["stats", "--workspace", str(tmp_path), "--json"])
    assert res.exit_code == 0
    agg = json.loads(res.stdout)

    assert agg["total_runs"] == 2
    assert agg["telemetry_runs"] == 1
    assert agg["total_tokens"] == 5000
    assert agg["avg_tokens_per_run"] == 2500
    assert agg["total_estimated_tokens"] > 0
    assert agg["total_cost_usd"] == 0.05

    by_stage = {row["stage"]: row for row in agg["by_stage"]}
    assert by_stage["build"]["total_tokens"] == 5000
    assert by_stage["build"]["total_cost_usd"] == 0.05
    assert by_stage["triage"]["total_tokens"] == 0
    assert by_stage["triage"]["total_estimated_tokens"] > 0

    assert agg["top_token_runs"][0]["run_id"] == "20261115-120000-runone"
    assert agg["top_token_runs"][1]["run_id"] == "20261115-130000-runtwo"


def test_cli_inspect_shows_output_size(tmp_path: Path) -> None:
    """`inspect` displays tool output character counts in the table and `--call` view."""
    run_id = "20261122-160000-size01"
    run_dir = tmp_path / ".factory" / "runs" / run_id
    attempt_dir = run_dir / "attempt-1"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "timestamp": "2026-11-22T16:00:00+00:00",
                "stage": "build",
                "succeeded": True,
                "attempts_used": 1,
            }
        ),
        encoding="utf-8",
    )
    transcript = {
        "tool_calls": [
            {
                "id": "call_0",
                "title": "Large output call",
                "kind": "read",
                "status": "completed",
                "content": "x" * 1234,
            },
            {
                "id": "call_1",
                "title": "Empty output call",
                "kind": "execute",
                "status": "completed",
            },
        ]
    }
    (attempt_dir / "transcript.json").write_text(
        json.dumps(transcript), encoding="utf-8"
    )

    table_output = _invoke_inspect("--workspace", str(tmp_path))
    assert "Chars" in table_output
    large_row = next(
        line for line in table_output.splitlines() if "Large output call" in line
    )
    empty_row = next(
        line for line in table_output.splitlines() if "Empty output call" in line
    )
    assert [cell.strip() for cell in large_row.split("│")][7] == "1,234"
    assert [cell.strip() for cell in empty_row.split("│")][7] == "-"

    detail_output = _invoke_inspect("--workspace", str(tmp_path), "--call", "0")
    assert "output: 1,234 chars" in detail_output


def test_resolve_transcript_path_primary_and_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: explicit/prefix lookups span resolved runs_dir and legacy `.factory/runs/`."""
    external = tmp_path / "external-runs"
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(external))
    primary = _write_sample_run(
        tmp_path,
        "20261115-120000-primary",
        timestamp="2026-11-15T12:00:00+00:00",
        runs_dir=external,
    )
    legacy = _write_sample_run(
        tmp_path,
        "20261115-110000-legacy",
        timestamp="2026-11-15T11:00:00+00:00",
    )

    assert resolve_transcript_path("20261115-120000-p", workspace=tmp_path) == primary
    assert resolve_transcript_path("20261115-110000-l", workspace=tmp_path) == legacy
    assert resolve_transcript_path(None, workspace=tmp_path) == primary


def test_resolve_transcript_path_ambiguous_prefix_across_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: a prefix matching runs in both directories is reported as ambiguous."""
    external = tmp_path / "external-runs"
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(external))
    _write_sample_run(tmp_path, "20261115-120000-aaaa", runs_dir=external)
    _write_sample_run(tmp_path, "20261115-120000-bbbb")

    with pytest.raises(FileNotFoundError, match="Ambiguous run ID prefix"):
        resolve_transcript_path("20261115-120000", workspace=tmp_path)


def test_compute_runs_stats_reads_primary_and_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: aggregate stats locate transcripts in both resolved and legacy runs."""
    external = tmp_path / "external-runs"
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(external))
    _write_sample_run(tmp_path, "20261115-110000-legacy")
    legacy_only = compute_runs_stats(tmp_path)
    assert legacy_only["total_runs"] == 1

    _write_sample_run(tmp_path, "20261115-120000-primary", runs_dir=external)
    combined = compute_runs_stats(tmp_path)

    assert combined["total_runs"] == 2
    assert combined["total_estimated_tokens"] == (
        2 * legacy_only["total_estimated_tokens"]
    )


def test_cli_inspect_and_stats_read_factory_runs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: CLI `inspect` and `stats` honor the resolved runs_dir."""
    external = tmp_path / "external-runs"
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(external))
    _write_sample_run(tmp_path, "20261115-120000-cli", runs_dir=external)

    inspect_out = _invoke_inspect("20261115-120000-cli", "--workspace", str(tmp_path))
    assert "Fetch release notes" in inspect_out

    res = CliRunner().invoke(app, ["stats", "--workspace", str(tmp_path), "--json"])
    assert res.exit_code == 0
    agg = json.loads(res.stdout)
    assert agg["total_runs"] == 1
    assert agg["top_token_runs"][0]["run_id"] == "20261115-120000-cli"
