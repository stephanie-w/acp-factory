"""Unit tests for turn event tracking, transcript serialization, and condensed JSON output."""

from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
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


@pytest.mark.parametrize(
    (
        "thought",
        "thought_max_chars",
        "expect_thinking_section",
        "expected_md_substrings",
        "unexpected_md_substrings",
        "expected_preview_prefix",
        "expected_preview_suffix",
    ),
    [
        (
            "Internal reasoning that must not leak into output artifacts.",
            0,
            False,
            [],
            [],
            "",
            "",
        ),
        (
            "A" * 40 + "B" * 40,
            50,
            True,
            ["A" * 40 + "B" * 10, "[truncated 30 chars]"],
            ["B" * 40],
            "A" * 40 + "B" * 10,
            "[truncated 30 chars]",
        ),
        (
            "T" * 600,
            None,
            True,
            ["T" * 600],
            ["[truncated"],
            "T" * 500,
            "[truncated 100 chars]",
        ),
    ],
    ids=["zero", "truncated", "none_default"],
)
def test_thought_max_chars(
    thought: str,
    thought_max_chars: int | None,
    expect_thinking_section: bool,
    expected_md_substrings: list[str],
    unexpected_md_substrings: list[str],
    expected_preview_prefix: str,
    expected_preview_suffix: str,
) -> None:
    """thought_max_chars controls markdown/summary truncation while keeping transcript.json full."""
    tracker = TurnEventTracker(
        initial_prompt="Initial prompt", thought_max_chars=thought_max_chars
    )
    tracker.begin_turn("Turn 1")
    tracker.recorder.add_thought(thought)
    tracker.recorder.add_message("Done.")

    summary = tracker.end_turn("end_turn")
    markdown = summary.transcript_markdown

    assert ("## Thinking" in markdown) is expect_thinking_section
    for sub in expected_md_substrings:
        assert sub in markdown
    for sub in unexpected_md_substrings:
        assert sub not in markdown

    assert summary.to_dict()["thought_text"] == thought
    assert summary.thought_text == thought
    assert tracker.recorder.thoughts == [thought]

    preview = summary.to_condensed_dict()["thought_preview"]
    if expected_preview_prefix:
        assert preview.startswith(expected_preview_prefix)
        assert expected_preview_suffix in preview
    else:
        assert preview == ""


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


def test_turn_event_tracker_resets_usage_between_turns() -> None:
    tracker = TurnEventTracker(initial_prompt="Initial prompt")
    tracker.begin_turn("Turn 1")
    tracker.recorder.set_usage({"used": 500, "cost": {"amount": 0.001}})
    turn1 = tracker.end_turn("end_turn")
    assert turn1.tokens == 500
    assert turn1.cost_usd == 0.001

    tracker.begin_turn("Turn 2")
    turn2 = tracker.end_turn("end_turn")
    assert turn2.usage is None
    assert turn2.tokens == 0
    assert turn2.cost_usd == 0.0
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
    assert isinstance(condensed_list, list)
    assert len(condensed_list) < len(long_list)  # truncated
    last = condensed_list[-1]
    assert isinstance(last, str)
    assert "omitted" in last

    # Test text truncation
    short = _truncate_text("hello", 10)
    assert short == "hello"
    long_txt = _truncate_text("hello world long text", 5)
    assert long_txt.startswith("hello")
    assert "truncated" in long_txt
    assert len(long_txt) > 5  # notice appended beyond the cap


def _persist(
    workspace: Path,
    *,
    config: FactoryConfig,
    run_id: str,
    response_text: str = "",
    succeeded: bool = True,
    attempts_used: int = 1,
    turn_transcripts: list[str] | None = None,
) -> Path | None:
    if turn_transcripts is None:
        turn_transcripts = ["# Attempt 1 Markdown"] if attempts_used > 0 else []
    return _persist_run_artifacts(
        workspace,
        run_id,
        config,
        succeeded=succeeded,
        attempts_used=attempts_used,
        total_duration=0.5,
        setup_logs=[],
        turn_transcripts=turn_transcripts,
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


@pytest.mark.parametrize(
    ("stage", "artifact_name", "pointer_name", "response_text"),
    [
        (
            "review",
            "report.md",
            "latest-report.md",
            "# Review Report\n\n## Findings\n- None.\n\nVERDICT: ACCEPTED\n",
        ),
        (
            "build",
            "implementation-notes.md",
            "latest-build-notes.md",
            "Sure, here is a walkthrough of my work.\n\n"
            "### Implementation Summary\n"
            "- **Files Modified**: `src/app.py`\n"
            "### Verification\n"
            "- **After**: `1 passed`\n"
            "STATUS: DONE\n",
        ),
    ],
    ids=["review_report", "build_notes"],
)
def test_latest_artifact_pointer_symlink_and_fallback(
    tmp_path: Path,
    stage: str,
    artifact_name: str,
    pointer_name: str,
    response_text: str,
) -> None:
    """Latest artifact pointers are relative symlinks that fall back to a copy on OSError."""
    (tmp_path / ".factory").mkdir()
    config = FactoryConfig(task=f"Run {stage}", active_stage=stage)

    run_dir = _persist(
        tmp_path, config=config, run_id="run-abc", response_text=response_text
    )
    assert run_dir is not None

    latest_run = tmp_path / ".factory" / "runs" / "latest"
    latest_pointer = tmp_path / ".factory" / pointer_name
    assert latest_run.is_symlink()
    assert os.readlink(latest_run) == "run-abc"
    assert latest_pointer.is_symlink()
    assert os.readlink(latest_pointer) == str(Path("runs") / "run-abc" / artifact_name)
    assert latest_pointer.read_text(encoding="utf-8") == (
        run_dir / artifact_name
    ).read_text(encoding="utf-8")

    with patch.object(Path, "symlink_to", side_effect=OSError("symlinks disabled")):
        fallback_dir = _persist(
            tmp_path, config=config, run_id="run-def", response_text=response_text
        )
    assert fallback_dir is not None
    assert latest_pointer.exists()
    assert not latest_pointer.is_symlink()
    assert latest_pointer.read_text(encoding="utf-8") == (
        fallback_dir / artifact_name
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
    assert os.readlink(tmp_path / ".factory" / "runs" / "latest") == "run-build"

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


@pytest.mark.parametrize(
    "stage",
    ["triage", "spec", "build", "review", "retro", None],
    ids=["triage", "spec", "build", "review", "retro", "ad_hoc"],
)
def test_persist_run_artifacts_updates_latest_symlink_for_all_stages(
    tmp_path: Path, stage: str | None
) -> None:
    """AC-1: every completed run refreshes `.factory/runs/latest`."""
    (tmp_path / ".factory").mkdir()
    config = FactoryConfig(task="Run stage", active_stage=stage)

    run_dir = _persist(tmp_path, config=config, run_id="run-newest")

    assert run_dir is not None
    latest = tmp_path / ".factory" / "runs" / "latest"
    assert latest.is_symlink()
    assert os.readlink(latest) == "run-newest"


@pytest.mark.parametrize(
    ("dry_run", "attempts_used"),
    [(True, 1), (False, 0)],
    ids=["dry_run", "setup_failure"],
)
def test_persist_run_artifacts_dry_run_or_setup_failure(
    tmp_path: Path, dry_run: bool, attempts_used: int
) -> None:
    """AC-3: dry runs and setup failures leave any existing latest pointer untouched."""
    (tmp_path / ".factory").mkdir()
    config = FactoryConfig(task="Run stage", active_stage="triage", dry_run=dry_run)

    run_dir = _persist(
        tmp_path, config=config, run_id="run-skipped", attempts_used=attempts_used
    )

    assert run_dir is not None
    assert not (tmp_path / ".factory" / "runs" / "latest").exists()


def test_persist_run_artifacts_keeps_latest_when_first_turn_produced_no_transcript(
    tmp_path: Path,
) -> None:
    """AC-3: a first-turn timeout (attempts_used=1, no transcript) leaves latest intact."""
    runs_dir = tmp_path / ".factory" / "runs"
    (runs_dir / "run-old").mkdir(parents=True)
    latest = runs_dir / "latest"
    latest.symlink_to("run-old", target_is_directory=True)
    config = FactoryConfig(task="Run stage", active_stage="triage")

    run_dir = _persist(
        tmp_path,
        config=config,
        run_id="run-timeout",
        attempts_used=1,
        turn_transcripts=[],
    )

    assert run_dir is not None
    assert os.readlink(latest) == "run-old"


@pytest.mark.parametrize(
    ("stage", "attr_name", "filename", "expected_text", "response_text"),
    [
        (
            "review",
            "report_path",
            "report.md",
            "VERDICT: ACCEPTED",
            "# Review Report\n\nAll good.\n\nVERDICT: ACCEPTED",
        ),
        (
            "build",
            "build_notes_path",
            "implementation-notes.md",
            "### Implementation Summary",
            "Sure, here is a walkthrough of my work.\n\n"
            "### Implementation Summary\n"
            "- **Files Modified**: `src/app.py`\n"
            "### Verification\n"
            "- **After**: `1 passed`\n"
            "STATUS: DONE\n",
        ),
    ],
    ids=["review_report", "build_notes"],
)
async def test_factory_run_outcome_artifact_path(
    tmp_path: Path,
    stage: str,
    attr_name: str,
    filename: str,
    expected_text: str,
    response_text: str,
) -> None:
    """`run_factory` exposes the persisted stage artifact path on the outcome."""
    (tmp_path / ".factory").mkdir()
    config = FactoryConfig(task=f"Run {stage}", active_stage=stage)

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.tracker = AsyncMock()
            self.tracker.history = []

        async def __aenter__(self) -> FakeClient:
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
                        response_text=response_text,
                        thought_text="",
                        stop_reason="end_turn",
                        duration_seconds=0.01,
                        tool_call_count=0,
                    ),
                },
            )()

    with patch("acp_factory.runner.loop.FactoryACPClient", FakeClient):
        outcome = await run_factory(config, tmp_path)

    assert outcome.succeeded is True
    run_dir = outcome.run_dir
    assert run_dir is not None
    artifact_path = getattr(outcome, attr_name)
    assert artifact_path == run_dir / filename
    assert artifact_path.is_file()
    assert expected_text in artifact_path.read_text(encoding="utf-8")

    # The field defaults to None for callers that construct outcomes directly.
    default_outcome = FactoryRunOutcome(
        succeeded=True, exit_code=0, attempts_used=1, total_duration_seconds=0.0
    )
    assert getattr(default_outcome, attr_name) is None


@pytest.mark.parametrize(
    ("kwarg_name", "label", "rel_path"),
    [
        ("report_path", "Report:", Path(".factory/runs/run-1/report.md")),
        (
            "build_notes_path",
            "Build Notes:",
            Path(".factory/runs/run-1/implementation-notes.md"),
        ),
    ],
    ids=["report_path", "build_notes_path"],
)
def test_show_final_summary_displays_artifact_path(
    kwarg_name: str, label: str, rel_path: Path
) -> None:
    """The final summary grid surfaces report_path and build_notes_path."""
    buffer = StringIO()
    display = FactoryDisplay(Console(file=buffer, width=200, highlight=False))

    display.show_final_summary(
        succeeded=True,
        attempts_used=1,
        max_attempts=1,
        total_duration=0.1,
        report_path=rel_path if kwarg_name == "report_path" else None,
        build_notes_path=rel_path if kwarg_name == "build_notes_path" else None,
    )

    output = buffer.getvalue()
    assert label in output
    assert str(rel_path) in output
    assert "[cyan]" not in output


@pytest.mark.parametrize(
    ("stage", "kwarg_name", "filename", "label", "include_review_cmd"),
    [
        ("review", "report_path", "report.md", "Review Report:", True),
        (
            "build",
            "build_notes_path",
            "implementation-notes.md",
            "Build Notes:",
            False,
        ),
    ],
    ids=["review_report", "build_notes"],
)
def test_cli_completion_prints_artifact_path(
    tmp_path: Path,
    stage: str,
    kwarg_name: str,
    filename: str,
    label: str,
    include_review_cmd: bool,
) -> None:
    """CLI commands print the artifact path when present on the outcome."""
    from typer.testing import CliRunner

    from acp_factory.cli import app

    artifact_path = tmp_path / ".factory" / "runs" / "run-1" / filename
    verdict = (
        StageVerdict(
            stage="review",
            status="accepted",
            target_label="factory:pr-ready",
            updated_factory_block="",
            summary="ok",
        )
        if stage == "review"
        else None
    )
    outcome = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=0.0,
        stage_verdict=verdict,
        report_path=artifact_path if kwarg_name == "report_path" else None,
        build_notes_path=artifact_path if kwarg_name == "build_notes_path" else None,
    )
    runner = CliRunner()

    with patch("acp_factory.cli.run_issue_stage", new=AsyncMock(return_value=outcome)):
        issue_res = runner.invoke(app, ["issue", "5", "--workspace", str(tmp_path)])
    assert issue_res.exit_code == 0
    assert label in issue_res.output
    assert str(artifact_path) in issue_res.output.replace("\n", "")

    if include_review_cmd:
        with patch(
            "acp_factory.cli.run_branch_review", new=AsyncMock(return_value=outcome)
        ):
            review_res = runner.invoke(app, ["review", "--workspace", str(tmp_path)])
        assert review_res.exit_code == 0
        assert label in review_res.output
        assert str(artifact_path) in review_res.output.replace("\n", "")

    task = TaskContext(issue_number=5, title="Task", labels=[f"factory:{stage}"])
    with (
        patch(
            "acp_factory.cli.FactoryConfig.discover",
            return_value=FactoryConfig(task=f"Run {stage}", active_stage=stage),
        ),
        patch(
            "acp_factory.cli.list_actionable_issues",
            new=AsyncMock(return_value=[task]),
        ),
        patch("acp_factory.cli.run_issue_stage", new=AsyncMock(return_value=outcome)),
    ):
        next_res = runner.invoke(app, ["next", "--workspace", str(tmp_path)])
    assert next_res.exit_code == 0
    assert label in next_res.output
    assert str(artifact_path) in next_res.output.replace("\n", "")


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
    assert os.readlink(plain_ws / ".factory" / "runs" / "latest") == "run-build-plain"


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


def test_agent_turn_status_quiet_mode_only() -> None:
    """agent_turn_status uses Status only when display.quiet is True and enabled."""
    from contextlib import nullcontext

    from rich.status import Status

    buffer = StringIO()
    normal_display = FactoryDisplay(Console(file=buffer, width=120, highlight=False))
    assert isinstance(normal_display.agent_turn_status(enabled=True), nullcontext)

    quiet_display = FactoryDisplay(
        Console(file=buffer, width=120, highlight=False), quiet=True
    )
    assert isinstance(quiet_display.agent_turn_status(enabled=False), nullcontext)

    enabled_cm = quiet_display.agent_turn_status(enabled=True, message="Working...")
    assert isinstance(enabled_cm, Status)
    with enabled_cm:
        pass


def test_show_response_preview_truncates() -> None:
    """show_response_preview renders a compact truncated panel."""
    buffer = StringIO()
    display = FactoryDisplay(
        Console(file=buffer, width=120, highlight=False), quiet=True
    )

    long_response = "\n".join(f"line {i}" for i in range(1, 25))
    display.show_response_preview(long_response, max_lines=5, max_chars=200)

    output = buffer.getvalue()
    assert "Agent Response (truncated)" in output
    assert "line 1" in output
    assert "line 5" in output
    assert "line 6" not in output
    assert "... (truncated)" in output


def test_cli_quiet_flag_passes_quiet_display(tmp_path: Path) -> None:
    """Passing -q/--quiet passes a quiet FactoryDisplay to run_issue_stage."""
    from typer.testing import CliRunner

    from acp_factory.cli import app
    from acp_factory.runner.loop import FactoryRunOutcome

    captured_kwargs: list[dict[str, object]] = []

    async def fake_run_issue_stage(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return FactoryRunOutcome(
            succeeded=True,
            exit_code=0,
            attempts_used=1,
            total_duration_seconds=0.1,
        )

    runner = CliRunner()
    with patch("acp_factory.cli.run_issue_stage", new=fake_run_issue_stage):
        res = runner.invoke(app, ["-q", "issue", "19", "--workspace", str(tmp_path)])

    assert res.exit_code == 0
    assert len(captured_kwargs) == 1
    disp = captured_kwargs[0].get("display")
    assert isinstance(disp, FactoryDisplay)
    assert disp.quiet is True
