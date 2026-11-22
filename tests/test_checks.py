"""Unit and integration tests for acp_factory.runner (checks, truncation, and retry loop)."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from acp_factory.acp.client import TurnResult
from acp_factory.acp.events import TurnSummary
from acp_factory.config import CheckConfig, FactoryConfig, RetryConfig
from acp_factory.runner.checks import (
    build_workspace_env,
    parse_review_verdict,
    run_checks_sequential,
    run_shell_command,
    truncate_check_output,
)
from acp_factory.runner.loop import build_retry_prompt, run_factory


def test_truncate_check_output_preserves_head_and_tail() -> None:
    lines = [f"line {i}" for i in range(1, 301)]
    raw = "\n".join(lines)
    truncated = truncate_check_output(raw, max_lines=50)

    assert "line 1" in truncated
    assert "line 300" in truncated
    assert "truncated 250 lines" in truncated
    assert "line 100" not in truncated


def test_truncate_check_output_caps_chars_on_wide_lines() -> None:
    wide = "A" * 8000 + "MIDDLE_NOISE" + "Z" * 8000
    truncated = truncate_check_output(wide, max_lines=150, max_chars=1000)

    assert truncated.startswith("A" * 600)
    assert truncated.endswith("Z" * 400)
    assert "truncated" in truncated
    assert "characters of output" in truncated
    assert "MIDDLE_NOISE" not in truncated


def test_truncate_check_output_spill_on_lines(tmp_path: Path) -> None:
    lines = [f"line {i}" for i in range(1, 301)]
    raw = "\n".join(lines)

    truncated = truncate_check_output(raw, max_lines=50, spill_dir=tmp_path)

    spill_files = list(tmp_path.iterdir())
    assert len(spill_files) == 1
    spill_path = spill_files[0]
    assert spill_path.read_text(encoding="utf-8") == raw
    assert stat.S_IMODE(spill_path.stat().st_mode) == 0o600
    assert f"Full output saved to {spill_path}" in truncated
    assert "use rg/read to inspect omitted lines" in truncated


def test_truncate_check_output_spill_on_chars(tmp_path: Path) -> None:
    raw = "A" * 8000 + "MIDDLE_NOISE" + "Z" * 8000

    truncated = truncate_check_output(
        raw, max_lines=150, max_chars=1000, spill_dir=tmp_path
    )

    spill_files = list(tmp_path.iterdir())
    assert len(spill_files) == 1
    spill_path = spill_files[0]
    assert spill_path.read_text(encoding="utf-8") == raw
    assert stat.S_IMODE(spill_path.stat().st_mode) == 0o600
    assert f"Full output saved to {spill_path}" in truncated


def test_truncate_check_output_no_spill_when_within_bounds(tmp_path: Path) -> None:
    within_bounds = "\n".join(f"line {i}" for i in range(1, 6))

    result = truncate_check_output(
        within_bounds, max_lines=50, max_chars=1000, spill_dir=tmp_path
    )

    assert result == within_bounds
    assert list(tmp_path.iterdir()) == []

    large = "\n".join(f"line {i}" for i in range(1, 301))
    no_spill_dir = truncate_check_output(large, max_lines=50, spill_dir=None)
    assert "Full output saved to" not in no_spill_dir


async def test_run_checks_sequential_populates_spill_path(tmp_path: Path) -> None:
    script = tmp_path / "big_failure.py"
    script.write_text(
        "import sys\nfor i in range(200):\n    print(f'line {i}')\nsys.exit(3)\n",
        encoding="utf-8",
    )
    checks = [CheckConfig(name="Big Fail", cmd=f"python3 {script}")]
    spill_dir = tmp_path / "spill"

    all_passed, results = await run_checks_sequential(
        checks, tmp_path, max_output_lines=10, spill_dir=spill_dir
    )

    assert all_passed is False
    result = results[0]
    assert result.passed is False
    assert result.spill_path is not None
    assert result.spill_path.is_file()
    assert result.spill_path.parent == spill_dir
    assert "line 199" in result.spill_path.read_text(encoding="utf-8")
    assert f"Full output saved to {result.spill_path}" in result.truncated_output


def test_parse_review_verdict() -> None:
    assert parse_review_verdict("All requirements met.\nVERDICT: PASS") is True
    assert parse_review_verdict("Missing unit test.\nVERDICT: FAIL") is False
    assert parse_review_verdict("Everything looks good.\nPASS") is True
    assert parse_review_verdict("Broken import.\nFAIL") is False


async def test_run_checks_sequential_fail_fast(tmp_path: Path) -> None:
    marker_file = tmp_path / "third_ran.txt"
    checks = [
        CheckConfig(name="Pass 1", cmd="python3 -c 'print(\"ok 1\")'"),
        CheckConfig(
            name="Fail 2",
            cmd="python3 -c 'import sys; print(\"boom\"); sys.exit(2)'",
        ),
        CheckConfig(
            name="Should Not Run 3",
            cmd=f"python3 -c 'from pathlib import Path; Path({str(marker_file)!r}).write_text(\"ran\")'",
        ),
    ]

    all_passed, results = await run_checks_sequential(checks, tmp_path)
    assert all_passed is False
    # Fail-fast: stops at check #2 and never executes check #3
    assert len(results) == 2
    assert results[0].passed is True
    assert results[1].passed is False
    assert results[1].exit_code == 2
    assert "boom" in results[1].raw_output
    assert not marker_file.exists()


async def test_sequential_checks_response_file_isolation(tmp_path: Path) -> None:
    original = (
        "Here is the combined review.\n\n"
        "### Risk Classification: MEDIUM\n"
        "- **Reason**: Logic change.\n\n"
        "### Findings\n"
        "- None.\n\n"
        "### Security Audit Summary\n"
        "- **Findings Count**: `0 blocking, 0 major, 0 minor`\n\n"
        "### Security Findings\n"
        "- None.\n\n"
        "STATUS: ACCEPTED\n"
    )
    response_file = tmp_path / "response.md"
    response_file.write_text(original, encoding="utf-8")

    review_script = tmp_path / "review_check.py"
    review_script.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "p = Path(os.environ['FACTORY_RESPONSE_FILE'])\n"
        "text = p.read_text(encoding='utf-8')\n"
        "assert '### Risk Classification: MEDIUM' in text\n"
        "assert '### Security Audit Summary' in text\n"
        "start = text.index('### Risk Classification')\n"
        "p.write_text(text[start:], encoding='utf-8')\n"
        f"Path({str(response_file)!r}).write_text('tampered by check 1', encoding='utf-8')\n",
        encoding="utf-8",
    )
    security_script = tmp_path / "security_check.py"
    security_script.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "p = Path(os.environ['FACTORY_RESPONSE_FILE'])\n"
        "text = p.read_text(encoding='utf-8')\n"
        "assert '### Risk Classification: MEDIUM' in text\n"
        "assert '### Security Audit Summary' in text\n"
        "start = text.index('### Security Audit Summary')\n"
        "p.write_text(text[start:], encoding='utf-8')\n",
        encoding="utf-8",
    )

    checks = [
        CheckConfig(name="validate-review", cmd=f"python3 {review_script}"),
        CheckConfig(name="validate-security", cmd=f"python3 {security_script}"),
    ]
    extra_env = {"FACTORY_RESPONSE_FILE": str(response_file)}

    all_passed, results = await run_checks_sequential(
        checks, tmp_path, extra_env=extra_env
    )

    assert all_passed is True
    assert [result.passed for result in results] == [True, True]
    # Check 1 mutated the original path directly; the snapshot must be restored.
    assert response_file.read_text(encoding="utf-8") == original
    # Per-check copies are cleaned up and the caller's env is not mutated.
    assert list(tmp_path.glob("check-*")) == []
    assert extra_env["FACTORY_RESPONSE_FILE"] == str(response_file)


async def test_run_review_prompt_check(tmp_path: Path) -> None:
    mock_client = AsyncMock()
    mock_client.send_prompt.return_value = TurnResult(
        stop_reason="end_turn",
        summary=TurnSummary(
            prompt="review",
            response_text="Checked implementation thoroughly.\nVERDICT: PASS",
            thought_text="",
            stop_reason="end_turn",
            duration_seconds=0.1,
            tool_call_count=0,
        ),
    )

    checks = [CheckConfig(name="Architecture Review", prompt="Check module structure")]
    all_passed, results = await run_checks_sequential(
        checks, tmp_path, acp_client=mock_client
    )
    assert all_passed is True
    assert len(results) == 1
    assert results[0].check_type == "prompt"
    assert results[0].passed is True


async def test_feedback_loop_recovers_on_second_attempt(tmp_path: Path) -> None:
    target_file = tmp_path / "solution.py"
    target_file.write_text("VALUE = 1\n", encoding="utf-8")

    cfg = FactoryConfig(
        task="Set VALUE = 42 in solution.py",
        checks=[
            CheckConfig(
                name="Verify VALUE == 42",
                cmd="python3 -c 'import solution; assert solution.VALUE == 42, f\"Expected 42, got {solution.VALUE}\"'",
            )
        ],
        retry=RetryConfig(max_attempts=3),
    )

    prompts_received: list[str] = []

    class FakeACPClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.tracker = AsyncMock()
            self.tracker.history = []

        async def __aenter__(self) -> FakeACPClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def send_prompt(self, prompt: str, **kwargs: object) -> TurnResult:
            prompts_received.append(prompt)
            # On attempt 1, leave bug; on attempt 2, fix it after receiving feedback!
            if len(prompts_received) == 2:
                target_file.write_text("VALUE = 42\n", encoding="utf-8")
            return TurnResult(
                stop_reason="end_turn",
                summary=TurnSummary(
                    prompt=prompt,
                    response_text="Updated solution.py",
                    thought_text="",
                    stop_reason="end_turn",
                    duration_seconds=0.05,
                    tool_call_count=1,
                ),
            )

    with patch("acp_factory.runner.loop.FactoryACPClient", FakeACPClient):
        outcome = await run_factory(cfg, tmp_path)

    assert outcome.succeeded is True
    assert outcome.exit_code == 0
    assert outcome.attempts_used == 2
    assert len(prompts_received) == 2
    assert "Retry Attempt 2 of 3" in prompts_received[1]
    assert "Expected 42, got 1" in prompts_received[1]


EMPTY_OUTPUT_REASON = "Agent produced no output (session aborted or failed)"


def _no_check_config() -> FactoryConfig:
    return FactoryConfig(task="Write specification", active_stage="spec")


def _fake_client_returning(response_text: str) -> type:
    class FakeNoCheckACPClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.tracker = AsyncMock()
            self.tracker.history = []

        async def __aenter__(self) -> FakeNoCheckACPClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def send_prompt(self, prompt: str, **kwargs: object) -> TurnResult:
            return TurnResult(
                stop_reason="end_turn",
                summary=TurnSummary(
                    prompt=prompt,
                    response_text=response_text,
                    thought_text="",
                    stop_reason="end_turn",
                    duration_seconds=0.01,
                    tool_call_count=0,
                ),
            )

    return FakeNoCheckACPClient


async def test_run_factory_empty_response_fails_fast(tmp_path: Path) -> None:
    for response_text in ("", "   \n\t "):
        with (
            patch(
                "acp_factory.runner.loop.FactoryACPClient",
                _fake_client_returning(response_text),
            ),
            patch("acp_factory.runner.loop.logger") as mock_logger,
        ):
            outcome = await run_factory(_no_check_config(), tmp_path)

        assert outcome.succeeded is False
        assert outcome.exit_code == 1
        assert outcome.failure_reason == EMPTY_OUTPUT_REASON
        mock_logger.error.assert_called_once_with(EMPTY_OUTPUT_REASON)


async def test_run_factory_no_checks_success(tmp_path: Path) -> None:
    with patch(
        "acp_factory.runner.loop.FactoryACPClient",
        _fake_client_returning("Specification complete.\nSTATUS: SPEC_READY"),
    ):
        outcome = await run_factory(_no_check_config(), tmp_path)

    assert outcome.succeeded is True
    assert outcome.exit_code == 0
    assert outcome.failure_reason is None


def test_build_retry_prompt_includes_context(tmp_path: Path) -> None:
    from acp_factory.runner.checks import CheckResult
    from acp_factory.runner.loop import build_initial_prompt

    ro_prompt = build_initial_prompt(
        "Write spec",
        [CheckConfig(name="validate-spec", cmd="uv run validate_spec.py")],
        read_only=True,
    )
    assert "do not write this file yourself" in ro_prompt
    assert "$FACTORY_RESPONSE_FILE" in ro_prompt
    assert "Execution Mode: Autonomous & Non-Interactive" in ro_prompt
    assert "AGENTS.md" in ro_prompt
    assert "strictly superseded" in ro_prompt
    assert "GUARDRAIL DENIED" in ro_prompt

    mutating_prompt = build_initial_prompt(
        "Build feature",
        [CheckConfig(name="test", cmd="pytest")],
        read_only=False,
        workspace=tmp_path,
    )
    assert "Active workspace:" not in ro_prompt
    assert f"Active workspace: `{tmp_path.resolve()}`" in mutating_prompt
    assert "Use workspace-relative paths" in mutating_prompt
    assert "Complete all necessary research, file modifications" in mutating_prompt
    assert "Do NOT pause to ask clarifying questions" in mutating_prompt
    assert "GUARDRAIL DENIED" in mutating_prompt
    assert "Tool & Token Discipline" in mutating_prompt
    assert "Do not narrate tool calls" in mutating_prompt
    assert "Batch parallel tool calls" in mutating_prompt
    assert "targeted, windowed file reads" in mutating_prompt
    assert "git diff --stat" in mutating_prompt
    assert "omit pleasantries, preambles" in mutating_prompt

    results = [
        CheckResult(
            name="Lint",
            check_type="cmd",
            command_or_prompt="ruff check .",
            passed=True,
            exit_code=0,
            raw_output="",
            truncated_output="",
            duration_seconds=0.1,
        ),
        CheckResult(
            name="Pytest",
            check_type="cmd",
            command_or_prompt="pytest",
            passed=False,
            exit_code=1,
            raw_output="AssertionError: 1 != 2",
            truncated_output="AssertionError: 1 != 2",
            duration_seconds=0.3,
        ),
    ]
    prompt = build_retry_prompt("Fix math module", 2, 3, results)
    assert "Previously Passing Checks" in prompt
    assert "Lint" in prompt
    assert "Pytest" in prompt
    assert "AssertionError: 1 != 2" in prompt
    assert "Remember: You are executing autonomously" in prompt
    assert "apply all changes directly to the workspace" in prompt
    assert "Do not narrate tool calls" in prompt
    assert "batch independent tool calls in parallel" in prompt


def test_build_retry_prompt_for_response_file_check() -> None:
    from acp_factory.runner.checks import CheckResult

    results = [
        CheckResult(
            name="Validate triage output",
            check_type="cmd",
            command_or_prompt='uv run --frozen python "$FACTORY_SKILLS_DIR/issue-triage/scripts/validate_triage.py" "$FACTORY_RESPONSE_FILE"',
            passed=False,
            exit_code=1,
            raw_output="Missing '### Codebase Inspection' heading.",
            truncated_output="Missing '### Codebase Inspection' heading.",
            duration_seconds=0.2,
        )
    ]
    prompt = build_retry_prompt(
        "Triage issue #10",
        attempt=2,
        max_attempts=3,
        check_results=results,
        captured_response="### Summary\nI looked at the repo.\nSTATUS: OK",
    )
    assert "Captured Response (`$FACTORY_RESPONSE_FILE`)" in prompt
    assert "### Summary\nI looked at the repo.\nSTATUS: OK" in prompt
    assert "Do NOT inspect orchestrator/runner code" in prompt
    assert "apply all changes directly to the workspace" not in prompt


async def test_spec_validation_check_self_heals_via_factory_response_file(
    tmp_path: Path,
) -> None:
    cfg = FactoryConfig(
        task="Write specification for token auth",
        active_stage="spec",
        checks=[
            CheckConfig(
                name="validate-spec",
                cmd='uv run "$FACTORY_BUNDLED_SKILLS_DIR/spec-writer/scripts/validate_spec.py" "$FACTORY_RESPONSE_FILE"',
            )
        ],
        retry=RetryConfig(max_attempts=3),
    )

    prompts_received: list[str] = []

    class FakeSpecACPClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.tracker = AsyncMock()
            self.tracker.history = []

        async def __aenter__(self) -> FakeSpecACPClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def send_prompt(self, prompt: str, **kwargs: object) -> TurnResult:
            prompts_received.append(prompt)
            if len(prompts_received) == 1:
                # Attempt 1: unformatted monologue without required sections or STATUS
                text = (
                    "I'll explore the codebase first.\n\n"
                    "Before finalizing, I have a question:\n"
                    "**[1]** Which token env var should we use? (Answer: 1=a or 1=b)"
                )
            else:
                # Attempt 2: properly formatted spec after receiving validation error feedback
                text = (
                    "Here is the corrected specification:\n\n"
                    "# Specification\n\n"
                    "## Problem\nMissing token auth.\n\n"
                    "## Behavior\n1. Read token from env.\n\n"
                    "## Files to Touch\n- `src/acp_factory/config.py`\n\n"
                    "## Decisions\n- None yet.\n\n"
                    "## Assumptions\n- Standard env var lookup.\n\n"
                    "## Acceptance Criteria\n"
                    "| ID | Done when | How to check |\n"
                    "| :--- | :--- | :--- |\n"
                    "| AC-1 | Token is loaded | `uv run pytest` |\n\n"
                    "## Out of Scope\n- OAuth flows.\n\n"
                    "STATUS: SPEC_READY\n"
                )
            return TurnResult(
                stop_reason="end_turn",
                summary=TurnSummary(
                    prompt=prompt,
                    response_text=text,
                    thought_text="",
                    stop_reason="end_turn",
                    duration_seconds=0.05,
                    tool_call_count=0,
                ),
            )

    with patch("acp_factory.runner.loop.FactoryACPClient", FakeSpecACPClient):
        outcome = await run_factory(cfg, tmp_path)

    assert outcome.succeeded is True
    assert outcome.attempts_used == 2
    assert len(prompts_received) == 2
    assert "Specification validation failed" in prompts_received[1]
    # The runner keeps the raw agent turn response instead of the validator's
    # mutated copy of $FACTORY_RESPONSE_FILE; markdown cleanup is deferred to
    # `_clean_markdown_stage_body` when the verdict is parsed.
    assert outcome.last_response_text.startswith("Here is the corrected specification:")
    assert "# Specification" in outcome.last_response_text
    assert outcome.last_response_text.rstrip().endswith("STATUS: SPEC_READY")


def test_all_skill_validators_and_templates_exist_and_work(tmp_path: Path) -> None:
    import subprocess

    from acp_factory.resources import get_bundled_resources_dir

    resources_root = get_bundled_resources_dir()
    skills_dir = resources_root / "skills"

    # 1. Triage validator
    triage_file = tmp_path / "triage.md"
    triage_file.write_text(
        "Let me check the repo.\n\n"
        "### Codebase Inspection\n"
        "- **Touched Area**: `src/acp_factory/cli.py`\n"
        "- **Current Behavior & Root Cause**: Missing flag.\n"
        "- **Duplicates / Related Issues**: `None`\n"
        "- **Estimated Scope**: `Small (~30 lines across 1 file)`\n"
        "- **Verification Tooling Available**: `uv run pytest`\n\n"
        "STATUS: OK\n",
        encoding="utf-8",
    )
    res_triage = subprocess.run(
        [
            "python3",
            str(skills_dir / "issue-triage" / "scripts" / "validate_triage.py"),
            str(triage_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_triage.returncode == 0
    assert triage_file.read_text(encoding="utf-8").startswith("### Codebase Inspection")

    # 1b. Triage validator with TRIVIAL (requires ### Acceptance Criteria)
    trivial_missing_ac = tmp_path / "triage_trivial_bad.md"
    trivial_missing_ac.write_text(
        "### Codebase Inspection\n"
        "- **Touched Area**: `src/acp_factory/cli.py`\n"
        "- **Current Behavior & Root Cause**: Typo in help text.\n"
        "- **Duplicates / Related Issues**: `None`\n"
        "- **Estimated Scope**: `Small (~2 lines across 1 file)`\n"
        "- **Verification Tooling Available**: `just test-fast`\n\n"
        "STATUS: TRIVIAL\n",
        encoding="utf-8",
    )
    res_trivial_bad = subprocess.run(
        [
            "python3",
            str(skills_dir / "issue-triage" / "scripts" / "validate_triage.py"),
            str(trivial_missing_ac),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_trivial_bad.returncode != 0
    assert "Acceptance Criteria" in res_trivial_bad.stderr

    trivial_valid = tmp_path / "triage_trivial_ok.md"
    trivial_valid.write_text(
        trivial_missing_ac.read_text(encoding="utf-8").replace(
            "STATUS: TRIVIAL",
            "### Acceptance Criteria\n"
            "- **AC-1**: Fix help text typo.\n"
            "- **Verification**: `just test-fast`\n\n"
            "STATUS: TRIVIAL",
        ),
        encoding="utf-8",
    )
    res_trivial_ok = subprocess.run(
        [
            "python3",
            str(skills_dir / "issue-triage" / "scripts" / "validate_triage.py"),
            str(trivial_valid),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_trivial_ok.returncode == 0

    # 2. Review validator
    review_file = tmp_path / "review.md"
    review_file.write_text(
        "### Risk Classification: LOW\n"
        "- **Reason**: Test-only update.\n\n"
        "### Acceptance Criteria Verification\n"
        "| ID | Status | Evidence |\n"
        "| :--- | :--- | :--- |\n"
        "| AC-1 | PASS | `tests/test_checks.py:1` |\n\n"
        "### Findings\n"
        "- None.\n\n"
        "STATUS: ACCEPTED\n",
        encoding="utf-8",
    )
    res_review = subprocess.run(
        [
            "python3",
            str(skills_dir / "code-reviewer" / "scripts" / "validate_review.py"),
            str(review_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_review.returncode == 0

    # Contradictory review (ACCEPTED with [blocking] finding) must fail
    bad_review = tmp_path / "bad_review.md"
    bad_review.write_text(
        "### Risk Classification: MEDIUM\n"
        "- **Reason**: Logic change.\n\n"
        "### Findings\n"
        "1. `[blocking]` `[unambiguous]` `src/app.py:10`\n"
        "   - **Problem**: Crash\n\n"
        "STATUS: ACCEPTED\n",
        encoding="utf-8",
    )
    res_bad_review = subprocess.run(
        [
            "python3",
            str(skills_dir / "code-reviewer" / "scripts" / "validate_review.py"),
            str(bad_review),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_bad_review.returncode == 1
    assert (
        "STATUS is ACCEPTED/PASS, but '[blocking]' or '[major]'"
        in res_bad_review.stderr
    )

    # 3. Security validator
    sec_file = tmp_path / "security.md"
    sec_file.write_text(
        "### Security Audit Summary\n"
        "- **Untrusted Inputs Traced**: `None`\n"
        "- **Findings Count**: `0 blocking, 0 major, 0 minor`\n\n"
        "### Security Findings\n"
        "- None.\n\n"
        "STATUS: ACCEPTED\n",
        encoding="utf-8",
    )
    res_sec = subprocess.run(
        [
            "python3",
            str(skills_dir / "security-auditor" / "scripts" / "validate_security.py"),
            str(sec_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_sec.returncode == 0

    # 4. Security validator accepts STATUS: NEEDS_HUMAN
    sec_human_file = tmp_path / "security_needs_human.md"
    sec_human_file.write_text(
        "### Security Audit Summary\n"
        "- **Untrusted Inputs Traced**: `None`\n"
        "- **Findings Count**: `0 blocking, 0 major, 0 minor`\n\n"
        "### Security Findings\n"
        "- None.\n\n"
        "STATUS: NEEDS_HUMAN\n",
        encoding="utf-8",
    )
    res_sec_human = subprocess.run(
        [
            "python3",
            str(skills_dir / "security-auditor" / "scripts" / "validate_security.py"),
            str(sec_human_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_sec_human.returncode == 0

    # 5. Combined review & security report validates cleanly against both validators
    combined_file = tmp_path / "combined_review.md"
    combined_file.write_text(
        "### Risk Classification: MEDIUM\n"
        "- **Reason**: Chained validators update.\n\n"
        "### Acceptance Criteria Verification\n"
        "| ID | Status | Evidence |\n"
        "| :--- | :--- | :--- |\n"
        "| AC-1 | PASS | `tests/test_checks.py:450` |\n\n"
        "### Findings\n"
        "- None.\n\n"
        "### Security Audit Summary\n"
        "- **Untrusted Inputs Traced**: `None`\n"
        "- **Findings Count**: `0 blocking, 0 major, 0 minor`\n\n"
        "### Security Findings\n"
        "- None.\n\n"
        "STATUS: ACCEPTED\n",
        encoding="utf-8",
    )
    res_comb_review = subprocess.run(
        [
            "python3",
            str(skills_dir / "code-reviewer" / "scripts" / "validate_review.py"),
            str(combined_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_comb_review.returncode == 0

    res_comb_sec = subprocess.run(
        [
            "python3",
            str(skills_dir / "security-auditor" / "scripts" / "validate_security.py"),
            str(combined_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_comb_sec.returncode == 0


@pytest.mark.parametrize(
    ("extra_vars", "stripped_keys"),
    [
        (
            {
                "PYTHONPATH": "/opt/custom_python",
                "PYTHONHOME": "/opt/python",
                "USER": "developer",
            },
            ["PYTHONPATH", "PYTHONHOME"],
        ),
        (
            {
                "NODE_PATH": "/global/node_modules",
                "NODE_OPTIONS": "--inspect",
                "INIT_CWD": "/outer/caller/dir",
                "npm_config_prefix": "/opt/npm",
                "BUNDLE_GEMFILE": "/outer/Gemfile",
                "BUNDLE_BIN_PATH": "/outer/bin/bundle",
                "GEM_HOME": "/outer/gems",
                "GEM_PATH": "/outer/gems",
                "RUBYOPT": "-W3",
            },
            [
                "NODE_PATH",
                "NODE_OPTIONS",
                "INIT_CWD",
                "npm_config_prefix",
                "BUNDLE_GEMFILE",
                "BUNDLE_BIN_PATH",
                "GEM_HOME",
                "GEM_PATH",
                "RUBYOPT",
            ],
        ),
    ],
    ids=["python_venv_vars", "node_npm_ruby_vars"],
)
def test_build_workspace_env_strips_leak_vars(
    tmp_path: Path, extra_vars: dict[str, str], stripped_keys: list[str]
) -> None:
    fake_venv = tmp_path / "host_venv"
    fake_venv_bin = fake_venv / "bin"
    fake_venv_bin.mkdir(parents=True)

    fake_conda = tmp_path / "host_conda"
    fake_conda_bin = fake_conda / "bin"
    fake_conda_bin.mkdir(parents=True)

    mock_env = {
        "PATH": f"{fake_venv_bin}:/usr/local/bin:{fake_conda_bin}:/usr/bin",
        "VIRTUAL_ENV": str(fake_venv),
        "VIRTUAL_ENV_PROMPT": "(host_venv)",
        "CONDA_PREFIX": str(fake_conda),
        "CONDA_DEFAULT_ENV": "base",
        **extra_vars,
    }

    with patch.dict("os.environ", mock_env, clear=True):
        env = build_workspace_env(tmp_path)

    for key in [
        "VIRTUAL_ENV",
        "VIRTUAL_ENV_PROMPT",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        *stripped_keys,
    ]:
        assert key not in env

    if "USER" in extra_vars:
        assert env.get("USER") == "developer"

    path_entries = env["PATH"].split(":")
    assert str(fake_venv_bin) not in path_entries
    assert str(fake_conda_bin) not in path_entries
    assert "/usr/local/bin" in path_entries
    assert "/usr/bin" in path_entries


def test_workspace_env_git_identity(tmp_path: Path) -> None:
    mock_env = {
        "GIT_AUTHOR_NAME": "Human Dev",
        "GIT_AUTHOR_EMAIL": "human@example.com",
        "GIT_COMMITTER_NAME": "Human Dev",
        "GIT_COMMITTER_EMAIL": "human@example.com",
        "PATH": "/usr/bin",
    }

    with patch.dict("os.environ", mock_env, clear=True):
        env = build_workspace_env(tmp_path)

    assert env["GIT_AUTHOR_NAME"] == "acp-factory[bot]"
    assert env["GIT_AUTHOR_EMAIL"] == "acp-factory[bot]@users.noreply.github.com"
    assert env["GIT_COMMITTER_NAME"] == "acp-factory[bot]"
    assert env["GIT_COMMITTER_EMAIL"] == "acp-factory[bot]@users.noreply.github.com"


def test_workspace_env_token_override(tmp_path: Path) -> None:
    # Host personal tokens are stripped by default; only the factory bot token is promoted.
    with patch.dict(
        "os.environ", {"GH_TOKEN": "human-token", "PATH": "/usr/bin"}, clear=True
    ):
        inherited = build_workspace_env(tmp_path)
    assert "GH_TOKEN" not in inherited

    with patch.dict(
        "os.environ",
        {
            "GH_TOKEN": "human-token",
            "FACTORY_GH_TOKEN": "bot-token",
            "PATH": "/usr/bin",
        },
        clear=True,
    ):
        overridden = build_workspace_env(tmp_path)
    assert overridden["GH_TOKEN"] == "bot-token"
    assert "FACTORY_GH_TOKEN" not in overridden

    # An explicit keep_env opt-in preserves the host token.
    with patch.dict(
        "os.environ", {"GH_TOKEN": "human-token", "PATH": "/usr/bin"}, clear=True
    ):
        kept = build_workspace_env(tmp_path, keep_env=["GH_TOKEN"])
    assert kept["GH_TOKEN"] == "human-token"

    # An empty FACTORY_GH_TOKEN must not create a GH_TOKEN value.
    with patch.dict(
        "os.environ", {"FACTORY_GH_TOKEN": "", "PATH": "/usr/bin"}, clear=True
    ):
        blank = build_workspace_env(tmp_path)
    assert "GH_TOKEN" not in blank


@pytest.mark.parametrize(
    "sensitive_key",
    [
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_APIKEY",
        "MY_SECRET",
        "DB_PASSWORD",
        "DB_PASSWD",
        "GCP_CREDENTIAL",
        "AWS_SECRET_ACCESS_KEY",
        "SSH_PRIVATE_KEY",
        "ACCESS_KEY_ID",
    ],
)
def test_build_workspace_env_sensitive_pattern_stripping(
    tmp_path: Path, sensitive_key: str
) -> None:
    mock_env = {
        sensitive_key: "host-secret-value",
        "USER": "developer",
        "PATH": "/usr/bin",
    }

    with patch.dict("os.environ", mock_env, clear=True):
        env = build_workspace_env(tmp_path)

    assert sensitive_key not in env
    assert env.get("USER") == "developer"


def test_build_workspace_env_keep_env_override(tmp_path: Path) -> None:
    mock_env = {
        "PYTHONPATH": "/opt/custom_python",
        "OPENAI_API_KEY": "sk-host",
        "GITHUB_TOKEN": "gh-host",
        "PATH": "/usr/bin",
    }

    with patch.dict("os.environ", mock_env, clear=True):
        env = build_workspace_env(tmp_path, keep_env=["PYTHONPATH", "OPENAI_API_KEY"])

    assert env.get("PYTHONPATH") == "/opt/custom_python"
    assert env.get("OPENAI_API_KEY") == "sk-host"
    assert "GITHUB_TOKEN" not in env


def test_build_workspace_env_strip_overrides_keep(tmp_path: Path) -> None:
    mock_env = {
        "OPENAI_API_KEY": "sk-host",
        "NODE_OPTIONS": "--inspect",
        "PATH": "/usr/bin",
    }

    with patch.dict("os.environ", mock_env, clear=True):
        env = build_workspace_env(
            tmp_path,
            keep_env=["OPENAI_API_KEY", "NODE_OPTIONS"],
            strip_env=["OPENAI_API_KEY"],
        )

    assert "OPENAI_API_KEY" not in env
    assert env.get("NODE_OPTIONS") == "--inspect"


def test_build_workspace_env_custom_strip_and_extra_env(tmp_path: Path) -> None:
    mock_env = {
        "MY_SECRET_HOST_VAR": "secret",
        "NODE_OPTIONS": "--inspect",
        "PATH": "/usr/bin",
    }

    with patch.dict("os.environ", mock_env, clear=True):
        env = build_workspace_env(
            tmp_path,
            strip_env=["MY_SECRET_HOST_VAR"],
            extra_env={
                "TARGET_ENV": "production",
                "NODE_OPTIONS": "--max-old-space-size=4096",  # Explicit extra_env wins
            },
        )

    assert "MY_SECRET_HOST_VAR" not in env
    assert env.get("TARGET_ENV") == "production"
    # Explicit extra_env applied after default strip
    assert env.get("NODE_OPTIONS") == "--max-old-space-size=4096"


def test_build_workspace_env_injects_low_noise_and_ripgrep_defaults(
    tmp_path: Path,
) -> None:
    mock_env = {
        "TERM": "xterm-256color",
        "PAGER": "less",
        "PATH": "/usr/bin",
    }
    with patch.dict("os.environ", mock_env, clear=True):
        env = build_workspace_env(tmp_path)

    assert env["NO_COLOR"] == "1"
    assert env["TERM"] == "dumb"
    assert env["CLICOLOR"] == "0"
    assert env["FORCE_COLOR"] == "0"
    assert env["GIT_PAGER"] == "cat"
    assert env["PAGER"] == "cat"
    assert env["UV_NO_PROGRESS"] == "1"
    assert env["PIP_PROGRESS_BAR"] == "off"
    assert env["npm_config_loglevel"] == "warn"
    assert env["PYTEST_ADDOPTS"] == "-q --tb=short --no-header"

    rg_cfg_path = Path(env["RIPGREP_CONFIG_PATH"])
    assert rg_cfg_path.is_file()
    rg_content = rg_cfg_path.read_text(encoding="utf-8")
    assert "--max-columns=300" in rg_content
    assert "--glob=!*.lock" in rg_content
    assert "--glob=!.venv/*" in rg_content

    # extra_env overrides defaults and strip_env suppresses defaults
    with patch.dict("os.environ", mock_env, clear=True):
        overridden = build_workspace_env(
            tmp_path,
            extra_env={"PYTEST_ADDOPTS": "-vv", "NO_COLOR": "0"},
            strip_env=["RIPGREP_CONFIG_PATH"],
        )
    assert overridden["PYTEST_ADDOPTS"] == "-vv"
    assert overridden["NO_COLOR"] == "0"
    assert "RIPGREP_CONFIG_PATH" not in overridden


async def test_run_shell_command_forwards_keep_env(tmp_path: Path) -> None:
    host_path = os.environ.get("PATH", "")
    command = 'python3 -c \'import os; print(os.environ.get("CUSTOM_API_KEY", ""))\''

    with patch.dict(
        "os.environ", {"CUSTOM_API_KEY": "host-secret", "PATH": host_path}, clear=True
    ):
        rc_kept, kept_output, _ = await run_shell_command(
            command, tmp_path, keep_env=["CUSTOM_API_KEY"]
        )
        rc_stripped, stripped_output, _ = await run_shell_command(command, tmp_path)

    assert rc_kept == 0
    assert kept_output.strip() == "host-secret"
    assert rc_stripped == 0
    assert stripped_output.strip() == ""


async def test_run_checks_sequential_forwards_keep_env(tmp_path: Path) -> None:
    host_path = os.environ.get("PATH", "")
    checks = [
        CheckConfig(
            name="token check",
            cmd=(
                "python3 -c 'import os, sys; "
                'sys.exit(0 if os.environ.get("CUSTOM_TOKEN") == "host-secret" else 1)\''
            ),
        )
    ]

    with patch.dict(
        "os.environ", {"CUSTOM_TOKEN": "host-secret", "PATH": host_path}, clear=True
    ):
        passed_with_keep, _ = await run_checks_sequential(
            checks, tmp_path, keep_env=["CUSTOM_TOKEN"]
        )
        passed_without_keep, _ = await run_checks_sequential(checks, tmp_path)

    assert passed_with_keep is True
    assert passed_without_keep is False


async def test_run_factory_threads_keep_env_to_setup_and_client(tmp_path: Path) -> None:
    marker = tmp_path / "setup_token.txt"
    setup_script = tmp_path / "setup_env.py"
    setup_script.write_text(
        "import os\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(os.environ.get('CUSTOM_TOKEN', 'missing'))\n",
        encoding="utf-8",
    )
    setup_cmd = f"python3 {setup_script}"
    cfg = FactoryConfig(task="noop", keep_env=["CUSTOM_TOKEN"], setup=[setup_cmd])
    captured_kwargs: dict[str, object] = {}

    class FakeACPClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured_kwargs.update(kwargs)
            self.tracker = AsyncMock()
            self.tracker.history = []

        async def __aenter__(self) -> FakeACPClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def send_prompt(self, prompt: str, **kwargs: object) -> TurnResult:
            return TurnResult(
                stop_reason="end_turn",
                summary=TurnSummary(
                    prompt=prompt,
                    response_text="Done.",
                    thought_text="",
                    stop_reason="end_turn",
                    duration_seconds=0.01,
                    tool_call_count=0,
                ),
            )

    host_path = os.environ.get("PATH", "")
    with (
        patch.dict(
            "os.environ", {"CUSTOM_TOKEN": "host-secret", "PATH": host_path}, clear=True
        ),
        patch("acp_factory.runner.loop.FactoryACPClient", FakeACPClient),
    ):
        outcome = await run_factory(cfg, tmp_path)

    assert outcome.succeeded is True
    assert marker.read_text(encoding="utf-8") == "host-secret"
    assert captured_kwargs["keep_env"] == ["CUSTOM_TOKEN"]


async def test_factory_acp_client_forwards_keep_env_to_workspace_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import acpterm.acp_agent as acpterm_agent_mod

    from acp_factory.acp import client as client_mod
    from acp_factory.config import AgentConfig

    captured_kwargs: dict[str, object] = {}
    sentinel = object()

    def fake_build_workspace_env(workspace: Path, **kwargs: object) -> dict[str, str]:
        _ = workspace
        captured_kwargs.update(kwargs)
        return {"PATH": "/usr/bin"}

    class FakeAgent:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs
            self.session_id = None

        async def start(self, **kwargs: object) -> None:
            _ = kwargs
            acpterm_agent_mod.spawn_stdio_transport("fake-agent")

    monkeypatch.setattr(
        acpterm_agent_mod, "spawn_stdio_transport", lambda *a, **k: sentinel
    )
    monkeypatch.setattr(
        "acp_factory.runner.checks.build_workspace_env", fake_build_workspace_env
    )
    monkeypatch.setattr(client_mod, "ACPAgent", FakeAgent)

    client = client_mod.FactoryACPClient(
        workspace=tmp_path,
        agent_config=AgentConfig(name="opencode", command=["fake-agent-binary"]),
        keep_env=["CUSTOM_TOKEN"],
    )
    await client.start()

    assert captured_kwargs["keep_env"] == ["CUSTOM_TOKEN"]


async def test_readonly_gh_shim_passes_factory_token(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text("#!/bin/sh\nprintf '%s' \"$GH_TOKEN\"\n", encoding="utf-8")
    fake_gh.chmod(0o755)

    mock_env = {
        "PATH": f"{fake_bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "GH_TOKEN": "human-token",
        "FACTORY_GH_TOKEN": "bot-token",
    }
    with patch.dict("os.environ", mock_env, clear=True):
        env = build_workspace_env(tmp_path, restrict_gh_writes=True)

    proc = await asyncio.create_subprocess_exec(
        "gh",
        "issue",
        "view",
        "1",
        cwd=str(tmp_path),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    assert proc.returncode == 0, stderr.decode("utf-8", errors="replace")
    assert stdout.decode("utf-8", errors="replace") == "bot-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("subcommand", ["token", "status", "login", "logout"])
async def test_readonly_gh_shim_blocks_auth_subcommands(
    tmp_path: Path, subcommand: str
) -> None:
    """The read-only gh shim rejects all `gh auth` subcommands."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_gh.chmod(0o755)

    mock_env = {
        "PATH": f"{fake_bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
    }
    with patch.dict("os.environ", mock_env, clear=True):
        env = build_workspace_env(tmp_path, restrict_gh_writes=True)

    proc = await asyncio.create_subprocess_exec(
        "gh",
        "auth",
        subcommand,
        cwd=str(tmp_path),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    assert proc.returncode == 1
    assert "disabled for agents" in stderr.decode("utf-8", errors="replace")


class _FakeReviewClient:
    """Async-context stand-in for a dedicated review sub-client."""

    captured: dict[str, object] = {}

    def __init__(self, **kwargs: object) -> None:
        type(self).captured = kwargs

    async def __aenter__(self) -> _FakeReviewClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def send_prompt(self, prompt: str, timeout: int | None = None) -> TurnResult:
        return TurnResult(
            stop_reason="end_turn",
            summary=TurnSummary(
                prompt=prompt,
                response_text="Reviewed.\nVERDICT: PASS",
                thought_text="",
                stop_reason="end_turn",
                duration_seconds=0.1,
                tool_call_count=0,
            ),
        )


@pytest.mark.parametrize(
    ("check_kwargs", "expected_level"),
    [
        ({"model": "other-model"}, "high"),
        ({"thought_level": " low "}, "low"),
        ({"agent": "kiro", "thought_level": "high"}, "high"),
    ],
    ids=["inherits", "check-override", "cross-agent-override"],
)
async def test_review_prompt_check_propagates_thought_level(
    tmp_path: Path, check_kwargs: dict[str, Any], expected_level: str
) -> None:
    """AC-5: dedicated review sub-clients inherit and can override thought_level."""
    from acp_factory.acp.client import FactoryACPClient
    from acp_factory.config import AgentConfig
    from acp_factory.runner.checks import run_single_check

    outer = FactoryACPClient(
        workspace=tmp_path,
        agent_config=AgentConfig(name="opencode", thought_level="high"),
        resource_dirs=[],
        discover_apm_resources=False,
        silent=True,
    )
    check = CheckConfig(
        name="Architecture Review", prompt="Check structure", **check_kwargs
    )

    with patch("acp_factory.acp.client.FactoryACPClient", _FakeReviewClient):
        result = await run_single_check(check, tmp_path, acp_client=outer)

    assert result.passed is True
    review_agent = _FakeReviewClient.captured["agent_config"]
    assert isinstance(review_agent, AgentConfig)
    assert review_agent.thought_level == expected_level


def test_persist_run_artifacts_records_thought_level(tmp_path: Path) -> None:
    """AC-5: run.json records the resolved stage thought_level."""
    import json

    from acp_factory.runner.loop import _persist_run_artifacts

    (tmp_path / ".factory").mkdir()
    config = FactoryConfig(task="Test task", agent={"thought_level": "high"})

    run_dir = _persist_run_artifacts(
        tmp_path,
        "run-thought-level",
        config,
        succeeded=True,
        attempts_used=1,
        total_duration=0.1,
        setup_logs=[],
        turn_transcripts=[],
        check_history=[],
        failure_reason=None,
    )

    assert run_dir is not None
    data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert data["thought_level"] == "high"


def test_show_run_header_displays_thought_level() -> None:
    """AC-5: the run header surfaces the configured thought level."""
    from io import StringIO

    from rich.console import Console

    from acp_factory.config import AgentConfig
    from acp_factory.ui.display import FactoryDisplay

    cfg = FactoryConfig(agent=AgentConfig(name="opencode", thought_level="high"))
    buf = StringIO()
    FactoryDisplay(
        custom_console=Console(file=buf, width=200, highlight=False)
    ).show_run_header(cfg, Path("/tmp/repo"))

    assert "(thought_level: high)" in buf.getvalue()
