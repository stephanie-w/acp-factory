"""Unit and integration tests for acp_factory.runner (checks, truncation, and retry loop)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from acp_factory.acp.client import TurnResult
from acp_factory.acp.events import TurnSummary
from acp_factory.config import CheckConfig, FactoryConfig, RetryConfig
from acp_factory.runner.checks import (
    parse_review_verdict,
    run_checks_sequential,
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


def test_build_retry_prompt_includes_context(tmp_path: Path) -> None:
    from acp_factory.runner.checks import CheckResult

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
    assert outcome.last_response_text.startswith("# Specification")
    assert "Here is the corrected specification" not in outcome.last_response_text


def test_all_skill_validators_and_templates_exist_and_work(tmp_path: Path) -> None:
    import subprocess

    repo_root = Path(__file__).resolve().parents[1]
    skills_dir = repo_root / "skills"

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
