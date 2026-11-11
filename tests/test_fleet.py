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


def test_repo_stage_ignores_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7: fleet `resolve_repo_stage` does not inherit the operator's user-level config."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    acp_dir = home / ".acp-factory"
    acp_dir.mkdir(parents=True)
    (acp_dir / "config.toml").write_text(
        'version = "1"\n'
        "[stages.build]\n"
        'pre = ["user-pre"]\n'
        'checks = ["user-check"]\n'
        "[checks.user-check]\n"
        'command = "user cmd"\n',
        encoding="utf-8",
    )

    central = FactoryConfig.model_validate(
        {
            "stages": {
                "build": {
                    "persona": "personas/implementer.md",
                    "pre": ["central-pre"],
                    "checks": ["test"],
                }
            },
            "named_checks": {"test": {"name": "test", "command": "pytest"}},
        }
    )

    bare_repo = tmp_path / "bare"
    bare_repo.mkdir()
    resolved = central.resolve_repo_stage("build", bare_repo)

    assert resolved.setup == ["central-pre"]
    assert [c.name for c in resolved.checks] == ["test"]


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


async def test_run_fleet_next_isolates_repo_errors(tmp_path: Path) -> None:
    """A failing repo must never block the next repo from advancing (Finding 1)."""
    from acp_factory.runner.loop import FactoryRunOutcome, run_fleet_next
    from acp_factory.runner.worktree import WorktreeError

    cfg = FactoryConfig.model_validate(
        {"fleet": {"repositories": {"acme/a": "~/DEV/a", "acme/b": "~/DEV/b"}}}
    )

    async def fake_checkout(repo_slug: str, *, repository_map_value: str) -> Path:
        if repo_slug == "acme/a":
            raise WorktreeError("missing checkout")
        return tmp_path

    spec_task = TaskContext(issue_number=1, title="x", labels=[LABEL_SPEC])

    with (
        patch(
            "acp_factory.runner.loop.ensure_fleet_repo_checkout",
            new=AsyncMock(side_effect=fake_checkout),
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
        ),
    ):
        results = await run_fleet_next(cfg)

    assert [r.repo_slug for r in results] == ["acme/a", "acme/b"]
    assert results[0].error is not None
    assert "missing checkout" in results[0].error
    assert results[1].error is None
    assert results[1].issues_processed == 1


async def test_collect_fleet_status_missing_checkout_uses_valid_cwd(
    tmp_path: Path,
) -> None:
    """A not-yet-cloned fleet repo lists remote issues via a valid cwd (Finding 3)."""
    from acp_factory.runner.loop import collect_fleet_status

    missing = tmp_path / "not-cloned"
    cfg = FactoryConfig.model_validate(
        {"fleet": {"repositories": {"acme/core": str(missing)}}}
    )
    with patch(
        "acp_factory.runner.loop.list_all_factory_issues",
        new=AsyncMock(return_value=[]),
    ) as mock_list:
        result = await collect_fleet_status(cfg)

    assert result == [("acme/core", [], None)]
    call = mock_list.call_args
    assert call is not None
    # cwd is positional; must exist or create_subprocess_exec(..., cwd=...) raises.
    assert call.args[0].exists()
    assert call.kwargs["repo_slug"] == "acme/core"
    assert call.kwargs["scan_worktrees"] is False


async def test_run_issue_stage_consumes_base_config_overlay(tmp_path: Path) -> None:
    """run_issue_stage resolves fleet base_config via resolve_repo_stage (Finding 1)."""
    from acp_factory.runner.loop import FactoryRunOutcome, run_issue_stage

    repo = tmp_path / "core"
    (repo / ".factory").mkdir(parents=True)
    (repo / ".factory" / "config.toml").write_text(
        'version = "1"\n'
        "[stages.spec]\n"
        'pre = ["uv sync"]\n'
        'checks = ["lint"]\n'
        "[checks.lint]\n"
        'command = "uv run ruff check ."\n',
        encoding="utf-8",
    )

    central = FactoryConfig.model_validate(
        {
            "fleet": {"repositories": {"acme/core": str(repo)}},
            "stages": {
                "spec": {
                    "persona": "personas/architect.md",
                    "checks": ["test"],
                }
            },
            "named_checks": {"test": {"name": "test", "command": "pytest"}},
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
            "acp_factory.runner.loop.fetch_issue",
            new=AsyncMock(return_value=spec_task),
        ),
        patch(
            "acp_factory.runner.loop.run_factory",
            new=AsyncMock(
                return_value=FactoryRunOutcome(
                    succeeded=True,
                    exit_code=0,
                    attempts_used=1,
                    total_duration_seconds=0.0,
                    last_response_text="",
                    run_dir=tmp_path / "runs",
                )
            ),
        ) as mock_run_factory,
    ):
        outcome = await run_issue_stage(
            repo,
            1,
            base_config=central,
            repo_slug="acme/core",
            dry_run=True,
        )
        assert outcome.succeeded

    resolved = mock_run_factory.call_args.args[0]
    # Central persona is inherited; repo pre + checks overlay the central stage.
    assert resolved.agent.persona == "personas/architect.md"
    assert resolved.setup == ["uv sync"]
    assert [c.name for c in resolved.checks] == ["lint"]
    assert resolved.checks[0].cmd == "uv run ruff check ."
    assert mock_run_factory.call_args.kwargs["repo_slug"] == "acme/core"
    assert mock_run_factory.call_args.kwargs["issue_number"] == 1


def test_display_run_header_and_attempt_banner_context() -> None:
    from io import StringIO

    from rich.console import Console

    from acp_factory.config import FactoryConfig
    from acp_factory.ui.display import FactoryDisplay

    cfg = FactoryConfig.model_validate(
        {
            "task": "Test task",
            "stages": {
                "spec": {
                    "agent": "opencode",
                    "mode": "plan",
                    "read_only": True,
                }
            },
        }
    )
    cfg.active_stage = "spec"

    buf = StringIO()
    display = FactoryDisplay(custom_console=Console(file=buf, width=120))
    display.show_run_header(
        cfg,
        Path("/tmp/repo"),
        repo_slug="acme/widget",
        issue_number=42,
        issue_title="fix widget alignment",
    )
    display.show_attempt_banner(1, 3, context="#42 · spec")

    output = buf.getvalue()
    assert "acme/widget" in output
    assert "#42" in output
    assert "fix widget alignment" in output
    assert "spec" in output
    assert "Worker Attempt 1 / 3 (#42 · spec)" in output


async def test_run_fleet_next_respects_limit(tmp_path: Path) -> None:
    """run_fleet_next halts once `limit` total issues across repos are processed."""
    from acp_factory.runner.loop import FactoryRunOutcome, run_fleet_next

    cfg = FactoryConfig.model_validate(
        {"fleet": {"repositories": {"acme/a": "~/DEV/a", "acme/b": "~/DEV/b"}}}
    )

    t1 = TaskContext(issue_number=1, title="issue 1", labels=[LABEL_SPEC])
    t2 = TaskContext(issue_number=2, title="issue 2", labels=[LABEL_SPEC])

    mock_run_stage = AsyncMock(
        return_value=FactoryRunOutcome(
            succeeded=True,
            exit_code=0,
            attempts_used=1,
            total_duration_seconds=0.0,
        )
    )

    with (
        patch(
            "acp_factory.runner.loop.ensure_fleet_repo_checkout",
            new=AsyncMock(return_value=tmp_path),
        ),
        patch(
            "acp_factory.runner.loop.list_actionable_issues",
            new=AsyncMock(return_value=[t1, t2]),
        ),
        patch("acp_factory.runner.loop.run_issue_stage", new=mock_run_stage),
    ):
        results = await run_fleet_next(cfg, limit=1)

    assert mock_run_stage.call_count == 1
    assert len(results) == 1
    assert results[0].repo_slug == "acme/a"
    assert results[0].issues_processed == 1


def test_cli_next_and_status_limit_help() -> None:
    """Verify that --limit / -n is exposed in CLI help for both next and status."""
    from typer.testing import CliRunner

    from acp_factory.cli import app

    runner = CliRunner()
    res_next = runner.invoke(app, ["next", "--help"])
    assert res_next.exit_code == 0
    assert "--limit" in res_next.output or "-n" in res_next.output

    res_status = runner.invoke(app, ["status", "--help"])
    assert res_status.exit_code == 0
    assert "--limit" in res_status.output or "-n" in res_status.output


def test_cli_next_fleet_effective_limit_defaults(tmp_path: Path) -> None:
    """CLI next in fleet mode defaults to limit=1 when neither --limit nor --all is passed."""
    from unittest.mock import AsyncMock, patch

    from typer.testing import CliRunner

    from acp_factory.cli import app
    from acp_factory.config import FactoryConfig

    cfg = FactoryConfig.model_validate(
        {"fleet": {"repositories": {"acme/repo": str(tmp_path)}}}
    )

    runner = CliRunner()
    with (
        patch("acp_factory.config.FactoryConfig.discover", return_value=cfg),
        patch(
            "acp_factory.runner.loop.run_fleet_next", new_callable=AsyncMock
        ) as mock_fleet_next,
    ):
        mock_fleet_next.return_value = []
        # Case 1: no flags -> effective_limit=1
        res = runner.invoke(app, ["next", "--fleet"])
        assert res.exit_code == 0
        assert mock_fleet_next.call_args.kwargs["limit"] == 1

        # Case 2: --all -> effective_limit=None
        mock_fleet_next.reset_mock()
        res = runner.invoke(app, ["next", "--fleet", "--all"])
        assert res.exit_code == 0
        assert mock_fleet_next.call_args.kwargs["limit"] is None

        # Case 3: --limit 5 -> effective_limit=5
        mock_fleet_next.reset_mock()
        res = runner.invoke(app, ["next", "--fleet", "-n", "5"])
        assert res.exit_code == 0
        assert mock_fleet_next.call_args.kwargs["limit"] == 5
