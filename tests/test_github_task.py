"""Unit tests for TaskContext, in-place Issue Body updates, and GitHub label stage verdicts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from acp_factory.runner.checks import parse_review_verdict_status
from acp_factory.runner.loop import (
    FactoryRunOutcome,
    build_delta_review_brief,
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
    TaskContext,
    clean_spec_block,
    extract_factory_findings,
    format_task_prompt_context,
    parse_factory_meta,
    parse_stage_verdict,
    render_issue_body,
    replace_factory_findings,
    set_factory_meta,
    split_issue_body,
    strip_factory_findings,
    strip_factory_meta,
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
            fake_task, LABEL_SPEC_READY, cwd=tmp_path.resolve(), repo_slug=None
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
            done_task, LABEL_COMPLETED, cwd=tmp_path.resolve(), repo_slug=None
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


async def test_run_issue_stage_review_publish_guard_aborts_on_sha_mismatch(
    tmp_path: Path,
) -> None:
    (tmp_path / ".worktrees" / "issue-9").mkdir(parents=True)
    task = TaskContext(
        issue_number=9,
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
            new=AsyncMock(side_effect=["pre-sha", "post-sha"]),
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
        res = await run_issue_stage(tmp_path, 9)
        assert res.stage_verdict is not None
        assert res.stage_verdict.target_label == LABEL_NEEDS_HUMAN
        mock_pr.assert_not_awaited()
        mock_transition.assert_awaited_once_with(
            task, LABEL_NEEDS_HUMAN, cwd=tmp_path.resolve(), repo_slug=None
        )


async def test_run_issue_stage_review_accepted_publishes_with_sha(
    tmp_path: Path,
) -> None:
    (tmp_path / ".worktrees" / "issue-10").mkdir(parents=True)
    task = TaskContext(
        issue_number=10,
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
            new=AsyncMock(return_value="pre-sha"),
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
        res = await run_issue_stage(tmp_path, 10)
        assert res.stage_verdict is not None
        assert res.stage_verdict.target_label == LABEL_PR_READY
        mock_pr.assert_awaited_once()
        assert "Reviewed-SHA: `pre-sha`" in mock_pr.call_args.kwargs["pr_body"]
        mock_transition.assert_awaited_once_with(
            task, LABEL_PR_READY, cwd=tmp_path.resolve(), repo_slug=None
        )
