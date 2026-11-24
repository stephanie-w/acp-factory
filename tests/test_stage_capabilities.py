"""Unit tests for Phase 3: StageCapabilities, flow resolution, admission, and flow-aware dispatcher."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from acp_factory.config import (
    FactoryConfig,
    FlowConfig,
    StageConfig,
    TransitionConfig,
)
from acp_factory.github import build_repo_admission, transition_issue_label
from acp_factory.runner.loop import run_issue_stage
from acp_factory.runner.orchestrator import FactoryRunOutcome
from acp_factory.task import TaskContext, extract_flow_from_labels


def _refactor_config() -> FactoryConfig:
    """Build a FactoryConfig with custom refactor stages and a `[flows.refactor]` state machine."""
    return FactoryConfig(
        stages={
            "refactor-plan": StageConfig(
                read_only=True,
                revision_brief=True,
            ),
            "refactor-apply": StageConfig(
                read_only=False,
                worktree=True,
            ),
            "refactor-review": StageConfig(
                read_only=True,
                worktree=True,
                require_clean_worktree=True,
                delta_review=True,
                inject_prior_notes_from="refactor-apply",
            ),
        },
        flows={
            "refactor": FlowConfig(
                entry="refactor-plan",
                transitions={
                    "refactor-plan": {
                        "READY": TransitionConfig(
                            label="factory:plan-ready",
                            action="update_issue_block",
                            block_mode="replace",
                        ),
                        "SPLIT_NEEDED": TransitionConfig(
                            label="factory:needs-human",
                            action="update_issue_block",
                            block_mode="replace",
                        ),
                    },
                    "refactor-apply": {
                        "DEFAULT": TransitionConfig(
                            label="factory:refactor-review",
                            action="none",
                            block_mode="preserve",
                            status="applied",
                        ),
                    },
                    "refactor-review": {
                        "ACCEPTED": TransitionConfig(
                            label="factory:pr-ready",
                            action="publish_pr",
                            block_mode="clear_findings",
                            record_review_report=True,
                        ),
                        "DEFAULT": TransitionConfig(
                            label="factory:refactor-apply",
                            action="update_issue_block",
                            block_mode="findings",
                            max_rounds=2,
                            record_review_report=True,
                        ),
                    },
                    "done": {
                        "DEFAULT": TransitionConfig(
                            label="factory:completed",
                            action="cleanup_worktree",
                            block_mode="preserve",
                        ),
                    },
                },
            ),
        },
    )


def test_stage_capabilities_combine_explicit_flags_and_legacy_defaults() -> None:
    """StageCapabilities reads explicit StageConfig flags and preserves PR defaults."""
    cfg = _refactor_config()

    # Legacy PR defaults still apply when stages are not explicitly declared
    assert cfg.stage_capabilities("build").worktree is True
    assert cfg.stage_capabilities("review").require_clean_worktree is True
    assert cfg.stage_capabilities("review").inject_prior_notes_from == "build"
    assert cfg.stage_capabilities("spec").revision_brief is True
    assert cfg.stage_capabilities("done").orchestrator_only is True

    # Custom refactor stage capabilities come from StageConfig
    assert cfg.stage_capabilities("refactor-plan").worktree is False
    assert cfg.stage_capabilities("refactor-plan").revision_brief is True
    assert cfg.stage_capabilities("refactor-apply").worktree is True
    assert cfg.stage_capabilities("refactor-apply").require_clean_worktree is False
    review_caps = cfg.stage_capabilities("refactor-review")
    assert review_caps.worktree is True
    assert review_caps.require_clean_worktree is True
    assert review_caps.delta_review is True
    assert review_caps.inject_prior_notes_from == "refactor-apply"


def test_resolve_issue_flow_explicit_inferred_and_mismatch() -> None:
    """`resolve_issue_flow` honors `flow:<name>`, infers unique owners, and rejects mismatches."""
    cfg = _refactor_config()

    # 1. Explicit flow:<name>
    name, flow = cfg.resolve_issue_flow(
        ["flow:refactor", "factory:refactor-plan"], stage="refactor-plan"
    )
    assert name == "refactor"
    assert flow.entry == "refactor-plan"

    # 2. Missing flow:<name> uniquely inferred from stage ownership
    inferred_name, _ = cfg.resolve_issue_flow(
        ["factory:refactor-plan"], stage="refactor-plan"
    )
    assert inferred_name == "refactor"

    # 3. Default PR flow when absent
    default_name, _ = cfg.resolve_issue_flow(["factory:spec"], stage="spec")
    assert default_name == "pr"

    # 4. Explicit flow mismatch fails fast
    with pytest.raises(ValueError, match="does not belong to flow 'pr'"):
        cfg.resolve_issue_flow(
            ["flow:pr", "factory:refactor-plan"], stage="refactor-plan"
        )

    # 5. Unknown explicit flow fails fast
    with pytest.raises(ValueError, match="Unknown flow 'unknown'"):
        cfg.resolve_issue_flow(["flow:unknown", "factory:triage"], stage="triage")


def test_build_repo_admission_respects_worktree_capabilities() -> None:
    """Read-only custom flow stages never consume branch slots; worktree stages do."""
    cfg = _refactor_config()
    plan_task = TaskContext(
        issue_number=1,
        title="Plan refactor",
        labels=["flow:refactor", "factory:refactor-plan"],
    )
    apply_task = TaskContext(
        issue_number=2,
        title="Apply refactor",
        labels=["flow:refactor", "factory:refactor-apply"],
    )
    pr_ready_task = TaskContext(
        issue_number=3,
        title="PR waiting merge",
        labels=["flow:refactor", "factory:pr-ready"],
    )

    admission = build_repo_admission(
        "acme/repo",
        [plan_task, apply_task, pr_ready_task],
        max_active_branches_per_repo=1,
        config=cfg,
    )
    assert [t.issue_number for t in admission.pr_ready_issues] == [3]
    assert [t.issue_number for t in admission.build_review_issues] == [2]
    assert [t.issue_number for t in admission.actionable_issues] == [1, 2]
    assert extract_flow_from_labels(plan_task.labels) == "refactor"


async def test_transition_issue_label_preserves_flow_label_and_replaces_custom_factory_label(
    tmp_path: Path,
) -> None:
    """`transition_issue_label` swaps `factory:*` stage labels while keeping `flow:*` intact."""
    task = TaskContext(
        issue_number=10,
        title="Extract seam",
        labels=["flow:refactor", "factory:refactor-plan", "enhancement"],
    )
    with patch("acp_factory.github.run_gh", new_callable=AsyncMock) as mock_gh:
        updated = await transition_issue_label(task, "factory:plan-ready", cwd=tmp_path)

    assert "flow:refactor" in updated
    assert "enhancement" in updated
    assert "factory:plan-ready" in updated
    assert "factory:refactor-plan" not in updated
    assert mock_gh.await_count == 3


async def test_run_issue_stage_dispatches_custom_refactor_flow_and_records_flow_meta(
    tmp_path: Path,
) -> None:
    """`run_issue_stage` resolves `flow:refactor`, runs `refactor-plan`, and transitions via FlowConfig."""
    cfg = _refactor_config()
    task = TaskContext(
        issue_number=25,
        title="Split loop.py",
        labels=["flow:refactor", "factory:refactor-plan"],
    )
    fake_outcome = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=0.2,
        last_response_text="# Refactor Contract\n\n## Invariant\n- Keep API.\n\nSTATUS: READY\n",
    )

    with (
        patch(
            "acp_factory.runner.loop.ensure_workspace_git_sync", new_callable=AsyncMock
        ),
        patch(
            "acp_factory.runner.loop.fetch_issue",
            new_callable=AsyncMock,
            return_value=task,
        ),
        patch(
            "acp_factory.runner.loop.run_factory",
            new_callable=AsyncMock,
            return_value=fake_outcome,
        ) as mock_run_factory,
        patch(
            "acp_factory.runner.loop.update_issue_factory_block",
            new_callable=AsyncMock,
        ) as mock_update_block,
        patch(
            "acp_factory.runner.loop.transition_issue_label",
            new_callable=AsyncMock,
        ) as mock_transition,
    ):
        outcome = await run_issue_stage(
            tmp_path,
            25,
            base_config=cfg,
            offline=True,
        )

    assert outcome.succeeded is True
    assert outcome.stage_verdict is not None
    assert outcome.stage_verdict.target_label == "factory:plan-ready"
    assert "flow=refactor" in outcome.stage_verdict.updated_factory_block
    assert mock_run_factory.await_args is not None
    assert mock_run_factory.await_args.kwargs["flow"] == "refactor"
    mock_update_block.assert_awaited_once()
    mock_transition.assert_awaited_once()
    assert mock_transition.await_args is not None
    assert mock_transition.await_args.args[1] == "factory:plan-ready"


async def test_dirty_worktree_on_custom_review_stage_bounces_to_flow_apply_label(
    tmp_path: Path,
) -> None:
    """A dirty worktree during `refactor-review` bounces to `factory:refactor-apply`, not `factory:build`."""
    cfg = _refactor_config()
    task = TaskContext(
        issue_number=26,
        title="Review seam",
        labels=["flow:refactor", "factory:refactor-review"],
    )
    wt_path = tmp_path / ".worktrees" / "issue-26"
    wt_path.mkdir(parents=True)

    with (
        patch(
            "acp_factory.runner.loop.ensure_workspace_git_sync", new_callable=AsyncMock
        ),
        patch(
            "acp_factory.runner.loop.fetch_issue",
            new_callable=AsyncMock,
            return_value=task,
        ),
        patch(
            "acp_factory.runner.loop.WorktreeManager.issue_worktree_path",
            return_value=wt_path,
        ),
        patch(
            "acp_factory.runner.loop.get_head_sha",
            new_callable=AsyncMock,
            return_value="abc1234",
        ),
        patch(
            "acp_factory.runner.loop.is_worktree_clean",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "acp_factory.runner.loop.transition_issue_label",
            new_callable=AsyncMock,
        ) as mock_transition,
    ):
        outcome = await run_issue_stage(
            tmp_path,
            26,
            base_config=cfg,
            offline=True,
        )

    assert outcome.succeeded is False
    mock_transition.assert_awaited_once()
    assert mock_transition.await_args is not None
    assert mock_transition.await_args.args[1] == "factory:refactor-apply"
