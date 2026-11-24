"""Unit tests for TaskContext, in-place Issue Body updates, and GitHub label stage verdicts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from acp_factory.github import GitHubError, fetch_issue, run_gh
from acp_factory.runner.checks import parse_review_verdict_status
from acp_factory.runner.loop import (
    FactoryRunOutcome,
    build_delta_review_brief,
    build_spec_revision_brief,
    has_existing_spec,
    run_issue_stage,
)
from acp_factory.task import (
    LABEL_BUILD,
    LABEL_COMPLETED,
    LABEL_DONE,
    LABEL_NEEDS_HUMAN,
    LABEL_PR_READY,
    LABEL_REVIEW,
    LABEL_SPEC,
    LABEL_SPEC_READY,
    IssueComment,
    TaskContext,
    clean_spec_block,
    extract_factory_findings,
    format_task_prompt_context,
    parse_factory_meta,
    render_attribution_banner,
    render_issue_body,
    replace_factory_findings,
    set_attribution_banner,
    set_factory_meta,
    split_issue_body,
    strip_attribution_banner,
    strip_factory_findings,
    strip_factory_meta,
)
from acp_factory.verdict import (
    StageVerdict,
    _clean_markdown_stage_body,
    parse_stage_verdict,
)


def _fake_git(cwd: Path, *args: str) -> tuple[int, str, str]:
    if args[:2] == ("rev-parse", "HEAD"):
        return (0, "abc123def", "")
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
        return (0, "feat/my-branch", "")
    if args[:2] == ("log", "-1"):
        return (0, "Add pipeline retry logic", "")
    if args[:2] == ("status", "--porcelain"):
        return (0, "", "")
    return (0, "", "")


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
    assert task.comments[0].kind == "issue_comment"
    assert task.comments[0].review_state is None

    prompt_ctx = format_task_prompt_context(task)
    assert "GitHub Issue #42: Add category filter" in prompt_ctx
    assert "Initial issue description." in prompt_ctx
    assert "Comment #1 by @stephanie" in prompt_ctx
    assert "Yes, make it case-insensitive." in prompt_ctx


def test_merge_pr_feedback_orders_comments_chronologically() -> None:
    task = TaskContext(
        issue_number=42,
        title="Add category filter",
        comments=[
            IssueComment(
                author="original",
                body="Issue comment",
                created_at="2026-11-01T12:00:00Z",
            )
        ],
    )

    task.merge_pr_feedback(
        [
            {
                "author": {"login": "alice"},
                "body": "PR conversation comment.",
                "createdAt": "2026-11-02T09:00:00Z",
            },
            "not-a-dict",
        ],
        [
            {
                "author": {"login": "bob"},
                "body": "Please add tests.",
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-11-03T09:00:00Z",
            },
            {
                "author": {"login": "carol"},
                "body": "",
                "state": "APPROVED",
                "submittedAt": "2026-11-02T10:00:00Z",
            },
        ],
    )

    assert [c.author for c in task.comments] == ["original", "alice", "carol", "bob"]
    assert [c.kind for c in task.comments] == [
        "issue_comment",
        "pr_comment",
        "pr_review",
        "pr_review",
    ]
    assert task.comments[2].review_state == "APPROVED"
    assert task.comments[2].created_at == "2026-11-02T10:00:00Z"
    assert task.comments[3].review_state == "CHANGES_REQUESTED"
    assert task.comments[3].body == "Please add tests."
    assert task.comments[3].created_at == "2026-11-03T09:00:00Z"


def test_merge_pr_feedback_skips_non_dict_and_tolerates_missing_fields() -> None:
    task = TaskContext(issue_number=42, title="Add category filter")

    task.merge_pr_feedback(
        [None, 42, {"body": "missing author"}],
        [
            {},
            {"author": {"login": "bob"}, "body": "no state", "createdAt": ""},
        ],
    )

    assert [c.author for c in task.comments] == ["unknown", "unknown", "bob"]
    assert task.comments[0].body == "missing author"
    assert task.comments[1].body == ""
    assert task.comments[1].review_state is None
    assert task.comments[2].review_state is None
    assert task.comments[2].created_at == ""


def test_format_task_prompt_context_with_pr_feedback() -> None:
    task = TaskContext(
        issue_number=42,
        title="Add category filter",
        url="https://github.com/acme/api/issues/42",
        original_body="Initial issue description.",
        pr_url="https://github.com/acme/api/pull/88",
        comments=[
            IssueComment(
                author="original",
                body="Issue comment",
                created_at="2026-11-01T12:00:00Z",
            ),
            IssueComment(
                author="alice",
                body="PR conversation comment.",
                created_at="2026-11-02T09:00:00Z",
                kind="pr_comment",
            ),
            IssueComment(
                author="bob",
                body="",
                created_at="2026-11-03T09:00:00Z",
                kind="pr_review",
                review_state="CHANGES_REQUESTED",
            ),
            IssueComment(
                author="carol",
                body="Looks good.",
                created_at="2026-11-04T09:00:00Z",
                kind="pr_review",
                review_state="APPROVED",
            ),
        ],
    )

    prompt_ctx = format_task_prompt_context(task)

    assert "- **PR URL:** https://github.com/acme/api/pull/88" in prompt_ctx
    # Authors, review states, bodies, and timestamps are rendered without
    # coupling to the exact markdown decoration.
    assert "@original" in prompt_ctx
    assert "Issue comment" in prompt_ctx
    assert "2026-11-01T12:00:00Z" in prompt_ctx
    assert "@alice" in prompt_ctx
    assert "PR conversation comment." in prompt_ctx
    assert "@bob" in prompt_ctx
    assert "CHANGES_REQUESTED" in prompt_ctx
    assert "no review body provided" in prompt_ctx
    assert "@carol" in prompt_ctx
    assert "APPROVED" in prompt_ctx
    assert "Looks good." in prompt_ctx
    # Comments render chronologically with PR feedback interleaved by timestamp.
    assert prompt_ctx.index("@original") < prompt_ctx.index("@alice")
    assert prompt_ctx.index("@alice") < prompt_ctx.index("@bob")
    assert prompt_ctx.index("@bob") < prompt_ctx.index("@carol")


def _issue_payload() -> dict[str, object]:
    return {
        "number": 42,
        "title": "Add category filter",
        "url": "https://github.com/acme/api/issues/42",
        "body": "Initial issue description.",
        "labels": [{"name": "factory:build"}],
        "comments": [
            {
                "author": {"login": "stephanie"},
                "body": "Issue-side context.",
                "createdAt": "2026-11-01T09:00:00Z",
            }
        ],
    }


async def test_fetch_issue_merges_pr_feedback(tmp_path: Path) -> None:
    pr_list = [{"number": 88, "url": "https://github.com/acme/api/pull/88"}]
    pr_view = {
        "comments": [
            {
                "author": {"login": "alice"},
                "body": "PR conversation comment.",
                "createdAt": "2026-11-02T09:00:00Z",
            }
        ],
        "reviews": [
            {
                "author": {"login": "bob"},
                "body": "Please add tests.",
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-11-03T09:00:00Z",
            }
        ],
    }
    calls: list[list[str]] = []

    async def fake_run_gh(
        args: list[str], cwd: Path, *, input_text: str | None = None
    ) -> str:
        calls.append(args)
        if args[:2] == ["issue", "view"]:
            return json.dumps(_issue_payload())
        if args[:2] == ["pr", "list"]:
            return json.dumps(pr_list)
        if args[:2] == ["pr", "view"]:
            return json.dumps(pr_view)
        raise AssertionError(f"unexpected gh call: {args}")

    with patch("acp_factory.github.run_gh", new=AsyncMock(side_effect=fake_run_gh)):
        task = await fetch_issue(42, tmp_path, repo_slug="acme/api")

    assert task.pr_url == "https://github.com/acme/api/pull/88"
    assert [c.kind for c in task.comments] == [
        "issue_comment",
        "pr_comment",
        "pr_review",
    ]
    assert [c.author for c in task.comments] == ["stephanie", "alice", "bob"]
    assert task.comments[2].created_at == "2026-11-03T09:00:00Z"
    # PR list: head branch, state, and JSON fields (flag order not asserted).
    pr_list_cmd = calls[1]
    assert pr_list_cmd[:2] == ["pr", "list"]
    assert "--head" in pr_list_cmd and "factory/issue-42" in pr_list_cmd
    assert "--state" in pr_list_cmd and "open" in pr_list_cmd
    assert "--json" in pr_list_cmd and "number,url" in pr_list_cmd
    assert "--repo" in pr_list_cmd and "acme/api" in pr_list_cmd
    # PR view: target number and JSON fields.
    pr_view_cmd = calls[2]
    assert pr_view_cmd[:2] == ["pr", "view"]
    assert "88" in pr_view_cmd
    assert "--json" in pr_view_cmd and "comments,reviews,body" in pr_view_cmd
    assert "--repo" in pr_view_cmd and "acme/api" in pr_view_cmd


@pytest.mark.parametrize(
    ("pr_list_response", "pr_view_response", "expected_pr_url"),
    [
        ("[]", None, None),
        (GitHubError("branch is not pushed"), None, None),
        (
            json.dumps([{"number": 88, "url": "https://example.test/pull/88"}]),
            GitHubError("rate limited"),
            "https://example.test/pull/88",
        ),
        (json.dumps({"number": 88}), None, None),
    ],
    ids=["no_pr", "pr_list_error", "pr_view_error", "malformed_pr_list"],
)
async def test_fetch_issue_pr_fallback_cases(
    tmp_path: Path,
    pr_list_response: str | Exception,
    pr_view_response: str | Exception | None,
    expected_pr_url: str | None,
) -> None:
    async def fake_run_gh(
        args: list[str], cwd: Path, *, input_text: str | None = None
    ) -> str:
        if args[:2] == ["issue", "view"]:
            return json.dumps(_issue_payload())
        if args[:2] == ["pr", "list"]:
            if isinstance(pr_list_response, Exception):
                raise pr_list_response
            return pr_list_response
        if args[:2] == ["pr", "view"]:
            if isinstance(pr_view_response, Exception):
                raise pr_view_response
            assert pr_view_response is not None
            return pr_view_response
        raise AssertionError(f"unexpected gh call: {args}")

    with patch("acp_factory.github.run_gh", new=AsyncMock(side_effect=fake_run_gh)):
        task = await fetch_issue(42, tmp_path)

    assert task.pr_url == expected_pr_url
    assert [c.kind for c in task.comments] == ["issue_comment"]
    assert [c.body for c in task.comments] == ["Issue-side context."]


def test_verdict_state_machine_lives_in_dedicated_module() -> None:
    import acp_factory.task as task_module
    import acp_factory.verdict as verdict_module

    assert verdict_module.StageVerdict.__module__ == "acp_factory.verdict"
    assert verdict_module.parse_stage_verdict.__module__ == "acp_factory.verdict"
    assert not hasattr(task_module, "StageVerdict")
    assert not hasattr(task_module, "parse_stage_verdict")


def test_parse_stage_verdicts_across_pipeline() -> None:
    # 1. Triage OK -> factory:spec
    v_triage_ok = parse_stage_verdict("triage", "Scoped to routes.py.\nSTATUS: OK")
    assert v_triage_ok.target_label == LABEL_SPEC

    # 1b. Triage TRIVIAL -> factory:spec-ready (skips spec, keeps Human Gate 1)
    v_triage_trivial = parse_stage_verdict(
        "triage",
        "### Codebase Inspection\nScoped to one function.\n"
        "### Acceptance Criteria\n- **AC-1**: Fix typo.\n"
        "STATUS: TRIVIAL",
    )
    assert v_triage_trivial.target_label == LABEL_SPEC_READY
    assert v_triage_trivial.status == "trivial"
    assert "## Triage Report (`TRIVIAL`)" in v_triage_trivial.updated_factory_block
    assert "### Acceptance Criteria" in v_triage_trivial.updated_factory_block

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


def test_spec_validator_catches_unformatted_questions_and_normalizes_valid_spec(
    tmp_path: Path,
) -> None:
    import subprocess

    from acp_factory.resources import get_bundled_resources_dir

    resources_root = get_bundled_resources_dir()
    validator = (
        resources_root / "skills" / "spec-writer" / "scripts" / "validate_spec.py"
    )

    # 1. Unformatted monologue + questions without headings/STATUS fails validation
    unformatted_output = (
        "I'll analyze the codebase to write a precise specification for this issue.\n\n"
        "Before I finalize the spec, I have two questions:\n\n"
        "**[1]** Where should `FACTORY_GH_TOKEN` be read from?\n"
        "a. Environment variable only\nb. .env file\n\n(Answer: 1=a or 1=b)"
    )
    resp_file = tmp_path / "response.md"
    resp_file.write_text(unformatted_output, encoding="utf-8")

    proc_fail = subprocess.run(
        ["python3", str(validator), str(resp_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_fail.returncode == 1
    assert "Specification validation failed" in proc_fail.stderr
    assert "Missing terminal 'STATUS:" in proc_fail.stderr

    # 2. Properly formatted NEEDS_HUMAN spec with leading monologue is normalized cleanly
    valid_needs_human = (
        "I'll inspect the codebase first.\n\n"
        "# Specification\n\n"
        "## Problem\nToken source is ambiguous.\n\n"
        "## Open Questions\n"
        "1. Where should `FACTORY_GH_TOKEN` be read from?\n"
        "   - Option A (Recommended): Environment variable only\n"
        "   - Option B: `.env` file\n\n"
        "STATUS: NEEDS_HUMAN\n"
        "Let me know which option you prefer!"
    )
    resp_file.write_text(valid_needs_human, encoding="utf-8")
    proc_ok = subprocess.run(
        ["python3", str(validator), str(resp_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_ok.returncode == 0
    normalized = resp_file.read_text(encoding="utf-8")
    assert normalized.startswith("# Specification")
    assert "I'll inspect the codebase" not in normalized
    assert "Let me know which option" not in normalized

    verdict = parse_stage_verdict("spec", normalized)
    assert verdict.target_label == LABEL_NEEDS_HUMAN
    assert verdict.status == "needs_human"
    assert "## Open Questions" in verdict.updated_factory_block


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
        mock_transition.assert_awaited_once()
        transition_args = mock_transition.await_args
        assert transition_args is not None
        assert transition_args.args[1] == LABEL_SPEC_READY

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
        mock_transition_done.assert_awaited_once()
        done_args = mock_transition_done.await_args
        assert done_args is not None
        assert done_args.args[1] == LABEL_COMPLETED


async def test_run_issue_stage_empty_response_needs_human(tmp_path: Path) -> None:
    task = TaskContext(
        issue_number=13,
        title="Write specification",
        original_body="orig",
        labels=[LABEL_SPEC],
    )
    outcome = FactoryRunOutcome(
        succeeded=False,
        exit_code=1,
        attempts_used=1,
        total_duration_seconds=0.05,
        failure_reason="Agent produced no output (session aborted or failed)",
    )

    with (
        patch("acp_factory.runner.loop.fetch_issue", new=AsyncMock(return_value=task)),
        patch(
            "acp_factory.runner.loop.run_factory", new=AsyncMock(return_value=outcome)
        ),
        patch(
            "acp_factory.runner.loop.transition_issue_label", new=AsyncMock()
        ) as mock_transition,
    ):
        res = await run_issue_stage(tmp_path, 13)

    assert res.succeeded is False
    assert res.failure_reason == "Agent produced no output (session aborted or failed)"
    mock_transition.assert_awaited_once()
    needs_human_args = mock_transition.await_args
    assert needs_human_args is not None
    assert needs_human_args.args[1] == LABEL_NEEDS_HUMAN


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


async def test_run_branch_review_standalone_and_create_pr(tmp_path: Path) -> None:
    from acp_factory.runner.loop import run_branch_review

    accepted_outcome = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=0.4,
        last_response_text=(
            "### Risk Classification: LOW\n"
            "- **Reason**: Isolated feature branch.\n\n"
            "### Findings\n- None.\n\n"
            "STATUS: ACCEPTED"
        ),
    )

    with (
        patch(
            "acp_factory.runner.worktree._run_git",
            new=AsyncMock(side_effect=_fake_git),
        ),
        patch(
            "acp_factory.runner.loop.run_factory",
            new=AsyncMock(return_value=accepted_outcome),
        ),
        patch(
            "acp_factory.runner.loop.push_and_create_or_update_standalone_pr",
            new=AsyncMock(return_value="https://github.com/org/repo/pull/99"),
        ) as mock_pr,
    ):
        # 1. Dry-run with --create-pr generates local PR body and manual git/gh commands
        dry_res = await run_branch_review(
            tmp_path,
            description="Add pipeline retry logic",
            create_pr=True,
            dry_run=True,
        )
        assert dry_res.succeeded is True
        assert dry_res.dry_run_plan is not None
        assert dry_res.dry_run_plan.pr_body_file is not None
        assert dry_res.dry_run_plan.pr_body_file.exists()
        pr_body_text = dry_res.dry_run_plan.pr_body_file.read_text(encoding="utf-8")
        assert "### Risk Classification: LOW" in pr_body_text
        assert "Isolated feature branch." in pr_body_text
        assert any(
            "gh pr create --base main --head feat/my-branch" in cmd
            for cmd in dry_res.dry_run_plan.commands
        )
        mock_pr.assert_not_awaited()

        # 2. Live mode with --create-pr pushes and creates PR when ACCEPTED
        live_res = await run_branch_review(
            tmp_path,
            description="Add pipeline retry logic",
            create_pr=True,
            dry_run=False,
        )
        assert live_res.succeeded is True
        mock_pr.assert_awaited_once()
        assert live_res.stage_verdict is not None
        assert "pull/99" in live_res.stage_verdict.summary


def test_factory_meta_and_findings_primitives() -> None:
    block = set_factory_meta("## Specification\n- X", stage="review", review_round="1")
    assert parse_factory_meta(block) == {"stage": "review", "review_round": "1"}
    assert "## Specification" in block

    with_findings = replace_factory_findings(
        block, "## Findings\n- f1", round_num=1, max_rounds=3
    )
    assert "## Latest Review Findings (Round 1/3)" in with_findings
    assert "## Findings\n- f1" in with_findings
    assert "f1" in extract_factory_findings(with_findings)
    assert "f1" not in strip_factory_findings(with_findings)
    assert "<!-- factory:meta" not in strip_factory_meta(with_findings)
    assert clean_spec_block(with_findings) == "## Specification\n- X"


@pytest.mark.parametrize(
    ("original", "spec"),
    [
        (
            "Example of a factory block inside a code fence:\n"
            "```markdown\n"
            "<!-- factory:start -->\n"
            "example content\n"
            "<!-- factory:end -->\n"
            "```\n"
            "After the example.",
            "## Specification\n"
            "Markers to demonstrate:\n"
            "```html\n"
            "<!-- factory:start -->\n"
            "<!-- factory:end -->\n"
            "```\n"
            "Tail of spec.",
        ),
        (
            "~~~\n<!-- factory:start -->\n<!-- factory:end -->\n~~~\nReal body.",
            "## Specification\n- X",
        ),
    ],
    ids=["backtick_fences", "tilde_fences"],
)
def test_split_issue_body_preserves_code_block_delimiters(
    original: str, spec: str
) -> None:
    rendered = render_issue_body(original, spec)
    parsed_orig, parsed_block = split_issue_body(rendered)

    assert parsed_orig == original
    assert parsed_block == spec
    # A second round-trip must remain stable.
    assert split_issue_body(render_issue_body(parsed_orig, parsed_block)) == (
        original,
        spec,
    )

    # Unclosed fences and blank bodies have no live factory block.
    for raw in ("```\n<!-- factory:start -->\nunclosed", "", "   \n\t "):
        expected_orig = raw if raw.strip() else ""
        assert split_issue_body(raw) == (expected_orig, "")


def test_findings_helpers_preserve_code_block_markers() -> None:
    example = (
        "## Specification\n"
        "Documentation example:\n"
        "```markdown\n"
        "<!-- factory:findings:start -->\n"
        "example findings\n"
        "<!-- factory:findings:end -->\n"
        "```"
    )
    active = replace_factory_findings(
        example, "## Findings\n- real", round_num=1, max_rounds=3
    )

    extracted = extract_factory_findings(active)
    assert "## Findings\n- real" in extracted
    assert "example findings" not in extracted

    stripped = strip_factory_findings(active)
    assert "example findings" in stripped
    assert "<!-- factory:findings:start -->" in stripped
    assert "real" not in stripped

    # A dangling top-level start marker is still removed.
    dangling = "## Specification\n<!-- factory:findings:start -->\nleftover"
    assert "factory:findings:start" not in strip_factory_findings(dangling)


def test_factory_meta_helpers_preserve_code_block_markers() -> None:
    block = (
        "## Specification\n"
        "Meta example:\n"
        "```html\n"
        "<!-- factory:meta tokens=999 cost=99.0 turns=99 -->\n"
        "```"
    )
    assert parse_factory_meta(block) == {}

    updated = set_factory_meta(block, stage="review", tokens="5")
    assert parse_factory_meta(updated) == {"stage": "review", "tokens": "5"}
    assert "<!-- factory:meta tokens=999 cost=99.0 turns=99 -->" in updated

    stripped = strip_factory_meta(updated)
    assert "tokens=999" in stripped
    assert "stage=review" not in stripped

    assert parse_factory_meta("") == {}
    assert strip_factory_meta("") == ""


def test_spec_verdict_ignores_open_questions_in_code_blocks() -> None:
    response = (
        "# Specification\n\n"
        "## Behavior\n"
        "Example question template:\n"
        "```markdown\n"
        "## Open Questions\n"
        "1. Example question?\n"
        "```\n"
    )
    verdict = parse_stage_verdict("spec", response)
    assert verdict.status == "spec_ready"
    assert verdict.target_label == LABEL_SPEC_READY

    needs_human = parse_stage_verdict(
        "spec", "# Specification\n\n## Open Questions\n1. Real question?\n"
    )
    assert needs_human.status == "needs_human"
    assert needs_human.target_label == LABEL_NEEDS_HUMAN


def test_review_circuit_breaker_and_findings_replacement() -> None:
    spec = "## Specification\n- Add pagination."

    # Round 1: REVISION_NEEDED -> back to build with findings replaced in place
    v1 = parse_stage_verdict(
        "review",
        "src/api.py:10: missing bounds check.\nSTATUS: REVISION_NEEDED",
        current_factory_block=spec,
        reviewed_sha="sha-1",
    )
    assert v1.target_label == LABEL_BUILD
    assert v1.status == "revision_needed"
    assert "Round 1/3" in v1.updated_factory_block
    assert v1.updated_factory_block.count("## Latest Review Findings") == 1
    meta1 = parse_factory_meta(v1.updated_factory_block)
    assert meta1["review_round"] == "1"
    assert meta1["last_reviewed_sha"] == "sha-1"

    # Round 2: prior findings replaced, never stacked
    v2 = parse_stage_verdict(
        "review",
        "src/api.py:12: still missing.\nSTATUS: REVISION_NEEDED",
        current_factory_block=v1.updated_factory_block,
        reviewed_sha="sha-2",
    )
    assert v2.target_label == LABEL_BUILD
    assert "Round 2/3" in v2.updated_factory_block
    assert v2.updated_factory_block.count("## Latest Review Findings") == 1
    assert "missing bounds check" not in v2.updated_factory_block
    assert "still missing" in v2.updated_factory_block
    meta2 = parse_factory_meta(v2.updated_factory_block)
    assert meta2["review_round"] == "2"
    assert meta2["last_reviewed_sha"] == "sha-2"

    # Round 3: REVISION_NEEDED trips the circuit breaker -> needs-human
    v3 = parse_stage_verdict(
        "review",
        "still unresolved.\nSTATUS: REVISION_NEEDED",
        current_factory_block=v2.updated_factory_block,
        reviewed_sha="sha-3",
    )
    assert v3.target_label == LABEL_NEEDS_HUMAN
    assert v3.status == "needs_human"
    assert "Round 3/3" in v3.updated_factory_block
    assert "Review Loop Escalation" in v3.updated_factory_block
    meta3 = parse_factory_meta(v3.updated_factory_block)
    assert meta3["review_round"] == "3"


def test_review_accepted_strips_findings_and_clean_spec() -> None:
    spec = "## Specification\n- Add pagination."
    v_rev = parse_stage_verdict(
        "review",
        "finding one.\nSTATUS: REVISION_NEEDED",
        current_factory_block=spec,
        reviewed_sha="sha-1",
    )

    v_ok = parse_stage_verdict(
        "review",
        "All findings resolved.\nSTATUS: ACCEPTED",
        current_factory_block=v_rev.updated_factory_block,
        reviewed_sha="sha-2",
    )
    assert v_ok.target_label == LABEL_PR_READY
    assert v_ok.publish_pr is True
    assert "## Latest Review Findings" not in v_ok.updated_factory_block
    assert "finding one" not in v_ok.updated_factory_block
    assert clean_spec_block(v_ok.updated_factory_block) == spec


def test_delta_review_brief_includes_prior_findings() -> None:
    task = TaskContext(
        issue_number=5,
        title="x",
        factory_block=replace_factory_findings(
            "## Specification\n- X", "## Findings\n- f1", round_num=1, max_rounds=3
        ),
    )
    brief = build_delta_review_brief(task, last_reviewed_sha="abc1234", review_round=1)
    assert "abc1234..HEAD" in brief
    assert "{last_reviewed_sha}" not in brief
    assert "## Findings\n- f1" in brief
    assert "do NOT invent new findings" in brief


def test_delta_review_brief_rejects_malformed_sha() -> None:
    task = TaskContext(issue_number=6, title="x")
    brief = build_delta_review_brief(
        task, last_reviewed_sha="not-a-sha; rm -rf /", review_round=1
    )
    assert "not-a-sha; rm -rf /" not in brief
    assert "<invalid-sha>" in brief


def test_parse_review_round_tolerates_non_numeric() -> None:
    from acp_factory.task import parse_review_round

    assert parse_review_round({}) == 0
    assert parse_review_round({"review_round": "2"}) == 2
    assert parse_review_round({"review_round": "abc"}) == 0


@pytest.mark.parametrize(
    ("issue_num", "head_shas", "expected_label", "should_publish"),
    [
        (9, ["pre-sha", "post-sha"], LABEL_NEEDS_HUMAN, False),
        (10, ["pre-sha", "pre-sha"], LABEL_PR_READY, True),
    ],
    ids=["sha_mismatch_aborts", "sha_match_publishes"],
)
async def test_run_issue_stage_review_publish_sha_guard(
    tmp_path: Path,
    issue_num: int,
    head_shas: list[str],
    expected_label: str,
    should_publish: bool,
) -> None:
    (tmp_path / ".worktrees" / f"issue-{issue_num}").mkdir(parents=True)
    task = TaskContext(
        issue_number=issue_num,
        title="Add filter",
        original_body="orig",
        factory_block="## Specification\n- Add filter.",
        labels=[LABEL_REVIEW],
    )
    outcome = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=0.3,
        last_response_text="Looks good.\nSTATUS: ACCEPTED",
    )

    with (
        patch("acp_factory.runner.loop.fetch_issue", new=AsyncMock(return_value=task)),
        patch(
            "acp_factory.runner.loop.run_factory", new=AsyncMock(return_value=outcome)
        ),
        patch(
            "acp_factory.runner.loop.get_head_sha",
            new=AsyncMock(side_effect=head_shas),
        ),
        patch(
            "acp_factory.runner.loop.is_worktree_clean",
            new=AsyncMock(return_value=True),
        ),
        patch("acp_factory.runner.loop.update_issue_factory_block", new=AsyncMock()),
        patch(
            "acp_factory.runner.loop.transition_issue_label", new=AsyncMock()
        ) as mock_transition,
        patch(
            "acp_factory.runner.loop.push_and_create_or_update_pr", new=AsyncMock()
        ) as mock_pr,
    ):
        res = await run_issue_stage(tmp_path, issue_num)

    assert res.stage_verdict is not None
    assert res.stage_verdict.target_label == expected_label
    if should_publish:
        mock_pr.assert_awaited_once()
        assert "Reviewed-SHA: `pre-sha`" in mock_pr.call_args.kwargs["pr_body"]
    else:
        mock_pr.assert_not_awaited()
    mock_transition.assert_awaited_once()
    transition_args = mock_transition.await_args
    assert transition_args is not None
    assert transition_args.args[1] == expected_label


def test_has_existing_spec_detection() -> None:
    empty_task = TaskContext(issue_number=1, title="Test", original_body="body")
    assert not has_existing_spec(empty_task)

    triage_task = TaskContext(
        issue_number=2,
        title="Test",
        original_body="body",
        factory_block="## Triage Report (`OK`)\n\n### Codebase Inspection\nEverything looks good.",
    )
    assert not has_existing_spec(triage_task)

    spec_task_1 = TaskContext(
        issue_number=3,
        title="Test",
        original_body="body",
        factory_block="# Specification\n\n## Problem\nBug occurs.",
    )
    assert has_existing_spec(spec_task_1)

    spec_task_2 = TaskContext(
        issue_number=4,
        title="Test",
        original_body="body",
        factory_block="## Acceptance Criteria\n| ID | Done when | How to check |\n| AC-1 | Passes | pytest |",
    )
    assert has_existing_spec(spec_task_2)


def test_build_spec_revision_brief() -> None:
    task = TaskContext(
        issue_number=5,
        title="Test",
        original_body="body",
        factory_block="## Problem\nFoo\n\n## Acceptance Criteria\n| ID | Done when | How to check |\n| AC-1 | A | B |",
    )
    brief_no_comments = build_spec_revision_brief(task)
    assert "Spec Revision Brief" in brief_no_comments
    assert "SPEC_REVISION" in brief_no_comments
    assert "Do NOT re-explore the codebase from scratch" in brief_no_comments
    assert "Preserve existing problem statement" in brief_no_comments

    task.comments.append(IssueComment(author="stephanie", body="Add AC-2"))
    brief_with_comments = build_spec_revision_brief(task)
    assert "to incorporate the latest human comments." in brief_with_comments


async def test_run_issue_stage_spec_injects_revision_brief(tmp_path: Path) -> None:
    task = TaskContext(
        issue_number=11,
        title="Add filter",
        original_body="orig",
        factory_block="## Problem\nOriginal problem.\n\n## Acceptance Criteria\n| ID | Done when | How to check |\n| AC-1 | works | test |",
        labels=[LABEL_SPEC],
    )
    outcome = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=0.2,
        last_response_text="# Specification\n\n## Problem\nUpdated.\nSTATUS: SPEC_READY",
    )

    with (
        patch("acp_factory.runner.loop.fetch_issue", new=AsyncMock(return_value=task)),
        patch(
            "acp_factory.runner.loop.run_factory", new=AsyncMock(return_value=outcome)
        ) as mock_run_factory,
        patch("acp_factory.runner.loop.update_issue_factory_block", new=AsyncMock()),
        patch("acp_factory.runner.loop.transition_issue_label", new=AsyncMock()),
    ):
        res = await run_issue_stage(tmp_path, 11)
        assert res.stage_verdict is not None
        assert res.stage_verdict.status == "spec_ready"
        mock_run_factory.assert_awaited_once()
        passed_cfg = mock_run_factory.call_args[0][0]
        assert "Spec Revision Brief" in passed_cfg.task
        assert "SPEC_REVISION" in passed_cfg.task


def test_clean_markdown_and_verdict_preserve_status_inside_code_blocks() -> None:
    raw_spec = (
        "# Specification\n\n"
        "## Problem\n"
        "Need budget caps.\n\n"
        "## How to Test\n"
        "Example terminal failure output:\n"
        "```text\n"
        "Factory Run Failed\n"
        "Status: FAILED\n"
        "Attempts: 1 / 3\n"
        "Reason: Budget cap breached\n"
        "```\n\n"
        "## Out of Scope\n"
        "- Unrelated features.\n\n"
        "STATUS: SPEC_READY\n"
    )

    cleaned = _clean_markdown_stage_body(raw_spec)
    assert "Status: FAILED" in cleaned
    assert "## Out of Scope" in cleaned
    assert "STATUS: SPEC_READY" not in cleaned

    verdict = parse_stage_verdict("spec", raw_spec)
    assert verdict.status == "spec_ready"
    assert verdict.target_label == LABEL_SPEC_READY
    assert "Status: FAILED" in verdict.updated_factory_block
    assert "## Out of Scope" in verdict.updated_factory_block


def test_attribution_banner_render_place_and_strip() -> None:
    spec = "## Specification\n- Add pagination."

    block = set_attribution_banner(spec, stage="spec", agent="opencode")
    assert block.startswith("> 🤖 **acp-factory** | Stage: spec | Agent: opencode")

    rendered = render_issue_body("Original issue body.", block)
    assert (
        "<!-- factory:start -->\n> 🤖 **acp-factory** | Stage: spec | Agent: opencode"
    ) in rendered

    # When metadata is present the banner sits immediately after the machine-parseable line.
    meta_block = set_factory_meta(spec, tokens="10", cost="0.01", turns="1")
    with_meta = set_attribution_banner(meta_block, stage="build", agent="opencode")
    lines = with_meta.splitlines()
    assert lines[0].startswith("<!-- factory:meta ")
    assert lines[1] == "> 🤖 **acp-factory** | Stage: build | Agent: opencode"

    # Re-rendering replaces the stale banner instead of stacking a second one.
    updated = set_attribution_banner(with_meta, stage="review", agent="claude")
    assert updated.count("> 🤖 **acp-factory**") == 1
    assert "Stage: review" in updated
    assert "Stage: build" not in updated

    # Clean specs (used in PR bodies and downstream prompts) drop the banner.
    assert clean_spec_block(with_meta) == spec
    assert "🤖" not in clean_spec_block(with_meta)
    assert strip_attribution_banner(with_meta) == meta_block

    assert render_attribution_banner("triage", "opencode") == (
        "> 🤖 **acp-factory** | Stage: triage | Agent: opencode"
    )


def test_attribution_banner_rejects_malformed_fields() -> None:
    with pytest.raises(ValueError, match="stage"):
        render_attribution_banner("", "opencode")
    with pytest.raises(ValueError, match="agent"):
        render_attribution_banner("spec", "   ")

    # Newlines and separators in adapter-provided names cannot break the banner line.
    banner = render_attribution_banner("spec\nInjected", "agent|name")
    assert banner == "> 🤖 **acp-factory** | Stage: spec Injected | Agent: agent/name"
    assert len(banner.splitlines()) == 1


async def test_run_gh_factory_token_override(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            return b"ok", b""

    async def fake_exec(*args: object, **kwargs: Any) -> _FakeProc:
        captured.clear()
        captured.update(kwargs)
        return _FakeProc()

    with (
        patch.dict(
            "os.environ",
            {"GH_TOKEN": "human-token", "FACTORY_GH_TOKEN": "bot-token"},
            clear=True,
        ),
        patch("acp_factory.github.asyncio.create_subprocess_exec", new=fake_exec),
    ):
        out = await run_gh(["issue", "view", "1"], cwd=tmp_path)

    assert out == "ok"
    assert captured["env"]["GH_TOKEN"] == "bot-token"

    # Without a factory token the environment is left untouched (interactive `gh auth`).
    with (
        patch.dict("os.environ", {"GH_TOKEN": "human-token"}, clear=True),
        patch("acp_factory.github.asyncio.create_subprocess_exec", new=fake_exec),
    ):
        await run_gh(["issue", "view", "1"], cwd=tmp_path)

    assert captured.get("env") is None


def test_render_pr_body_preserves_external_text() -> None:
    from acp_factory.task import (
        FACTORY_REVIEW_END,
        FACTORY_REVIEW_START,
        extract_pr_review_section,
        render_pr_body_with_review,
    )

    original = "Human-written intro.\n\n## Summary\nKeep this section.\n"
    rendered = render_pr_body_with_review(original, "## Findings\n- first")

    assert rendered.startswith("Human-written intro.\n\n## Summary\nKeep this section.")
    assert FACTORY_REVIEW_START in rendered
    assert FACTORY_REVIEW_END in rendered
    assert extract_pr_review_section(rendered) == "## Findings\n- first"

    updated = render_pr_body_with_review(rendered, "## Findings\n- second")
    assert updated.count(FACTORY_REVIEW_START) == 1
    assert "first" not in updated
    assert "## Findings\n- second" in extract_pr_review_section(updated)
    assert "Human-written intro." in updated
    assert "Keep this section." in updated

    assert extract_pr_review_section("no managed review section") == ""

    empty = render_pr_body_with_review("", "## Findings\n- only")
    assert extract_pr_review_section(empty) == "## Findings\n- only"

    dangling = render_pr_body_with_review(
        "Human note.\n<!-- factory:review:start -->\nstale text", "## Findings\n- new"
    )
    assert dangling.count(FACTORY_REVIEW_START) == 1
    assert "stale text" not in dangling
    assert "Human note." in dangling
    assert extract_pr_review_section(dangling) == "## Findings\n- new"

    with pytest.raises(ValueError, match="review_report"):
        render_pr_body_with_review(original, "   ")


async def test_pr_review_body_ingestion_and_context(tmp_path: Path) -> None:
    pr_list = [{"number": 88, "url": "https://github.com/acme/api/pull/88"}]
    pr_view = {
        "body": (
            "Human PR note.\n"
            "<!-- factory:review:start -->\n"
            "## Findings\n- Add integration tests.\n"
            "<!-- factory:review:end -->\n"
            "Human footer."
        ),
        "comments": [],
        "reviews": [],
    }
    calls: list[list[str]] = []

    async def fake_run_gh(
        args: list[str], cwd: Path, *, input_text: str | None = None
    ) -> str:
        calls.append(args)
        if args[:2] == ["issue", "view"]:
            return json.dumps(_issue_payload())
        if args[:2] == ["pr", "list"]:
            return json.dumps(pr_list)
        if args[:2] == ["pr", "view"]:
            return json.dumps(pr_view)
        raise AssertionError(f"unexpected gh call: {args}")

    with patch("acp_factory.github.run_gh", new=AsyncMock(side_effect=fake_run_gh)):
        task = await fetch_issue(42, tmp_path, repo_slug="acme/api")

    assert task.pr_review_body == "## Findings\n- Add integration tests."
    pr_view_cmd = calls[2]
    assert pr_view_cmd[:2] == ["pr", "view"]
    assert "88" in pr_view_cmd
    assert "--json" in pr_view_cmd and "comments,reviews,body" in pr_view_cmd
    assert "--repo" in pr_view_cmd and "acme/api" in pr_view_cmd

    prompt_ctx = format_task_prompt_context(task)
    assert "## Latest PR Review Findings" in prompt_ctx
    assert "## Findings\n- Add integration tests." in prompt_ctx
    assert "Human PR note." not in prompt_ctx

    task_without_review = TaskContext(issue_number=1, title="No PR review")
    assert "## Latest PR Review Findings" not in format_task_prompt_context(
        task_without_review
    )


async def test_update_pr_review_section_refetches_and_preserves(tmp_path: Path) -> None:
    from acp_factory.github import update_pr_review_section
    from acp_factory.task import FACTORY_REVIEW_START, extract_pr_review_section

    task = TaskContext(
        issue_number=42,
        title="Add category filter",
        branch="factory/issue-42",
        pr_url="https://github.com/acme/api/pull/88",
    )
    calls: list[tuple[list[str], str | None]] = []

    async def fake_run_gh(
        args: list[str], cwd: Path, *, input_text: str | None = None
    ) -> str:
        calls.append((args, input_text))
        if args[:2] == ["pr", "list"]:
            return json.dumps(
                [
                    {
                        "number": 88,
                        "body": (
                            "Human PR description.\n"
                            "<!-- factory:review:start -->\n"
                            "## Findings\n- old finding\n"
                            "<!-- factory:review:end -->\n"
                            "Human footer."
                        ),
                    }
                ]
            )
        if args[:2] == ["pr", "edit"]:
            return ""
        raise AssertionError(f"unexpected gh call: {args}")

    with patch("acp_factory.github.run_gh", new=AsyncMock(side_effect=fake_run_gh)):
        new_body = await update_pr_review_section(
            task, "## Findings\n- new finding", tmp_path, repo_slug="acme/api"
        )

    pr_list_cmd = calls[0][0]
    assert pr_list_cmd[:2] == ["pr", "list"]
    assert "--head" in pr_list_cmd and "factory/issue-42" in pr_list_cmd
    assert "--state" in pr_list_cmd and "open" in pr_list_cmd
    assert "--json" in pr_list_cmd and "number,body" in pr_list_cmd
    assert "--repo" in pr_list_cmd and "acme/api" in pr_list_cmd

    pr_edit_cmd = calls[1][0]
    assert pr_edit_cmd[:2] == ["pr", "edit"]
    assert "88" in pr_edit_cmd
    assert "--body-file" in pr_edit_cmd
    assert "-" in pr_edit_cmd
    assert "--repo" in pr_edit_cmd and "acme/api" in pr_edit_cmd
    assert calls[1][1] == new_body
    assert "Human PR description." in new_body
    assert "Human footer." in new_body
    assert "old finding" not in new_body
    assert new_body.count(FACTORY_REVIEW_START) == 1
    assert extract_pr_review_section(new_body) == "## Findings\n- new finding"


async def test_review_updates_pr_body_when_pr_exists(tmp_path: Path) -> None:
    task = TaskContext(
        issue_number=21,
        title="Add filter",
        original_body="orig",
        factory_block=replace_factory_findings(
            "## Specification\n- Add filter.",
            "## Findings\n- stale fallback finding",
            round_num=1,
            max_rounds=3,
        ),
        labels=[LABEL_REVIEW],
        pr_url="https://github.com/acme/api/pull/88",
    )
    outcome = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=0.3,
        last_response_text="## Findings\n- Add tests.\nSTATUS: REVISION_NEEDED",
    )

    with (
        patch("acp_factory.runner.loop.fetch_issue", new=AsyncMock(return_value=task)),
        patch(
            "acp_factory.runner.loop.run_factory", new=AsyncMock(return_value=outcome)
        ),
        patch(
            "acp_factory.runner.loop.update_issue_factory_block", new=AsyncMock()
        ) as mock_issue,
        patch(
            "acp_factory.runner.loop.update_pr_review_section", new=AsyncMock()
        ) as mock_pr_review,
        patch("acp_factory.runner.loop.transition_issue_label", new=AsyncMock()),
        patch(
            "acp_factory.runner.loop.push_and_create_or_update_pr", new=AsyncMock()
        ) as mock_pr,
    ):
        res = await run_issue_stage(tmp_path, 21)

    assert res.stage_verdict is not None
    assert res.stage_verdict.target_label == LABEL_BUILD
    mock_pr_review.assert_awaited_once()
    assert "Add tests." in mock_pr_review.call_args.args[1]
    assert mock_pr_review.call_args.kwargs["cwd"] == tmp_path.resolve()

    issue_block = mock_issue.call_args.args[1]
    assert "factory:findings" not in issue_block
    assert "Add tests." not in issue_block
    assert "stale fallback finding" not in issue_block
    mock_pr.assert_not_awaited()


async def test_review_accept_with_existing_pr_embeds_section_without_double_edit(
    tmp_path: Path,
) -> None:
    (tmp_path / ".worktrees" / "issue-22").mkdir(parents=True)
    task = TaskContext(
        issue_number=22,
        title="Add filter",
        original_body="orig",
        factory_block="## Specification\n- Add filter.",
        labels=[LABEL_REVIEW],
        pr_url="https://github.com/acme/api/pull/88",
    )
    outcome = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=0.3,
        last_response_text="Looks good.\nSTATUS: ACCEPTED",
    )

    with (
        patch("acp_factory.runner.loop.fetch_issue", new=AsyncMock(return_value=task)),
        patch(
            "acp_factory.runner.loop.run_factory", new=AsyncMock(return_value=outcome)
        ),
        patch(
            "acp_factory.runner.loop.get_head_sha",
            new=AsyncMock(return_value="pre-sha"),
        ),
        patch(
            "acp_factory.runner.loop.is_worktree_clean",
            new=AsyncMock(return_value=True),
        ),
        patch("acp_factory.runner.loop.update_issue_factory_block", new=AsyncMock()),
        patch(
            "acp_factory.runner.loop.update_pr_review_section", new=AsyncMock()
        ) as mock_pr_review,
        patch(
            "acp_factory.runner.loop.push_and_create_or_update_pr", new=AsyncMock()
        ) as mock_pr,
        patch("acp_factory.runner.loop.transition_issue_label", new=AsyncMock()),
    ):
        res = await run_issue_stage(tmp_path, 22)

    assert res.stage_verdict is not None
    assert res.stage_verdict.target_label == LABEL_PR_READY
    mock_pr_review.assert_not_awaited()
    mock_pr.assert_awaited_once()
    pr_body = mock_pr.call_args.kwargs["pr_body"]
    assert "<!-- factory:review:start -->" in pr_body
    assert "Looks good." in pr_body


async def test_review_maintains_issue_meta(tmp_path: Path) -> None:
    from acp_factory.acp.events import UsageTotals

    (tmp_path / ".worktrees" / "issue-23").mkdir(parents=True)
    task = TaskContext(
        issue_number=23,
        title="Add filter",
        original_body="orig",
        factory_block=set_factory_meta(
            "## Specification\n- Add filter.",
            stage="review",
            review_round="1",
        ),
        labels=[LABEL_REVIEW],
        pr_url="https://github.com/acme/api/pull/88",
    )
    outcome = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=0.3,
        last_response_text="## Findings\n- Still missing tests.\nSTATUS: REVISION_NEEDED",
        usage=UsageTotals(tokens=1234, cost_usd=0.5, turns=2),
    )

    with (
        patch("acp_factory.runner.loop.fetch_issue", new=AsyncMock(return_value=task)),
        patch(
            "acp_factory.runner.loop.run_factory", new=AsyncMock(return_value=outcome)
        ),
        patch(
            "acp_factory.runner.loop.get_head_sha",
            new=AsyncMock(return_value="pre-sha"),
        ),
        patch(
            "acp_factory.runner.loop.is_worktree_clean",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "acp_factory.runner.loop.update_issue_factory_block", new=AsyncMock()
        ) as mock_issue,
        patch("acp_factory.runner.loop.update_pr_review_section", new=AsyncMock()),
        patch("acp_factory.runner.loop.transition_issue_label", new=AsyncMock()),
    ):
        await run_issue_stage(tmp_path, 23)

    issue_block = mock_issue.call_args.args[1]
    meta = parse_factory_meta(issue_block)
    assert meta["review_round"] == "2"
    assert meta["last_reviewed_sha"] == "pre-sha"
    assert meta["tokens"] == "1234"
    assert meta["cost"] == "0.5"
    assert meta["turns"] == "2"
    assert "factory:findings" not in issue_block
    assert "Still missing tests." not in issue_block


@pytest.mark.parametrize(
    (
        "pr_body_in",
        "verdict_text",
        "expected_in",
        "expected_not_in",
        "expect_warning",
    ),
    [
        (
            "",
            "## Findings\n- Add tests.\nSTATUS: REVISION_NEEDED",
            ["<!-- factory:review:start -->", "- Add tests."],
            [],
            True,
        ),
        (
            "Human-written PR intro.\n\n"
            "<!-- factory:review:start -->\n"
            "## Findings\n- old finding\n"
            "<!-- factory:review:end -->\n\n"
            "Human-written PR footer.",
            "## Findings\n- Add tests.\nSTATUS: REVISION_NEEDED",
            ["Human-written PR intro.", "Human-written PR footer.", "- Add tests."],
            ["old finding"],
            False,
        ),
        (
            "Human-written PR intro.\n\n"
            "<!-- factory:pr-description:start -->\n"
            "## Summary\nOld summary\n"
            "<!-- factory:pr-description:end -->\n\n"
            "<!-- factory:review:start -->\n"
            "## Findings\n- stale unresolved finding\n"
            "<!-- factory:review:end -->\n\n"
            "Human-written PR footer.",
            "STATUS: ACCEPTED",
            ["Human-written PR intro.", "Human-written PR footer."],
            [
                "stale unresolved finding",
                "<!-- factory:review:start -->",
                "<!-- factory:review:end -->",
            ],
            False,
        ),
    ],
    ids=["no_fetched_body_warns", "preserves_fetched_body", "clears_stale_on_accept"],
)
def test_dry_run_pr_edit_plan(
    tmp_path: Path,
    pr_body_in: str,
    verdict_text: str,
    expected_in: list[str],
    expected_not_in: list[str],
    expect_warning: bool,
) -> None:
    from acp_factory.github import build_dry_run_gh_plan

    task = TaskContext(
        issue_number=35,
        title="Add filter",
        original_body="orig",
        url="https://github.com/acme/api/issues/35",
        factory_block="## Specification\n- Add filter.",
        labels=[LABEL_REVIEW],
        pr_url="https://github.com/acme/api/pull/88",
        pr_body=pr_body_in,
    )
    verdict = parse_stage_verdict(
        "review",
        verdict_text,
        current_factory_block=task.factory_block,
        reviewed_sha="pre-sha",
    )

    plan = build_dry_run_gh_plan(
        task,
        verdict,
        workspace=tmp_path,
        run_dir=tmp_path / ".factory" / "runs" / "dry-run-test",
        reviewed_sha="pre-sha",
    )

    assert plan.pr_body_file is not None
    assert plan.pr_body_file.exists()
    pr_body = plan.pr_body_file.read_text(encoding="utf-8")
    for needle in expected_in:
        assert needle in pr_body
    for needle in expected_not_in:
        assert needle not in pr_body
    assert any(
        command.startswith("gh pr edit 88") and "--body-file" in command
        for command in plan.commands
    )
    assert any("WARNING" in command for command in plan.commands) is expect_warning
    if not pr_body_in:
        assert any("pr-35-body.md" in command for command in plan.commands)


@pytest.mark.parametrize(
    (
        "existing_body",
        "fresh_pr_body",
        "expected_in",
        "expected_not_in",
        "expect_review",
    ),
    [
        (
            "Human intro.\n\n"
            "Closes #42\n\n"
            "<!-- factory:pr-description:start -->\n"
            "## Summary\nOLD summary\n\n## Specification\nOLD spec\n\n"
            "Reviewed-SHA: `oldsha`\n"
            "<!-- factory:pr-description:end -->\n\n"
            "<!-- factory:review:start -->\n"
            "## Findings\n- old finding\n"
            "<!-- factory:review:end -->\n\n"
            "Human footer.",
            "<!-- factory:pr-description:start -->\n"
            "## Summary\nNEW summary\n\n## Specification\nNEW spec\n\n"
            "Reviewed-SHA: `newsha`\n"
            "<!-- factory:pr-description:end -->\n\n"
            "<!-- factory:review:start -->\n"
            "## Findings\n- new finding\n"
            "<!-- factory:review:end -->",
            [
                "Human intro.",
                "Human footer.",
                "Closes #42",
                "NEW summary",
                "NEW spec",
                "Reviewed-SHA: `newsha`",
            ],
            ["OLD summary", "OLD spec", "oldsha", "old finding"],
            True,
        ),
        (
            "Human intro.\n\n"
            "<!-- factory:pr-description:start -->\n"
            "## Summary\nAccepted summary\n"
            "<!-- factory:pr-description:end -->\n\n"
            "<!-- factory:review:start -->\n"
            "## Findings\n- stale unresolved finding\n"
            "<!-- factory:review:end -->\n\n"
            "Human footer.",
            "<!-- factory:pr-description:start -->\n"
            "## Summary\nAccepted summary\n"
            "<!-- factory:pr-description:end -->",
            ["Human intro.", "Human footer.", "Accepted summary"],
            [
                "stale unresolved finding",
                "<!-- factory:review:start -->",
                "<!-- factory:review:end -->",
            ],
            False,
        ),
    ],
    ids=["refreshes_managed_sections", "clears_stale_review_on_empty_accept"],
)
async def test_push_and_create_or_update_pr_managed_sections(
    tmp_path: Path,
    existing_body: str,
    fresh_pr_body: str,
    expected_in: list[str],
    expected_not_in: list[str],
    expect_review: bool,
) -> None:
    from acp_factory.github import push_and_create_or_update_pr
    from acp_factory.task import (
        FACTORY_PR_DESCRIPTION_START,
        extract_pr_description_section,
        extract_pr_review_section,
    )

    task = TaskContext(
        issue_number=42,
        title="Add filter",
        branch="factory/issue-42",
        pr_url="https://github.com/acme/api/pull/88",
    )

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_exec(*args: object, **kwargs: object) -> _FakeProc:
        return _FakeProc()

    calls: list[tuple[list[str], str | None]] = []

    async def fake_run_gh(
        args: list[str], cwd: Path, *, input_text: str | None = None
    ) -> str:
        calls.append((args, input_text))
        if args[:2] == ["pr", "list"]:
            return json.dumps(
                [{"number": 88, "url": "https://github.com/acme/api/pull/88"}]
            )
        if args[:2] == ["pr", "view"]:
            return json.dumps({"body": existing_body})
        if args[:2] == ["pr", "edit"]:
            return ""
        raise AssertionError(f"unexpected gh call: {args}")

    with (
        patch("acp_factory.github.run_gh", new=AsyncMock(side_effect=fake_run_gh)),
        patch("acp_factory.github.asyncio.create_subprocess_exec", new=fake_exec),
    ):
        await push_and_create_or_update_pr(task, pr_body=fresh_pr_body, cwd=tmp_path)

    edit_call = next(call for call in calls if call[0][:2] == ["pr", "edit"])
    updated = edit_call[1] or ""

    for needle in expected_in:
        assert needle in updated
    for needle in expected_not_in:
        assert needle not in updated
    assert updated.count(FACTORY_PR_DESCRIPTION_START) == 1
    if expect_review:
        assert extract_pr_description_section(updated) == (
            "## Summary\nNEW summary\n\n## Specification\nNEW spec\n\n"
            "Reviewed-SHA: `newsha`"
        )
        assert extract_pr_review_section(updated) == "## Findings\n- new finding"


def test_render_pr_description_section_rejects_bad_input() -> None:
    from acp_factory.task import (
        extract_pr_description_section,
        render_pr_body_with_managed_sections,
        render_pr_description_section,
    )

    with pytest.raises(ValueError, match="description"):
        render_pr_description_section("   ")
    with pytest.raises(ValueError, match="description"):
        render_pr_body_with_managed_sections("body", description="\n\t")
    assert extract_pr_description_section("no managed description") == ""
    assert (
        extract_pr_description_section("<!-- factory:pr-description:start -->\nstale")
        == ""
    )


async def test_update_pr_review_section_ignores_markers_inside_code_fences(
    tmp_path: Path,
) -> None:
    from acp_factory.github import update_pr_review_section
    from acp_factory.task import FACTORY_REVIEW_START

    task = TaskContext(
        issue_number=42,
        title="Add filter",
        branch="factory/issue-42",
        pr_url="https://github.com/acme/api/pull/88",
    )

    async def fake_run_gh(
        args: list[str], cwd: Path, *, input_text: str | None = None
    ) -> str:
        if args[:2] == ["pr", "list"]:
            return json.dumps(
                [{"number": 88, "body": "```\n<!-- factory:review:start -->\n```"}]
            )
        if args[:2] == ["pr", "edit"]:
            return ""
        raise AssertionError(f"unexpected gh call: {args}")

    with patch("acp_factory.github.run_gh", new=AsyncMock(side_effect=fake_run_gh)):
        new_body = await update_pr_review_section(
            task, "## Findings\n- fresh", tmp_path
        )

    assert new_body.count(FACTORY_REVIEW_START) == 2
    assert "## Findings\n- fresh" in new_body
    assert "```\n<!-- factory:review:start -->\n```" in new_body


@pytest.mark.parametrize(
    ("raw_body", "report", "expected_in", "expected_not_in", "expected_extracted"),
    [
        (
            "Human note.\n<!-- factory:review:end -->\nstray tail",
            "## Findings\n- new",
            ["Human note.", "stray tail"],
            [],
            "## Findings\n- new",
        ),
        (
            "<!-- factory:review:end -->",
            "## Findings\n- only",
            [],
            [],
            "## Findings\n- only",
        ),
        (
            "Intro\n<!-- factory:review:end -->\n"
            "<!-- factory:review:start -->\n## Findings\n- old\n<!-- factory:review:end -->\n\n"
            "Outro\n\n<!-- factory:review:start -->\nstray stale text\n<!-- factory:review:end -->",
            "## Findings\n- fresh",
            ["Intro", "Outro"],
            ["stray stale text"],
            "## Findings\n- fresh",
        ),
        (
            "Intro\n<!-- factory:review:start -->\n## Findings\n- old\n<!-- factory:review:end -->\n\n"
            "Outro\n\n<!-- factory:review:start -->\ndangling unclosed text",
            "## Findings\n- fresh",
            ["Outro"],
            ["dangling unclosed text"],
            "## Findings\n- fresh",
        ),
        (
            "Human PR note.",
            "## Findings\n- Check `<!-- factory:review:start -->` and `<!-- factory:review:end -->` handling.",
            ["Human PR note.", "(escaped)"],
            [],
            None,
        ),
    ],
    ids=[
        "dangling_end_marker",
        "end_marker_only",
        "trailing_dangling_markers",
        "trailing_unclosed_marker",
        "sanitizes_embedded_markers",
    ],
)
def test_render_pr_body_with_review_edge_cases(
    raw_body: str,
    report: str,
    expected_in: list[str],
    expected_not_in: list[str],
    expected_extracted: str | None,
) -> None:
    from acp_factory.task import (
        FACTORY_REVIEW_END,
        FACTORY_REVIEW_START,
        extract_pr_review_section,
        render_pr_body_with_review,
    )

    rendered = render_pr_body_with_review(raw_body, report)
    assert rendered.count(FACTORY_REVIEW_START) == 1
    assert rendered.count(FACTORY_REVIEW_END) == 1
    for needle in expected_in:
        assert needle in rendered
    for needle in expected_not_in:
        assert needle not in rendered
    if expected_extracted is not None:
        assert extract_pr_review_section(rendered) == expected_extracted
    else:
        assert "(escaped)" in extract_pr_review_section(rendered)
        re_rendered = render_pr_body_with_review(rendered, "## Findings\n- clean")
        assert extract_pr_review_section(re_rendered) == "## Findings\n- clean"
        assert "Human PR note." in re_rendered


def test_build_issue_pr_body_accepted_does_not_duplicate_review_report() -> None:
    from acp_factory.github import build_issue_pr_body
    from acp_factory.task import extract_pr_review_section
    from acp_factory.verdict import parse_stage_verdict

    task = TaskContext(
        issue_number=42,
        title="feat: sample",
        original_body="Spec body",
        factory_block="<!-- factory:meta stage=review -->",
        labels=["factory:review"],
        branch="factory/issue-42",
    )
    report_body = "### Findings\n- None.\n\n### Security Findings\n- None."
    verdict = parse_stage_verdict(
        "review",
        f"{report_body}\n\nSTATUS: ACCEPTED",
        current_factory_block=task.factory_block,
        reviewed_sha="deadbeef",
    )

    pr_body = build_issue_pr_body(task, verdict, reviewed_sha="deadbeef")
    assert pr_body.count("### Findings") == 1
    assert "Review accepted -> PR ready for Human Gate 2" in pr_body
    assert extract_pr_review_section(pr_body) == report_body


def test_render_neutralizes_spurious_prose_hash_refs_only_in_managed_prose() -> None:
    from acp_factory.task import (
        extract_pr_description_section,
        extract_pr_review_section,
        render_issue_body,
        render_pr_body_with_review,
        render_pr_description_section,
    )

    human_original = "Human note mentioning Behavior #1 and Issue #29."
    factory_block = (
        "See Behavior #1, Step #2, and AC #3.\n"
        "Preserve Issue #29, PR #41, Closes #29, and owner/repo#41.\n"
        "Preserve inline `Behavior #1` and fenced:\n"
        "```\n"
        "Finding #4\n"
        "```\n"
    )
    rendered_issue = render_issue_body(human_original, factory_block)
    assert human_original in rendered_issue
    assert "See Behavior 1, Step 2, and AC 3." in rendered_issue
    assert (
        "Preserve Issue #29, PR #41, Closes #29, and owner/repo#41." in rendered_issue
    )
    assert "`Behavior #1`" in rendered_issue
    assert "```\nFinding #4\n```" in rendered_issue

    desc = render_pr_description_section(
        "Implements Behavior #1 and Criterion #2 (see PR #41)."
    )
    assert extract_pr_description_section(desc) == (
        "Implements Behavior 1 and Criterion 2 (see PR #41)."
    )

    pr_body = render_pr_body_with_review(
        "Human PR text: Behavior #1",
        "1. The spec's Behavior #1 conflicts with Finding #2; see Issue #29 and `Step #3`.",
    )
    assert "Human PR text: Behavior #1" in pr_body
    assert extract_pr_review_section(pr_body) == (
        "1. The spec's Behavior 1 conflicts with Finding 2; see Issue #29 and `Step #3`."
    )


def _init_worktree_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "factory@example.com"], cwd=path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Factory Tests"], cwd=path, check=True
    )
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)
    return path.resolve()


def _add_issue_worktree(main_root: Path, issue_number: int) -> Path:
    wt_path = main_root / ".worktrees" / f"issue-{issue_number}"
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-q",
            "-b",
            f"factory/issue-{issue_number}",
            str(wt_path),
        ],
        cwd=main_root,
        check=True,
    )
    return wt_path


def _closed_worktree_gh(*args: object, **_kwargs: object) -> str:
    cmd = args[0]
    assert isinstance(cmd, list)
    if cmd[:2] == ["issue", "list"]:
        return "[]"
    if cmd[:2] == ["issue", "view"]:
        number = int(cmd[2])
        return json.dumps(
            {
                "number": number,
                "title": f"Closed issue {number}",
                "state": "CLOSED",
                "body": "",
                "labels": [{"name": LABEL_DONE}],
                "comments": [],
                "url": f"https://github.com/acme/api/issues/{number}",
            }
        )
    if cmd[:2] == ["pr", "list"]:
        return "[]"
    raise AssertionError(f"unexpected gh invocation: {cmd}")


@pytest.mark.parametrize(
    "scan_root",
    ["worktree_subdir", "main_subdir"],
    ids=["from_linked_worktree", "from_repo_subdirectory"],
)
async def test_worktree_scan_from_worktree(tmp_path: Path, scan_root: str) -> None:
    from acp_factory.github import list_actionable_issues, list_all_factory_issues

    main_root = _init_worktree_repo(tmp_path / "main")
    worktree = _add_issue_worktree(main_root, 5)
    if scan_root == "worktree_subdir":
        nested = worktree / "nested"
    else:
        nested = main_root / "src" / "pkg"
    nested.mkdir(parents=True)

    with patch(
        "acp_factory.github.run_gh",
        new=AsyncMock(side_effect=_closed_worktree_gh),
    ):
        actionable = await list_actionable_issues(nested, repo_slug="acme/api")
        all_issues = await list_all_factory_issues(nested, repo_slug="acme/api")

    assert [task.issue_number for task in actionable] == [5]
    assert [task.issue_number for task in all_issues] == [5]


def test_build_dry_run_gh_plan_worktree_removal_anchoring(tmp_path: Path) -> None:
    from acp_factory.github import build_dry_run_gh_plan

    main_root = _init_worktree_repo(tmp_path / "main")
    source_worktree = _add_issue_worktree(main_root, 10)
    task = TaskContext(
        issue_number=19,
        title="Anchor worktree removal",
        original_body="orig",
        factory_block="## Specification\n- Anchor it.",
        labels=[LABEL_DONE],
    )
    verdict = parse_stage_verdict(
        "done",
        "## Summary\nDone.\nSTATUS: ACCEPTED",
        current_factory_block=task.factory_block,
    )

    plan = build_dry_run_gh_plan(
        task,
        verdict,
        workspace=source_worktree,
        run_dir=tmp_path / "dry-run",
    )

    removal = next(command for command in plan.commands if "worktree remove" in command)
    assert str(main_root) in removal
    assert ".worktrees/issue-19" in removal
    assert ".worktrees/issue-10" not in removal


@pytest.mark.parametrize(
    "order",
    [
        ("acp_factory.github", "acp_factory.runner"),
        ("acp_factory.runner", "acp_factory.github"),
    ],
    ids=["github_first", "runner_first"],
)
def test_import_order_has_no_cycle(order: tuple[str, str]) -> None:
    subprocess.run(
        [sys.executable, "-c", f"import {order[0]}; import {order[1]}"],
        check=True,
    )


def test_build_dry_run_gh_plan_defaults_to_resolved_runs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: dry-run staging falls back to `<runs_dir>/dry-run`, not `.factory/runs/`."""
    from acp_factory.github import build_dry_run_gh_plan

    external = tmp_path / "external-runs"
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(external))
    task = TaskContext(
        issue_number=41,
        title="Dry run",
        original_body="orig",
        factory_block="## Specification\n- Keep it.",
        labels=[LABEL_REVIEW],
    )
    verdict = parse_stage_verdict(
        "review",
        "## Summary\nLooks good.\nSTATUS: ACCEPTED",
        current_factory_block=task.factory_block,
    )

    plan = build_dry_run_gh_plan(task, verdict, workspace=tmp_path)

    assert plan.pr_body_file is not None
    assert plan.pr_body_file.parent == external / "dry-run"
    assert plan.pr_body_file.is_file()
    assert not (tmp_path / ".factory" / "runs" / "dry-run").exists()


def test_build_dry_run_standalone_plan_defaults_to_resolved_runs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: standalone review dry-run staging uses the resolved runs_dir."""
    from acp_factory.github import build_dry_run_standalone_pr_plan

    external = tmp_path / "external-runs"
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(external))
    verdict = StageVerdict(
        stage="review",
        status="accepted",
        target_label=LABEL_PR_READY,
        updated_factory_block="",
        summary="ok",
    )

    plan = build_dry_run_standalone_pr_plan(
        "feature/x",
        pr_title="Review",
        pr_body="body",
        verdict=verdict,
        workspace=tmp_path,
    )

    assert plan.pr_body_file == external / "dry-run" / "pr-feature-x-body.md"
    assert plan.pr_body_file.is_file()
    assert not (tmp_path / ".factory" / "runs" / "dry-run").exists()
