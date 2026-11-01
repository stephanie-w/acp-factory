"""Unit tests for fleet orchestration: config, admission control, repo sync, and review-round threading."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from acp_factory.config import FactoryConfig
from acp_factory.github import build_repo_admission
from acp_factory.runner.loop import FactoryRunOutcome
from acp_factory.runner.worktree import (
    detect_default_branch,
    ensure_fleet_repo_checkout,
)
from acp_factory.task import (
    LABEL_BUILD,
    LABEL_PR_READY,
    LABEL_REVIEW,
    LABEL_SPEC,
    TaskContext,
    parse_stage_verdict,
)


def _task(number: int, *labels: str) -> TaskContext:
    return TaskContext(
        issue_number=number, title=f"Issue {number}", labels=list(labels)
    )


def test_fleet_config_parsing_and_defaults() -> None:
    cfg = FactoryConfig.model_validate(
        {
            "fleet": {
                "repositories": {"acme/core": "~/DEV/core", "acme/api": "~/DEV/api"},
                "max_active_branches_per_repo": 2,
                "max_review_rounds": 5,
            }
        }
    )
    assert cfg.fleet is not None
    assert cfg.fleet.repositories == {
        "acme/core": "~/DEV/core",
        "acme/api": "~/DEV/api",
    }
    assert cfg.fleet.max_active_branches_per_repo == 2
    assert cfg.fleet.max_review_rounds == 5
    # fleet max_review_rounds wins over top-level
    assert cfg.effective_max_review_rounds == 5


def test_fleet_config_defaults_when_absent() -> None:
    cfg = FactoryConfig.model_validate({"max_review_rounds": 7})
    assert cfg.fleet is None
    assert cfg.effective_max_review_rounds == 7


def test_build_repo_admission_single_active_branch() -> None:
    tasks = [
        _task(1, LABEL_BUILD),
        _task(2, LABEL_BUILD),
        _task(3, LABEL_SPEC),
    ]
    admission = build_repo_admission("acme/repo", tasks, max_active_branches_per_repo=1)
    # Only one build/review issue is admitted (cap applied); no pr-ready
    assert admission.has_pr_ready is False
    assert admission.active_branch_count == 1
    assert [t.issue_number for t in admission.build_review_issues] == [1]
    # Read-only spec issue is still actionable (not branch-contending)
    assert [t.issue_number for t in admission.actionable_issues] == [1, 2, 3]


def test_build_repo_admission_pr_ready_counts_as_contending() -> None:
    tasks = [
        _task(1, LABEL_PR_READY),
        _task(2, LABEL_BUILD),
    ]
    admission = build_repo_admission("acme/repo", tasks, max_active_branches_per_repo=1)
    assert admission.has_pr_ready is True
    assert [t.issue_number for t in admission.pr_ready_issues] == [1]
    # The build is still tracked but pr-ready blocks it
    assert [t.issue_number for t in admission.build_review_issues] == [2]


def test_build_repo_admission_no_contending_issues() -> None:
    tasks = [_task(1, LABEL_SPEC), _task(2, LABEL_REVIEW)]
    # review is branch-contending; spec is not
    admission = build_repo_admission("acme/repo", tasks, max_active_branches_per_repo=2)
    assert admission.has_pr_ready is False
    assert admission.active_branch_count == 1
    assert [t.issue_number for t in admission.build_review_issues] == [2]


def test_build_repo_admission_pr_ready_higher_number_still_blocks() -> None:
    """A pr-ready issue with a HIGHER number than a queued build still blocks it (Finding 1)."""
    tasks = [
        _task(10, LABEL_BUILD),
        _task(11, LABEL_PR_READY),
    ]
    admission = build_repo_admission("acme/repo", tasks, max_active_branches_per_repo=1)
    assert admission.has_pr_ready is True
    assert [t.issue_number for t in admission.pr_ready_issues] == [11]


def test_max_review_rounds_threaded_through_parse() -> None:
    """The parse entrypoint honors a caller-provided cap instead of the module constant."""
    spec = "## Specification\n- X"
    # Round 1 with cap 2: REVISION_NEEDED -> build
    v1 = parse_stage_verdict(
        "review",
        "finding.\nSTATUS: REVISION_NEEDED",
        current_factory_block=spec,
        reviewed_sha="s1",
        max_review_rounds=2,
    )
    assert v1.target_label == LABEL_BUILD
    assert parse_factory_meta_helper(v1.updated_factory_block)["review_round"] == "1"

    # Round 2 with cap 2 trips the breaker -> needs-human
    v2 = parse_stage_verdict(
        "review",
        "still.\nSTATUS: REVISION_NEEDED",
        current_factory_block=v1.updated_factory_block,
        reviewed_sha="s2",
        max_review_rounds=2,
    )
    assert v2.target_label == "factory:needs-human"
    assert "Review Loop Escalation" in v2.updated_factory_block


def parse_factory_meta_helper(block: str) -> dict[str, str]:
    from acp_factory.task import parse_factory_meta

    return parse_factory_meta(block)


async def test_ensure_fleet_repo_checkout_local_path(tmp_path: Path) -> None:
    repo = tmp_path / "core"
    (repo / ".git").mkdir(parents=True)
    with patch(
        "acp_factory.runner.worktree._run_git",
        new=AsyncMock(return_value=(0, "", "")),
    ) as mock_git:
        checkout = await ensure_fleet_repo_checkout(
            "acme/core", repository_map_value=str(repo)
        )
        assert checkout == repo.resolve()
        # fetch --prune was invoked on the resolved checkout
        assert mock_git.await_args_list[-1].args[1:] == ("fetch", "--prune", "origin")


async def test_ensure_fleet_repo_checkout_missing_path_raises(tmp_path: Path) -> None:
    """An explicit local path that does not exist raises (no on-demand cloning)."""
    from acp_factory.runner.worktree import WorktreeError

    missing = tmp_path / "does-not-exist"
    with pytest.raises(WorktreeError):
        await ensure_fleet_repo_checkout("acme/core", repository_map_value=str(missing))


async def test_detect_default_branch(tmp_path: Path) -> None:
    with patch(
        "acp_factory.runner.worktree._run_git",
        new=AsyncMock(return_value=(0, "origin/main", "")),
    ):
        assert await detect_default_branch(tmp_path) == "main"

    with patch(
        "acp_factory.runner.worktree._run_git",
        new=AsyncMock(return_value=(0, "origin/develop", "")),
    ):
        assert await detect_default_branch(tmp_path) == "develop"


def test_resolve_repo_stage_overlays_checks(tmp_path: Path) -> None:
    central = FactoryConfig.model_validate(
        {
            "stages": {
                "build": {
                    "persona": "personas/implementer.md",
                    "checks": ["test"],
                }
            },
            "named_checks": {"test": {"name": "test", "command": "pytest"}},
        }
    )

    # Repo without .factory scaffolding inherits central config unchanged
    empty_repo = tmp_path / "empty"
    empty_repo.mkdir()
    resolved = central.resolve_repo_stage("build", empty_repo)
    assert [c.name for c in resolved.checks] == ["test"]

    # Repo with its own .factory/config.toml overrides pre + checks
    repo = tmp_path / "withcfg"
    (repo / ".factory").mkdir(parents=True)
    (repo / ".factory" / "config.toml").write_text(
        'version = "1"\n'
        "[stages.build]\n"
        'pre = ["uv sync"]\n'
        'checks = ["lint"]\n'
        "[checks.lint]\n"
        'command = "uv run ruff check ."\n',
        encoding="utf-8",
    )
    overlaid = central.resolve_repo_stage("build", repo)
    assert overlaid.setup == ["uv sync"]
    assert [c.name for c in overlaid.checks] == ["lint"]
    assert overlaid.checks[0].cmd == "uv run ruff check ."
    # Central persona is inherited (not overridden by repo)
    assert overlaid.agent.persona == "personas/implementer.md"


def test_dry_run_plan_non_accepted_review_has_no_publish_commands() -> None:
    from io import StringIO

    from rich.console import Console

    from acp_factory.github import DryRunPlan
    from acp_factory.ui.display import FactoryDisplay

    plan = DryRunPlan(
        issue_number=0,
        stage="review",
        target_label="factory:build",
        updated_factory_block="### Findings\n- [blocking] x",
        pr_body_file=Path("pr-body.md"),
        commands=["# Review verdict: revision_needed (saved to pr-body.md)"],
        verdict_status="revision_needed",
        publish_pr=False,
    )

    buf = StringIO()
    display = FactoryDisplay(custom_console=Console(file=buf, width=120))
    display.show_dry_run_plan(plan)
    out = buf.getvalue()

    assert "Review Not Accepted" in out
    assert "revision_needed" in out
    assert "Fix the findings" in out
    assert "Run these commands manually" not in out


def test_dry_run_plan_accepted_review_shows_commands() -> None:
    from io import StringIO

    from rich.console import Console

    from acp_factory.github import DryRunPlan
    from acp_factory.ui.display import FactoryDisplay

    plan = DryRunPlan(
        issue_number=0,
        stage="review",
        target_label="factory:pr-ready",
        updated_factory_block="### Findings\n- None",
        pr_body_file=Path("pr-body.md"),
        commands=["git push -u origin feat/x", "gh pr create ..."],
        verdict_status="accepted",
        publish_pr=True,
    )

    buf = StringIO()
    display = FactoryDisplay(custom_console=Console(file=buf, width=120))
    display.show_dry_run_plan(plan)
    out = buf.getvalue()

    assert "Run these commands manually" in out
    assert "git push -u origin feat/x" in out
    assert "Review Not Accepted" not in out


async def test_advance_fleet_repo_threads_central_config(tmp_path: Path) -> None:
    """The fleet runner passes the central config so per-repo stages inherit governance (Finding 1)."""
    from acp_factory.runner.loop import _advance_fleet_repo
    from acp_factory.task import LABEL_SPEC
    from acp_factory.ui.display import FactoryDisplay

    central = FactoryConfig.model_validate(
        {
            "fleet": {
                "repositories": {"acme/core": "~/DEV/core"},
                "max_review_rounds": 4,
            },
            "stages": {"spec": {"persona": "personas/architect.md", "checks": []}},
        }
    )

    spec_task = TaskContext(
        issue_number=1,
        title="x",
        labels=[LABEL_SPEC],
        url="https://github.com/acme/core/issues/1",
    )

    with (
        patch(
            "acp_factory.runner.loop.ensure_fleet_repo_checkout",
            new=AsyncMock(return_value=tmp_path),
        ),
        patch(
            "acp_factory.runner.loop.detect_default_branch",
            new=AsyncMock(return_value="main"),
        ),
        patch(
            "acp_factory.runner.loop.list_actionable_issues",
            new=AsyncMock(return_value=[spec_task]),
        ),
        patch(
            "acp_factory.runner.loop.run_issue_stage",
            new=AsyncMock(
                return_value=FactoryRunOutcome(
                    succeeded=True,
                    exit_code=0,
                    attempts_used=1,
                    total_duration_seconds=0.0,
                )
            ),
        ) as mock_run,
    ):
        result = await _advance_fleet_repo(
            central,
            "acme/core",
            str(tmp_path),
            dry_run=True,
            verbose=False,
            display=FactoryDisplay(),
        )
        assert result.error is None
        # Central config was threaded into run_issue_stage as base_config
        called_kwargs = mock_run.call_args.kwargs
        assert called_kwargs["base_config"] is central


async def test_advance_fleet_repo_pr_ready_blocks_new_build(tmp_path: Path) -> None:
    """A pr-ready issue blocks a second build branch; the pr-ready task is never run (Finding 2)."""
    from acp_factory.runner.loop import _advance_fleet_repo
    from acp_factory.task import LABEL_BUILD, LABEL_PR_READY
    from acp_factory.ui.display import FactoryDisplay

    central = FactoryConfig.model_validate(
        {"fleet": {"repositories": {"acme/core": "~/DEV/core"}}}
    )

    pr_ready_task = TaskContext(
        issue_number=1, title="awaiting merge", labels=[LABEL_PR_READY]
    )
    build_task = TaskContext(issue_number=2, title="next build", labels=[LABEL_BUILD])

    with (
        patch(
            "acp_factory.runner.loop.ensure_fleet_repo_checkout",
            new=AsyncMock(return_value=tmp_path),
        ),
        patch(
            "acp_factory.runner.loop.detect_default_branch",
            new=AsyncMock(return_value="main"),
        ),
        patch(
            "acp_factory.runner.loop.list_actionable_issues",
            new=AsyncMock(return_value=[pr_ready_task, build_task]),
        ),
        patch(
            "acp_factory.runner.loop.run_issue_stage",
            new=AsyncMock(
                return_value=FactoryRunOutcome(
                    succeeded=True,
                    exit_code=0,
                    attempts_used=1,
                    total_duration_seconds=0.0,
                )
            ),
        ) as mock_run,
    ):
        result = await _advance_fleet_repo(
            central,
            "acme/core",
            str(tmp_path),
            dry_run=True,
            verbose=False,
            display=FactoryDisplay(),
        )
        assert result.error is None
        # The pr-ready issue (1) is never passed to run_issue_stage; build (2) is not run either
        # because pr-ready blocks all build/review.
        run_numbers = [c.args[1] for c in mock_run.call_args_list]
        assert 1 not in run_numbers
        assert 2 not in run_numbers


def test_pr_ready_is_not_actionable_stage() -> None:
    """`factory:pr-ready` must not resolve to an actionable stage (Human Gate 2, not runnable)."""
    t = TaskContext(issue_number=1, title="x", labels=[LABEL_PR_READY])
    assert t.current_stage is None


def test_resolve_repo_stage_wraps_prompt_with_task_context(tmp_path: Path) -> None:
    """A stage prompt wraps the issue context instead of being overwritten (Finding 1)."""
    central = FactoryConfig.model_validate(
        {
            "stages": {
                "spec": {
                    "persona": "personas/architect.md",
                    "prompt": "prompts/SPEC.md",
                }
            }
        }
    )
    # Write the referenced prompt file so _load_markdown_or_inline resolves it.
    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "SPEC.md").write_text(
        "# Specification\nWrite a spec.", encoding="utf-8"
    )

    task_ctx = "## Original Issue Description\nAdd pagination."
    resolved = central.resolve_repo_stage(
        "spec", tmp_path, workspace=tmp_path, task=task_ctx
    )
    assert "# Specification" in resolved.task
    assert "## Task Context" in resolved.task
    assert "Add pagination." in resolved.task


def test_resolve_repo_stage_minimal_fleet_config(tmp_path: Path) -> None:
    """A minimal [fleet]-only config resolves via built-in defaults (Finding 3)."""
    minimal = FactoryConfig.model_validate(
        {"fleet": {"repositories": {"acme/core": "~/DEV/core"}}}
    )
    task_ctx = "## Original Issue Description\nSomething."
    resolved = minimal.resolve_repo_stage(
        "build", tmp_path, workspace=tmp_path, task=task_ctx
    )
    # Fell back to built-in defaults without raising; read_only set for build=False context
    assert resolved.active_stage == "build"
    assert resolved.task == task_ctx  # no prompt to wrap, so task stays as-is


def test_effective_max_review_rounds_precedence() -> None:
    """Top-level max_review_rounds is respected when [fleet].max_review_rounds is unset (Finding 4)."""
    cfg = FactoryConfig.model_validate(
        {"max_review_rounds": 5, "fleet": {"repositories": {"a/b": "~/DEV/b"}}}
    )
    assert cfg.effective_max_review_rounds == 5

    overridden = FactoryConfig.model_validate(
        {"max_review_rounds": 5, "fleet": {"max_review_rounds": 2}}
    )
    assert overridden.effective_max_review_rounds == 2


async def test_ensure_fleet_repo_checkout_rejects_invalid_slug(tmp_path: Path) -> None:
    """A hostile/malformed repo slug is rejected before any git command runs."""
    from acp_factory.runner.worktree import WorktreeError

    for bad_slug in ("../evil", "./x", "a/.hidden", "a", "a/b/c"):
        with patch(
            "acp_factory.runner.worktree._run_git",
            new=AsyncMock(return_value=(0, "", "")),
        ) as mock_git:
            with pytest.raises(WorktreeError):
                await ensure_fleet_repo_checkout(
                    bad_slug, repository_map_value=str(tmp_path)
                )
            # Validation must reject before running any git command
            mock_git.assert_not_awaited()

    # Sanity: a valid slug still works (validation is not over-broad).
    repo = tmp_path / "api"
    (repo / ".git").mkdir(parents=True)
    with patch(
        "acp_factory.runner.worktree._run_git",
        new=AsyncMock(return_value=(0, "", "")),
    ):
        result = await ensure_fleet_repo_checkout(
            "acme/api", repository_map_value=str(repo)
        )
        assert result == repo.resolve()


async def test_list_actionable_issues_retains_pr_ready_for_admission(
    tmp_path: Path,
) -> None:
    """The real list_actionable_issues (not a mock) retains pr-ready for fleet admission (Finding 1)."""
    import json

    from acp_factory.github import list_actionable_issues

    gh_items = [
        {
            "number": 1,
            "title": "awaiting merge",
            "state": "OPEN",
            "body": "",
            "labels": [{"name": "factory:pr-ready"}],
            "comments": [],
            "url": "https://github.com/acme/core/issues/1",
        },
        {
            "number": 2,
            "title": "next build",
            "state": "OPEN",
            "body": "",
            "labels": [{"name": "factory:build"}],
            "comments": [],
            "url": "https://github.com/acme/core/issues/2",
        },
    ]

    with patch(
        "acp_factory.github.run_gh",
        new=AsyncMock(return_value=json.dumps(gh_items)),
    ):
        # Without include_branch_contending: pr-ready dropped (non-fleet next behavior)
        plain = await list_actionable_issues(tmp_path, repo_slug="acme/core")
        assert [t.issue_number for t in plain] == [2]

        # With include_branch_contending: pr-ready retained for admission
        fleet = await list_actionable_issues(
            tmp_path, repo_slug="acme/core", include_branch_contending=True
        )
        assert [t.issue_number for t in fleet] == [1, 2]
        # pr-ready task has no actionable stage but is branch-contending
        pr_ready_task = next(t for t in fleet if t.issue_number == 1)
        assert pr_ready_task.current_stage is None
        assert LABEL_PR_READY in pr_ready_task.labels


async def test_detect_default_branch_falls_back_to_ls_remote(tmp_path: Path) -> None:
    """detect_default_branch falls back to ls-remote when origin/HEAD is absent (Finding 1)."""

    async def fake_git(cwd: Path, *args: str) -> tuple[int, str, str]:
        if args[:2] == ("symbolic-ref",):
            return (1, "", "ref not found")
        if args[0] == "ls-remote":
            return (0, "ref: refs/heads/main\tHEAD\nabcdef\tHEAD", "")
        return (0, "", "")

    with patch(
        "acp_factory.runner.worktree._run_git", new=AsyncMock(side_effect=fake_git)
    ):
        assert await detect_default_branch(tmp_path) == "main"


async def test_run_fleet_next_empty_repositories_returns_empty() -> None:
    """run_fleet_next returns [] instead of raising when [fleet.repositories] is empty (Finding 3)."""
    from acp_factory.runner.loop import run_fleet_next

    cfg = FactoryConfig.model_validate({"fleet": {}})
    result = await run_fleet_next(cfg)
    assert result == []


async def test_collect_fleet_status_does_not_clone(tmp_path: Path) -> None:
    """status --fleet lists via gh --repo without cloning/fetching (read-only)."""

    from acp_factory.runner.loop import collect_fleet_status

    cfg = FactoryConfig.model_validate(
        {"fleet": {"repositories": {"acme/core": str(tmp_path / "core")}}}
    )
    with (
        patch(
            "acp_factory.runner.loop.list_all_factory_issues",
            new=AsyncMock(return_value=[]),
        ) as mock_list,
        patch(
            "acp_factory.runner.worktree.ensure_fleet_repo_checkout",
            new=AsyncMock(),
        ) as mock_ensure,
    ):
        result = await collect_fleet_status(cfg)
        assert result == [("acme/core", [], None)]
        mock_list.assert_awaited_once()
        mock_ensure.assert_not_awaited()
