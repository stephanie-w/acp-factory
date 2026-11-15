"""Tests for `acp-factory inspect` and transcript inspection helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from acp_factory.cli import app
from acp_factory.inspect import (
    extract_tool_exit_code,
    filter_tool_calls,
    resolve_transcript_path,
)


def _write_sample_run(
    workspace: Path,
    run_id: str = "20261115-120000-abcdef",
    *,
    started_at: str = "2026-11-15T12:00:00Z",
) -> Path:
    run_dir = workspace / ".factory" / "runs" / run_id
    attempt_dir = run_dir / "attempt-1"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": run_id, "started_at": started_at, "succeeded": True}),
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


def test_resolve_transcript_path_variants(tmp_path: Path) -> None:
    older = _write_sample_run(
        tmp_path, "20261115-110000-111111", started_at="2026-11-15T11:00:00Z"
    )
    newer = _write_sample_run(
        tmp_path, "20261115-120000-222222", started_at="2026-11-15T12:00:00Z"
    )

    assert resolve_transcript_path(None, workspace=tmp_path) == newer
    assert resolve_transcript_path("20261115-110000", workspace=tmp_path) == older
    assert resolve_transcript_path(str(older), workspace=tmp_path) == older

    with pytest.raises(FileNotFoundError):
        resolve_transcript_path("nonexistent", workspace=tmp_path)


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


def test_turn_summary_forwards_tool_call_duration() -> None:
    """TurnEventTracker and TurnSummary preserve duration_seconds from acpterm."""
    from acp_factory.acp.events import TurnEventTracker

    tracker = TurnEventTracker()
    tracker.begin_turn("hi")
    tracker.recorder.tool_calls["tc_1"] = {
        "title": "Run pytest",
        "kind": "execute",
        "status": "completed",
        "content": ["ok"],
        "raw_input": {},
        "raw_output": {"metadata": {"exit": 0}},
        "duration_seconds": 1.234,
    }
    summary = tracker.end_turn("end_turn")
    full = summary.to_dict()
    condensed = summary.to_condensed_dict()

    assert full["tool_calls"][0]["duration_seconds"] == 1.234
    assert condensed["tool_calls"][0]["duration_seconds"] == 1.234


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
    assert single["slowest_calls"][0]["index"] == 0
    assert single["slowest_calls"][0]["duration_seconds"] == 4.25

    res_agg = runner.invoke(app, ["stats", "--workspace", str(tmp_path), "--json"])
    assert res_agg.exit_code == 0
    agg = json.loads(res_agg.stdout)
    assert agg["total_runs"] == 1
    assert agg["succeeded_runs"] == 1
    assert agg["slowest_calls"][0]["duration_seconds"] == 4.25

    res_table = runner.invoke(app, ["stats", "--workspace", str(tmp_path)])
    assert res_table.exit_code == 0
    assert "Stats by Stage" in res_table.stdout
    assert "Top 1 Slowest Runs" in res_table.stdout
