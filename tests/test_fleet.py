"""Unit tests for fleet orchestration: config, admission control, repo sync, and review-round threading."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from acp_factory.config import FactoryConfig
from acp_factory.github import build_repo_admission
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
    return TaskContext(issue_number=number, title=f"Issue {number}", labels=list(labels))


def test_fleet_config_parsing_and_defaults() -> None:
    cfg = FactoryConfig.model_validate(
        {
            "fleet": {
                "workspaces_dir": "~/.acp-factory/repos",
                "org": "acme",
                "repositories": {"acme/core": "~/DEV/core", "acme/api": "auto"},
                "max_active_branches_per_repo": 2,
                "max_review_rounds": 5,
            }
        }
    )
    assert cfg.fleet is not None
    assert cfg.fleet.workspaces_dir == "~/.acp-factory/repos"
    assert cfg.fleet.org == "acme"
    assert cfg.fleet.repositories == {"acme/core": "~/DEV/core", "acme/api": "auto"}
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
    # Only one branch-contending issue is admitted as active
    assert admission.active_branch_count == 1
    assert [t.issue_number for t in admission.active_branch_issues] == [1]
    # Read-only spec issue is still actionable (not branch-contending)
    assert [t.issue_number for t in admission.actionable_issues] == [1, 2, 3]


def test_build_repo_admission_pr_ready_counts_as_contending() -> None:
    tasks = [
        _task(1, LABEL_PR_READY),
        _task(2, LABEL_BUILD),
    ]
    admission = build_repo_admission("acme/repo", tasks, max_active_branches_per_repo=1)
    assert admission.has_active_branch is True
    assert admission.active_branch_count == 1
    assert admission.active_branch_issues[0].issue_number == 1


def test_build_repo_admission_no_contending_issues() -> None:
    tasks = [_task(1, LABEL_SPEC), _task(2, LABEL_REVIEW)]
    # review is branch-contending; spec is not
    admission = build_repo_admission("acme/repo", tasks, max_active_branches_per_repo=2)
    assert admission.active_branch_count == 1
    assert admission.active_branch_issues[0].issue_number == 2


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


async def test_ensure_fleet_repo_checkout_auto_clone(tmp_path: Path) -> None:
    ws = tmp_path / "repos"

    async def fake_git(cwd: Path, *args: str) -> tuple[int, str, str]:
        if args[:2] == ("clone",):
            # Simulate a successful clone by creating a .git dir in the target
            target = cwd / args[2]
            (target / ".git").mkdir(parents=True, exist_ok=True)
            return (0, "", "")
        return (0, "", "")

    with patch(
        "acp_factory.runner.worktree._run_git", new=AsyncMock(side_effect=fake_git)
    ) as mock_git:
        checkout = await ensure_fleet_repo_checkout(
            "acme/api", repository_map_value="auto", workspaces_dir=str(ws)
        )
        assert checkout == (ws / "acme__api").resolve()
        assert mock_git.await_args_list[0].args[1:3] == (
            "clone",
            "https://github.com/acme/api.git",
        )


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
