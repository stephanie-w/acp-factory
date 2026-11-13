"""Unit tests for turn event tracking, transcript serialization, and condensed JSON output."""

from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from rich.console import Console

from acp_factory.acp.events import (
    TurnEventTracker,
    TurnSummary,
    _condense_value,
    _safe_json_value,
    _truncate_text,
)
from acp_factory.config import FactoryConfig
from acp_factory.runner.loop import (
    FactoryRunOutcome,
    _persist_run_artifacts,
    run_branch_review,
    run_factory,
    run_issue_stage,
)
from acp_factory.task import StageVerdict, TaskContext
from acp_factory.ui.display import FactoryDisplay


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


def _persist(
    workspace: Path,
    *,
    config: FactoryConfig,
    run_id: str,
    response_text: str = "",
    succeeded: bool = True,
) -> Path | None:
    return _persist_run_artifacts(
        workspace,
        run_id,
        config,
        succeeded=succeeded,
        attempts_used=1,
        total_duration=0.5,
        setup_logs=[],
        turn_transcripts=[],
        check_history=[],
        failure_reason=None,
        last_response_text=response_text,
    )


def test_persist_run_artifacts_review_report(tmp_path: Path) -> None:
    """AC-1: review responses are persisted as a clean standalone report with the verdict."""
    (tmp_path / ".factory").mkdir()
    config = FactoryConfig(task="Review changes", active_stage="review")
    response = (
        "Sure, let me walk you through it.\n\n"
        "# Review Report\n\n"
        "## Acceptance Criteria\n- AC-1: pass\n\n"
        "## Findings\n- None.\n\n"
        "VERDICT: ACCEPTED\n"
    )

    run_dir = _persist(
        tmp_path, config=config, run_id="run-review", response_text=response
    )

    assert run_dir is not None
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# Review Report")
    assert "## Acceptance Criteria" in report
    assert "## Findings" in report
    assert "VERDICT: ACCEPTED" in report
    assert "Sure, let me walk you through it." not in report

    # A verdict-only response still produces a non-empty report retaining the token.
    verdict_only_dir = _persist(
        tmp_path,
        config=config,
        run_id="run-verdict-only",
        response_text="VERDICT: REVISION_NEEDED",
    )
    assert verdict_only_dir is not None
    verdict_report = (verdict_only_dir / "report.md").read_text(encoding="utf-8")
    assert verdict_report.strip() == "VERDICT: REVISION_NEEDED"

    # Whitespace-only review output must not create a report.
    blank_dir = _persist(
        tmp_path, config=config, run_id="run-blank", response_text="  \n\t "
    )
    assert blank_dir is not None
    assert not (blank_dir / "report.md").exists()


def test_latest_report_pointer_symlink_and_fallback(tmp_path: Path) -> None:
    """AC-2: latest pointers are relative symlinks that fall back to a copy on OSError."""
    (tmp_path / ".factory").mkdir()
    config = FactoryConfig(task="Review changes", active_stage="review")
    response = "# Review Report\n\n## Findings\n- None.\n\nVERDICT: ACCEPTED\n"

    run_dir = _persist(
        tmp_path, config=config, run_id="run-abc", response_text=response
    )
    assert run_dir is not None

    latest_run = tmp_path / ".factory" / "runs" / "latest"
    latest_report = tmp_path / ".factory" / "latest-report.md"
    assert latest_run.is_symlink()
    assert os.readlink(latest_run) == "run-abc"
    assert latest_report.is_symlink()
    assert os.readlink(latest_report) == str(Path("runs") / "run-abc" / "report.md")
    assert latest_report.read_text(encoding="utf-8") == (
        run_dir / "report.md"
    ).read_text(encoding="utf-8")

    # When the platform/filesystem refuses symlinks, the pointer gracefully becomes a copy.
    with patch.object(Path, "symlink_to", side_effect=OSError("symlinks disabled")):
        fallback_dir = _persist(
            tmp_path, config=config, run_id="run-def", response_text=response
        )
    assert fallback_dir is not None
    assert latest_report.exists()
    assert not latest_report.is_symlink()
    assert latest_report.read_text(encoding="utf-8") == (
        fallback_dir / "report.md"
    ).read_text(encoding="utf-8")


def test_persist_run_artifacts_non_review_stage(tmp_path: Path) -> None:
    """AC-3: non-review stages and missing `.factory/` never write a report or raise."""
    (tmp_path / ".factory").mkdir()
    config = FactoryConfig(task="Build it", active_stage="build")

    run_dir = _persist(
        tmp_path,
        config=config,
        run_id="run-build",
        response_text="# Build report\nSTATUS: DONE",
    )
    assert run_dir is not None
    assert not (run_dir / "report.md").exists()
    assert not (tmp_path / ".factory" / "latest-report.md").exists()
    assert not (tmp_path / ".factory" / "runs" / "latest").exists()

    # A workspace without `.factory/` is a no-op, not an error.
    bare_workspace = tmp_path / "bare"
    bare_workspace.mkdir()
    assert (
        _persist(
            bare_workspace,
            config=FactoryConfig(task="Review", active_stage="review"),
            run_id="run-bare",
            response_text="# Review\nVERDICT: ACCEPTED",
        )
        is None
    )


async def test_factory_run_outcome_report_path(tmp_path: Path) -> None:
    """AC-4: `run_factory` exposes the persisted review report path on the outcome."""
    (tmp_path / ".factory").mkdir()
    config = FactoryConfig(task="Review changes", active_stage="review")

    class FakeReviewClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.tracker = AsyncMock()
            self.tracker.history = []

        async def __aenter__(self) -> FakeReviewClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def send_prompt(self, prompt: str, **kwargs: object) -> object:
            from acp_factory.acp.events import TurnSummary

            return type(
                "TurnResult",
                (),
                {
                    "stop_reason": "end_turn",
                    "summary": TurnSummary(
                        prompt=prompt,
                        response_text="# Review Report\n\nAll good.\n\nVERDICT: ACCEPTED",
                        thought_text="",
                        stop_reason="end_turn",
                        duration_seconds=0.01,
                        tool_call_count=0,
                    ),
                },
            )()

    with patch("acp_factory.runner.loop.FactoryACPClient", FakeReviewClient):
        outcome = await run_factory(config, tmp_path)

    assert outcome.succeeded is True
    run_dir = outcome.run_dir
    assert run_dir is not None
    assert outcome.report_path is not None
    assert outcome.report_path == run_dir / "report.md"
    assert outcome.report_path.is_file()
    assert "VERDICT: ACCEPTED" in outcome.report_path.read_text(encoding="utf-8")

    # The field defaults to None for callers that construct outcomes directly.
    assert (
        FactoryRunOutcome(
            succeeded=True, exit_code=0, attempts_used=1, total_duration_seconds=0.0
        ).report_path
        is None
    )


def test_show_final_summary_displays_report_path(tmp_path: Path) -> None:
    """AC-5: the final summary grid surfaces the report path."""
    buffer = StringIO()
    display = FactoryDisplay(Console(file=buffer, width=200, highlight=False))
    report_path = Path(".factory/runs/run-1/report.md")

    display.show_final_summary(
        succeeded=True,
        attempts_used=1,
        max_attempts=1,
        total_duration=0.1,
        report_path=report_path,
    )

    output = buffer.getvalue()
    assert "Report:" in output
    assert str(report_path) in output
    assert "[cyan]" not in output


def test_cli_completion_prints_report_path(tmp_path: Path) -> None:
    """AC-6: `issue`, `review`, and `next` print the review report path when present."""
    from typer.testing import CliRunner

    from acp_factory.cli import app

    report_path = tmp_path / ".factory" / "runs" / "run-1" / "report.md"
    verdict = StageVerdict(
        stage="review",
        status="accepted",
        target_label="factory:pr-ready",
        updated_factory_block="",
        summary="ok",
    )
    outcome = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=0.0,
        stage_verdict=verdict,
        report_path=report_path,
    )
    runner = CliRunner()

    with patch("acp_factory.cli.run_issue_stage", new=AsyncMock(return_value=outcome)):
        issue_res = runner.invoke(app, ["issue", "5", "--workspace", str(tmp_path)])
    assert issue_res.exit_code == 0
    assert "Review Report:" in issue_res.output
    assert str(report_path) in issue_res.output.replace("\n", "")

    with patch(
        "acp_factory.cli.run_branch_review", new=AsyncMock(return_value=outcome)
    ):
        review_res = runner.invoke(app, ["review", "--workspace", str(tmp_path)])
    assert review_res.exit_code == 0
    assert "Review Report:" in review_res.output
    assert str(report_path) in review_res.output.replace("\n", "")

    task = TaskContext(issue_number=5, title="Review me", labels=["factory:review"])
    with (
        patch(
            "acp_factory.cli.FactoryConfig.discover",
            return_value=FactoryConfig(task="Review", active_stage="review"),
        ),
        patch(
            "acp_factory.cli.list_actionable_issues",
            new=AsyncMock(return_value=[task]),
        ),
        patch("acp_factory.cli.run_issue_stage", new=AsyncMock(return_value=outcome)),
    ):
        next_res = runner.invoke(app, ["next", "--workspace", str(tmp_path)])
    assert next_res.exit_code == 0
    assert "Review Report:" in next_res.output
    assert str(report_path) in next_res.output.replace("\n", "")


def _build_summary_response() -> str:
    return (
        "Sure, here is a walkthrough of my work.\n\n"
        "### Implementation Summary\n"
        "- **Files Modified**: `src/app.py`\n"
        "- **Door & Blast Radius**: Two-way — Isolated\n"
        "- **Evidence**:\n"
        "  - **Before**: `1 failed`\n"
        "  - **After**: `1 passed`\n\n"
        "STATUS: DONE\n"
    )


def _factory_workspace(root: Path, name: str) -> Path:
    workspace = root / name
    (workspace / ".factory").mkdir(parents=True)
    return workspace


def test_persist_run_artifacts_build_notes(tmp_path: Path) -> None:
    """AC-1: build summaries persist cleanly and other stages/responses stay silent."""
    build_ws = _factory_workspace(tmp_path, "build")
    build_config = FactoryConfig(task="Build it", active_stage="build")
    run_dir = _persist(
        build_ws,
        config=build_config,
        run_id="run-build-notes",
        response_text=_build_summary_response(),
    )

    assert run_dir is not None
    notes = (run_dir / "implementation-notes.md").read_text(encoding="utf-8")
    assert notes.startswith("### Implementation Summary")
    assert "- **Files Modified**: `src/app.py`" in notes
    assert "- **After**: `1 passed`" in notes
    assert "Sure, here is a walkthrough" not in notes
    assert "STATUS: DONE" not in notes

    review_ws = _factory_workspace(tmp_path, "review")
    review_dir = _persist(
        review_ws,
        config=FactoryConfig(task="Review it", active_stage="review"),
        run_id="run-review-notes",
        response_text=_build_summary_response(),
    )
    assert review_dir is not None
    assert not (review_dir / "implementation-notes.md").exists()
    assert not (review_ws / ".factory" / "latest-build-notes.md").exists()

    plain_ws = _factory_workspace(tmp_path, "plain")
    plain_dir = _persist(
        plain_ws,
        config=FactoryConfig(task="Build it", active_stage="build"),
        run_id="run-build-plain",
        response_text="# Build complete\n\nSTATUS: DONE",
    )
    assert plain_dir is not None
    assert not (plain_dir / "implementation-notes.md").exists()
    assert not (plain_ws / ".factory" / "latest-build-notes.md").exists()
    assert not (plain_ws / ".factory" / "runs" / "latest").exists()


def test_latest_build_notes_symlink_and_fallback(tmp_path: Path) -> None:
    """AC-2: build-note pointers are relative symlinks that fall back to a copy."""
    workspace = _factory_workspace(tmp_path, "ws")
    config = FactoryConfig(task="Build it", active_stage="build")

    run_dir = _persist(
        workspace,
        config=config,
        run_id="run-build",
        response_text=_build_summary_response(),
    )
    assert run_dir is not None

    latest_run = workspace / ".factory" / "runs" / "latest"
    latest_notes = workspace / ".factory" / "latest-build-notes.md"
    assert latest_run.is_symlink()
    assert os.readlink(latest_run) == "run-build"
    assert latest_notes.is_symlink()
    assert os.readlink(latest_notes) == str(
        Path("runs") / "run-build" / "implementation-notes.md"
    )
    assert latest_notes.read_text(encoding="utf-8") == (
        run_dir / "implementation-notes.md"
    ).read_text(encoding="utf-8")

    with patch.object(Path, "symlink_to", side_effect=OSError("symlinks disabled")):
        fallback_dir = _persist(
            workspace,
            config=config,
            run_id="run-build-fallback",
            response_text=_build_summary_response(),
        )
    assert fallback_dir is not None
    assert latest_notes.exists()
    assert not latest_notes.is_symlink()
    assert latest_notes.read_text(encoding="utf-8") == (
        fallback_dir / "implementation-notes.md"
    ).read_text(encoding="utf-8")


async def test_factory_run_outcome_build_notes_path(tmp_path: Path) -> None:
    """AC-3: `run_factory` exposes the persisted build notes path on the outcome."""
    (tmp_path / ".factory").mkdir()
    config = FactoryConfig(task="Build it", active_stage="build")

    class FakeBuildClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.tracker = AsyncMock()
            self.tracker.history = []

        async def __aenter__(self) -> FakeBuildClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def send_prompt(self, prompt: str, **kwargs: object) -> object:
            from acp_factory.acp.events import TurnSummary

            return type(
                "TurnResult",
                (),
                {
                    "stop_reason": "end_turn",
                    "summary": TurnSummary(
                        prompt=prompt,
                        response_text=_build_summary_response(),
                        thought_text="",
                        stop_reason="end_turn",
                        duration_seconds=0.01,
                        tool_call_count=0,
                    ),
                },
            )()

    with patch("acp_factory.runner.loop.FactoryACPClient", FakeBuildClient):
        outcome = await run_factory(config, tmp_path)

    assert outcome.succeeded is True
    run_dir = outcome.run_dir
    assert run_dir is not None
    assert outcome.build_notes_path is not None
    assert outcome.build_notes_path == run_dir / "implementation-notes.md"
    assert outcome.build_notes_path.is_file()
    assert "### Implementation Summary" in outcome.build_notes_path.read_text(
        encoding="utf-8"
    )

    assert (
        FactoryRunOutcome(
            succeeded=True, exit_code=0, attempts_used=1, total_duration_seconds=0.0
        ).build_notes_path
        is None
    )


def test_show_final_summary_displays_build_notes_path(tmp_path: Path) -> None:
    """AC-4: the final summary grid surfaces the build notes path."""
    buffer = StringIO()
    display = FactoryDisplay(Console(file=buffer, width=200, highlight=False))
    build_notes_path = Path(".factory/runs/run-1/implementation-notes.md")

    display.show_final_summary(
        succeeded=True,
        attempts_used=1,
        max_attempts=1,
        total_duration=0.1,
        build_notes_path=build_notes_path,
    )

    output = buffer.getvalue()
    assert "Build Notes:" in output
    assert str(build_notes_path) in output
    assert "[cyan]" not in output


def test_cli_completion_prints_build_notes_path(tmp_path: Path) -> None:
    """AC-5: `issue` and `next` print the build notes path when present."""
    from typer.testing import CliRunner

    from acp_factory.cli import app

    build_notes_path = (
        tmp_path / ".factory" / "runs" / "run-1" / "implementation-notes.md"
    )
    outcome = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=0.0,
        build_notes_path=build_notes_path,
    )
    runner = CliRunner()

    with patch("acp_factory.cli.run_issue_stage", new=AsyncMock(return_value=outcome)):
        issue_res = runner.invoke(app, ["issue", "5", "--workspace", str(tmp_path)])
    assert issue_res.exit_code == 0
    assert "Build Notes:" in issue_res.output
    assert str(build_notes_path) in issue_res.output.replace("\n", "")

    task = TaskContext(issue_number=5, title="Build me", labels=["factory:build"])
    with (
        patch(
            "acp_factory.cli.FactoryConfig.discover",
            return_value=FactoryConfig(task="Build", active_stage="build"),
        ),
        patch(
            "acp_factory.cli.list_actionable_issues",
            new=AsyncMock(return_value=[task]),
        ),
        patch("acp_factory.cli.run_issue_stage", new=AsyncMock(return_value=outcome)),
    ):
        next_res = runner.invoke(app, ["next", "--workspace", str(tmp_path)])
    assert next_res.exit_code == 0
    assert "Build Notes:" in next_res.output
    assert str(build_notes_path) in next_res.output.replace("\n", "")


async def test_review_stage_injects_prior_build_notes(tmp_path: Path) -> None:
    """AC-6: `run_issue_stage` feeds latest build notes into the review context."""
    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory" / "latest-build-notes.md").write_text(
        "### Implementation Summary\n- **Files Modified**: `src/app.py`\n",
        encoding="utf-8",
    )
    task = TaskContext(issue_number=7, title="Review me", labels=["factory:review"])
    captured: list[FactoryConfig] = []

    async def fake_run_factory(
        config: FactoryConfig, workspace: Path, **kwargs: object
    ) -> FactoryRunOutcome:
        captured.append(config)
        return FactoryRunOutcome(
            succeeded=False,
            exit_code=1,
            attempts_used=1,
            total_duration_seconds=0.0,
            failure_reason="stop",
        )

    with (
        patch("acp_factory.runner.loop.fetch_issue", new=AsyncMock(return_value=task)),
        patch(
            "acp_factory.runner.loop.FactoryConfig.discover",
            return_value=FactoryConfig(task="base"),
        ),
        patch("acp_factory.runner.loop.run_factory", new=fake_run_factory),
        patch("acp_factory.runner.loop.transition_issue_label", new=AsyncMock()),
    ):
        outcome = await run_issue_stage(tmp_path, 7, stage_override="review")

    assert outcome.succeeded is False
    assert captured
    injected = captured[0].task
    assert "## Builder Implementation Notes" in injected
    assert "Treat claimed tests" in injected
    assert "- **Files Modified**: `src/app.py`" in injected


async def test_branch_review_injects_prior_build_notes(tmp_path: Path) -> None:
    """AC-6: `run_branch_review` resolves build notes via `runs/latest` fallback."""
    run_dir = tmp_path / ".factory" / "runs" / "run-x"
    run_dir.mkdir(parents=True)
    (run_dir / "implementation-notes.md").write_text(
        "### Implementation Summary\n- **Evidence**: `1 passed`\n",
        encoding="utf-8",
    )
    (tmp_path / ".factory" / "runs" / "latest").symlink_to(
        "run-x", target_is_directory=True
    )
    captured: list[FactoryConfig] = []

    async def fake_run_factory(
        config: FactoryConfig, workspace: Path, **kwargs: object
    ) -> FactoryRunOutcome:
        captured.append(config)
        return FactoryRunOutcome(
            succeeded=False,
            exit_code=1,
            attempts_used=1,
            total_duration_seconds=0.0,
        )

    with (
        patch(
            "acp_factory.runner.loop.FactoryConfig.discover",
            return_value=FactoryConfig(task="base"),
        ),
        patch("acp_factory.runner.loop.run_factory", new=fake_run_factory),
        patch(
            "acp_factory.runner.loop.get_head_sha",
            new=AsyncMock(return_value="abc1234"),
        ),
        patch(
            "acp_factory.runner.loop.is_worktree_clean",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "acp_factory.runner.worktree._run_git",
            new=AsyncMock(return_value=(0, "subject", "")),
        ),
    ):
        outcome = await run_branch_review(tmp_path, branch="feature", description="fix")

    assert outcome.succeeded is False
    assert captured
    injected = captured[0].task
    assert "## Builder Implementation Notes" in injected
    assert "- **Evidence**: `1 passed`" in injected
