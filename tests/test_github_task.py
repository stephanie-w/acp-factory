"""Unit tests for TaskContext, in-place Issue Body updates, and GitHub label stage verdicts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from acp_factory.runner.checks import parse_review_verdict_status
from acp_factory.runner.loop import FactoryRunOutcome, run_issue_stage
from acp_factory.task import (
    LABEL_BUILD,
    LABEL_COMPLETED,
    LABEL_DONE,
    LABEL_NEEDS_HUMAN,
    LABEL_PR_READY,
    LABEL_SPEC,
    LABEL_SPEC_READY,
    TaskContext,
    format_task_prompt_context,
    parse_stage_verdict,
    render_issue_body,
    split_issue_body,
)


def test_split_and_render_issue_body_in_place() -> None:
    original = "Users cannot filter items by category.\nPlease add support."
    spec_v1 = "## Specification\n- Add `category` query parameter."
    rendered_v1 = render_issue_body(original, spec_v1)

    assert "<!-- factory:start -->" in rendered_v1
    assert "<!-- factory:end -->" in rendered_v1

    parsed_orig, parsed_block = split_issue_body(rendered_v1)
    assert parsed_orig == original
    assert parsed_block == spec_v1

    # Updating the factory block in-place never alters original_body
    spec_v2 = "## Specification\n- Add `category` and `limit` parameters."
    rendered_v2 = render_issue_body(parsed_orig, spec_v2)
    parsed_orig_2, parsed_block_2 = split_issue_body(rendered_v2)
    assert parsed_orig_2 == original
    assert parsed_block_2 == spec_v2


def test_task_context_from_gh_json_and_prompt_formatting() -> None:
    gh_payload = {
        "number": 42,
        "title": "Add category filter",
        "url": "https://github.com/acme/api/issues/42",
        "body": render_issue_body(
            "Initial issue description.",
            "## Open Questions\n1. Should category matching be case-insensitive?",
        ),
        "labels": [{"name": "bug"}, {"name": "factory:spec"}],
        "comments": [
            {
                "author": {"login": "stephanie"},
                "body": "1: Yes, make it case-insensitive.",
                "createdAt": "2026-10-31T09:00:00Z",
            }
        ],
    }

    task = TaskContext.from_gh_json(gh_payload)
    assert task.issue_number == 42
    assert task.current_stage == "spec"
    assert task.branch == "factory/issue-42"
    assert task.original_body == "Initial issue description."
    assert "Open Questions" in task.factory_block
    assert len(task.comments) == 1
    assert task.comments[0].author == "stephanie"

    prompt_ctx = format_task_prompt_context(task)
    assert "GitHub Issue #42: Add category filter" in prompt_ctx
    assert "Initial issue description." in prompt_ctx
    assert "Comment #1 by @stephanie" in prompt_ctx
    assert "Yes, make it case-insensitive." in prompt_ctx


def test_parse_stage_verdicts_across_pipeline() -> None:
    # 1. Triage OK -> factory:spec
    v_triage_ok = parse_stage_verdict("triage", "Scoped to routes.py.\nSTATUS: OK")
    assert v_triage_ok.target_label == LABEL_SPEC

    # 2. Triage TOO_BIG -> factory:needs-human
    v_triage_big = parse_stage_verdict(
        "triage", "Requires 4 microservices.\nSTATUS: TOO_BIG"
    )
    assert v_triage_big.target_label == LABEL_NEEDS_HUMAN

    # 3. Spec with Open Questions -> factory:needs-human
    v_spec_q = parse_stage_verdict(
        "spec",
        "## Behavior\n...\n## Open Questions\n1. Redis or Postgres?\nSTATUS: NEEDS_HUMAN",
    )
    assert v_spec_q.target_label == LABEL_NEEDS_HUMAN
    assert "## Open Questions" in v_spec_q.updated_factory_block

    # 4. Spec Ready -> factory:spec-ready (Human Gate 1), stripping conversational intro/outro
    v_spec_ok = parse_stage_verdict(
        "spec",
        "I have inspected the code. Here is the spec:\n\n---\n\n"
        "# Specification\n## Behavior\nComplete spec.\n## Assumptions\n- Default limit 10.\n"
        "STATUS: SPEC_READY\n\n---\nOne thing worth flagging for your awareness...",
    )
    assert v_spec_ok.target_label == LABEL_SPEC_READY
    assert "STATUS: SPEC_READY" not in v_spec_ok.updated_factory_block
    assert "I have inspected the code" not in v_spec_ok.updated_factory_block
    assert "One thing worth flagging" not in v_spec_ok.updated_factory_block
    assert v_spec_ok.updated_factory_block.startswith("# Specification")

    # 5. Review Accepted -> factory:pr-ready (Human Gate 2, publish_pr=True)
    v_rev_ok = parse_stage_verdict(
        "review",
        "Diff matches spec and all tests pass.\nSTATUS: ACCEPTED",
        current_factory_block="## Spec\nDetails",
    )
    assert v_rev_ok.target_label == LABEL_PR_READY
    assert v_rev_ok.publish_pr is True

    # 6. Review Revision Needed -> factory:build with findings appended
    v_rev_retry = parse_stage_verdict(
        "review",
        "routes.py:22: missing negative offset check.\nSTATUS: REVISION_NEEDED",
        current_factory_block="## Spec\nDetails",
    )
    assert v_rev_retry.target_label == LABEL_BUILD
    assert v_rev_retry.publish_pr is False
    assert "Latest Review Findings" in v_rev_retry.updated_factory_block


def test_structured_json_review_check_verdict() -> None:
    status, feedback = parse_review_verdict_status(
        'Review complete.\n{"status": "retry", "feedback": "src/api.py:10: missing check"}'
    )
    assert status == "retry"
    assert feedback == "src/api.py:10: missing check"

    status_stop, _ = parse_review_verdict_status("Cannot proceed.\nVERDICT: STOP")
    assert status_stop == "stop"


async def test_run_issue_stage_spec_and_done(tmp_path: Path) -> None:
    fake_task = TaskContext(
        issue_number=7,
        title="Add healthcheck endpoint",
        original_body="Add GET /healthz returning 200 OK.",
        labels=[LABEL_SPEC],
    )

    fake_outcome = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=1.2,
        last_response_text="## Behavior\nReturn 200 OK.\nSTATUS: SPEC_READY",
    )

    with (
        patch(
            "acp_factory.runner.loop.fetch_issue", new=AsyncMock(return_value=fake_task)
        ),
        patch(
            "acp_factory.runner.loop.run_factory",
            new=AsyncMock(return_value=fake_outcome),
        ),
        patch(
            "acp_factory.runner.loop.update_issue_factory_block", new=AsyncMock()
        ) as mock_update,
        patch(
            "acp_factory.runner.loop.transition_issue_label", new=AsyncMock()
        ) as mock_transition,
    ):
        outcome = await run_issue_stage(tmp_path, 7)
        assert outcome.stage_verdict is not None
        assert outcome.stage_verdict.target_label == LABEL_SPEC_READY
        mock_update.assert_awaited_once()
        mock_transition.assert_awaited_once_with(
            fake_task, LABEL_SPEC_READY, cwd=tmp_path.resolve()
        )

    # Test `done` stage cleanup (including when triggered automatically by a CLOSED issue with leftover labels)
    done_task = TaskContext(
        issue_number=7,
        title="Add healthcheck endpoint",
        state="CLOSED",
        labels=["factory:review", LABEL_DONE],
    )
    with (
        patch(
            "acp_factory.runner.loop.fetch_issue", new=AsyncMock(return_value=done_task)
        ),
        patch(
            "acp_factory.runner.loop.WorktreeManager.remove_issue_worktree",
            new=AsyncMock(return_value=True),
        ) as mock_rm_wt,
        patch(
            "acp_factory.runner.loop.transition_issue_label", new=AsyncMock()
        ) as mock_transition_done,
    ):
        done_outcome = await run_issue_stage(tmp_path, 7)
        assert done_outcome.succeeded is True
        assert done_outcome.stage_verdict is not None
        assert done_outcome.stage_verdict.target_label == LABEL_COMPLETED
        mock_rm_wt.assert_awaited_once_with(tmp_path.resolve(), 7)
        mock_transition_done.assert_awaited_once_with(
            done_task, LABEL_COMPLETED, cwd=tmp_path.resolve()
        )


async def test_run_issue_stage_dry_run_never_mutates_github(tmp_path: Path) -> None:
    fake_task = TaskContext(
        issue_number=12,
        title="Support pagination",
        original_body="Original description.",
        labels=[LABEL_SPEC],
        url="https://github.com/stephanie-w/acpterm/issues/12",
    )
    fake_outcome = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=0.5,
        last_response_text="## Behavior\nAdd limit and offset.\nSTATUS: SPEC_READY",
    )

    with (
        patch(
            "acp_factory.runner.loop.fetch_issue",
            new=AsyncMock(return_value=fake_task),
        ),
        patch(
            "acp_factory.runner.loop.run_factory",
            new=AsyncMock(return_value=fake_outcome),
        ),
        patch(
            "acp_factory.runner.loop.update_issue_factory_block",
            new=AsyncMock(),
        ) as mock_update,
        patch(
            "acp_factory.runner.loop.transition_issue_label",
            new=AsyncMock(),
        ) as mock_transition,
    ):
        outcome = await run_issue_stage(tmp_path, 12, dry_run=True)
        assert outcome.dry_run_plan is not None
        assert outcome.dry_run_plan.issue_body_file is not None
        assert outcome.dry_run_plan.issue_body_file.exists()
        body_text = outcome.dry_run_plan.issue_body_file.read_text(encoding="utf-8")
        assert "Original description." in body_text
        assert "Add limit and offset." in body_text
        assert any(
            "gh issue edit 12 -R stephanie-w/acpterm --body-file" in c
            for c in outcome.dry_run_plan.commands
        )
        assert any(
            "gh label create factory:spec-ready" in c
            for c in outcome.dry_run_plan.commands
        )
        assert any(
            "--add-label factory:spec-ready" in c for c in outcome.dry_run_plan.commands
        )

        # Crucially: no mutating gh calls were executed
        mock_update.assert_not_awaited()
        mock_transition.assert_not_awaited()


async def test_readonly_gh_shim_blocks_mutating_commands(tmp_path: Path) -> None:
    import asyncio

    from acp_factory.runner.checks import build_workspace_env

    env = build_workspace_env(tmp_path, restrict_gh_writes=True)
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "issue",
        "edit",
        "42",
        "--add-label",
        "factory:ready",
        cwd=str(tmp_path),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    assert proc.returncode == 1
    assert "Mutating 'gh' commands are disabled for agents" in stderr.decode("utf-8")
