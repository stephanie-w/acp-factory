"""Unit tests for turn event tracking, transcript serialization, and condensed JSON output."""

from __future__ import annotations

import json
from pathlib import Path

from acp_factory.acp.events import (
    TurnEventTracker,
    TurnSummary,
    _condense_value,
    _safe_json_value,
    _truncate_text,
)
from acp_factory.config import FactoryConfig
from acp_factory.runner.loop import _persist_run_artifacts


def test_turn_summary_to_dict_basic() -> None:
    summary = TurnSummary(
        prompt="Write a function in src/util.py",
        response_text="Created util.py successfully.",
        thought_text="Let me inspect the directory first.",
        stop_reason="end_turn",
        duration_seconds=1.25,
        tool_call_count=1,
        file_operations=[{"operation": "write", "path": "src/util.py"}],
        permissions=[{"title": "write_file", "kind": "fs", "outcome": "approved"}],
        usage={"used": 1500, "cost": {"amount": 0.003}},
        tool_calls=[
            {
                "id": "tc-1",
                "title": "write_file",
                "kind": "fs",
                "status": "completed",
                "raw_input": {
                    "path": "src/util.py",
                    "content": "def add(a, b): return a + b\n",
                },
                "content": "File written: 32 bytes",
                "raw_output": {"bytes_written": 32},
            }
        ],
    )

    data = summary.to_dict()
    # Ensure serializable to standard JSON
    serialized = json.dumps(data)
    loaded = json.loads(serialized)

    assert loaded["summary"]["duration_seconds"] == 1.25
    assert loaded["summary"]["stop_reason"] == "end_turn"
    assert loaded["summary"]["tool_call_count"] == 1
    assert loaded["summary"]["file_operations_count"] == 1
    assert loaded["summary"]["tokens"] == 1500
    assert loaded["summary"]["cost_usd"] == 0.003

    assert loaded["prompt"] == "Write a function in src/util.py"
    assert loaded["response_text"] == "Created util.py successfully."
    assert len(loaded["tool_calls"]) == 1
    assert loaded["tool_calls"][0]["title"] == "write_file"
    assert loaded["tool_calls"][0]["status"] == "completed"
    assert loaded["tool_calls"][0]["content"] == "File written: 32 bytes"


def test_turn_summary_to_condensed_dict_truncation() -> None:
    huge_output = "Line of output\n" * 500  # ~7500 chars
    summary = TurnSummary(
        prompt="Run tests and inspect errors",
        response_text="Tests executed.",
        thought_text="Thinking process " * 100,
        stop_reason="end_turn",
        duration_seconds=5.0,
        tool_call_count=1,
        tool_calls=[
            {
                "id": "tc-huge",
                "title": "run_command",
                "kind": "shell",
                "status": "completed",
                "raw_input": {"CommandLine": "pytest -v", "big_data": "x" * 2000},
                "content": huge_output,
                "raw_output": {"log": huge_output},
            }
        ],
    )

    condensed = summary.to_condensed_dict(max_output_len=200)
    serialized = json.dumps(condensed)
    loaded = json.loads(serialized)

    assert "[truncated" in loaded["thought_preview"]
    assert len(loaded["tool_calls"]) == 1
    tc = loaded["tool_calls"][0]
    assert tc["title"] == "run_command"
    assert "[truncated" in tc["output_preview"]
    assert len(tc["output_preview"]) < 300
    assert "[truncated" in tc["raw_input"]["big_data"]
    assert "[truncated" in tc["raw_output"]["log"]


def test_turn_event_tracker_lifecycle() -> None:
    tracker = TurnEventTracker(initial_prompt="Initial prompt")
    tracker.begin_turn("Turn 1 prompt")

    # Simulate acpterm recording events
    tracker.recorder.add_thought("Investigating repository...")
    tracker.recorder.add_tool_call(
        "tc-1", "read_file", "fs", raw_input={"Path": "README.md"}
    )
    tracker.recorder.update_tool_call(
        "tc-1", status="completed", content="Readme contents"
    )
    tracker.recorder.add_file_operation("read", "README.md", line=1, limit=50)
    tracker.recorder.add_permission("read_file", "fs", "approved")
    tracker.recorder.add_message("Found the instructions in README.")
    tracker.recorder.set_usage({"used": 500, "cost": {"amount": 0.001}})

    summary = tracker.end_turn("end_turn")

    assert summary.prompt == "Turn 1 prompt"
    assert summary.thought_text == "Investigating repository..."
    assert summary.response_text == "Found the instructions in README."
    assert summary.tool_call_count == 1
    assert len(summary.tool_calls) == 1
    assert summary.tool_calls[0]["title"] == "read_file"
    assert summary.tool_calls[0]["content"] == "Readme contents"
    assert len(summary.file_operations) == 1
    assert summary.file_operations[0]["path"] == "README.md"
    assert len(summary.permissions) == 1
    assert summary.tokens == 500
    assert summary.cost_usd == 0.001

    # Verify history
    assert len(tracker.history) == 1
    assert tracker.cumulative_tokens() == 500
    assert tracker.cumulative_cost_usd() == 0.001


def test_persist_run_artifacts_json(tmp_path: Path) -> None:
    factory_dir = tmp_path / ".factory"
    factory_dir.mkdir(parents=True)

    config = FactoryConfig(task="Test task")
    summary = TurnSummary(
        prompt="Task prompt",
        response_text="Done",
        thought_text="Think",
        stop_reason="end_turn",
        duration_seconds=2.5,
        tool_call_count=1,
        tool_calls=[
            {
                "id": "tc-1",
                "title": "run_command",
                "status": "completed",
                "content": "output text",
            }
        ],
    )

    run_dir = _persist_run_artifacts(
        tmp_path,
        "run-123",
        config,
        succeeded=True,
        attempts_used=1,
        total_duration=2.5,
        setup_logs=[],
        turn_transcripts=["# Attempt 1 Markdown"],
        check_history=[],
        failure_reason=None,
        turn_summaries=[summary.to_dict()],
        turn_condensed=[summary.to_condensed_dict()],
    )

    assert run_dir is not None
    attempt_dir = run_dir / "attempt-1"
    assert (attempt_dir / "transcript.md").exists()
    assert (attempt_dir / "transcript.json").exists()
    assert (attempt_dir / "transcript.summary.json").exists()

    # Verify JSON content is valid and structured
    full_json = json.loads(
        (attempt_dir / "transcript.json").read_text(encoding="utf-8")
    )
    assert full_json["summary"]["stop_reason"] == "end_turn"
    assert full_json["tool_calls"][0]["title"] == "run_command"

    summary_json = json.loads(
        (attempt_dir / "transcript.summary.json").read_text(encoding="utf-8")
    )
    assert summary_json["summary"]["duration_seconds"] == 2.5
    assert summary_json["tool_calls"][0]["output_preview"] == "output text"


def test_helpers_safe_json_and_condense() -> None:
    # Test Path conversion
    path_val = Path("/tmp/some/file.txt")
    assert _safe_json_value(path_val) == "/tmp/some/file.txt"

    # Test list truncation
    long_list = list(range(50))
    condensed_list = _condense_value(long_list, 100)
    assert len(condensed_list) == 26  # 25 items + 1 omitted notice
    assert "omitted" in condensed_list[-1]

    # Test text truncation
    short = _truncate_text("hello", 10)
    assert short == "hello"
    long_txt = _truncate_text("hello world long text", 5)
    assert long_txt.startswith("hello\n... [truncated 16 chars]")
