"""Tests for Phase 4: bundled `refactor` flow, CLI `--flow`, `status`/`history` flow columns, and starter template."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from acp_factory.cli import app, history_cmd, status_cmd
from acp_factory.config import BUILTIN_FLOWS, DEFAULT_REFACTOR_FLOW, FactoryConfig
from acp_factory.resources import get_bundled_resources_dir
from acp_factory.runner.loop import run_issue_stage
from acp_factory.runner.orchestrator import FactoryRunOutcome
from acp_factory.task import TaskContext
from acp_factory.verdict import parse_stage_verdict


def test_builtin_refactor_flow_is_available_out_of_the_box() -> None:
    """`refactor` is bundled in `BUILTIN_FLOWS` with default stage capabilities."""
    cfg = FactoryConfig()
    assert "refactor" in BUILTIN_FLOWS
    assert cfg.get_flow("refactor") == DEFAULT_REFACTOR_FLOW

    assert cfg.stage_capabilities("refactor-plan").revision_brief is True
    assert cfg.stage_capabilities("refactor-plan").worktree is False
    assert cfg.stage_capabilities("refactor-apply").worktree is True
    review_caps = cfg.stage_capabilities("refactor-review")
    assert review_caps.worktree is True
    assert review_caps.require_clean_worktree is True
    assert review_caps.delta_review is True
    assert review_caps.inject_prior_notes_from == "refactor-apply"

    # Unique stage ownership infers `refactor` even without an explicit `flow:refactor` label
    flow_name, flow_cfg = cfg.resolve_issue_flow(
        ["factory:refactor-plan"], stage="refactor-plan"
    )
    assert flow_name == "refactor"
    verdict = parse_stage_verdict(
        "refactor-plan",
        "# Refactor Contract\n\n## Invariant\n- No behavior change.\n\nSTATUS: READY\n",
        flow=flow_cfg,
    )
    assert verdict.target_label == "factory:plan-ready"
    assert verdict.action == "update_issue_block"


def test_bundled_config_template_parses_with_refactor_stages() -> None:
    """`resources/templates/config.toml` parses cleanly and defines refactor stages and validator."""
    template_path = get_bundled_resources_dir() / "templates" / "config.toml"
    cfg = FactoryConfig.from_file(template_path)
    for stage_name in ("refactor-plan", "refactor-apply", "refactor-review"):
        assert stage_name in cfg.stages
    assert "validate-refactor-plan" in cfg.named_checks


def test_issue_cli_exposes_flow_option() -> None:
    """`acp-factory issue --help` exposes `--flow` / `-f`."""
    runner = CliRunner()
    res = runner.invoke(app, ["issue", "--help"])
    assert res.exit_code == 0
    assert "--flow" in res.output


async def test_run_issue_stage_honors_flow_override(tmp_path: Path) -> None:
    """`flow_override` selects the requested flow explicitly and rejects stage mismatches."""
    cfg = FactoryConfig()
    task = TaskContext(
        issue_number=30,
        title="Refactor parser",
        labels=["factory:refactor-plan"],
    )
    fake_outcome = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=0.1,
        last_response_text="# Refactor Contract\n\nSTATUS: READY\n",
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
        ),
        patch(
            "acp_factory.runner.loop.update_issue_factory_block",
            new_callable=AsyncMock,
        ),
        patch(
            "acp_factory.runner.loop.transition_issue_label",
            new_callable=AsyncMock,
        ),
    ):
        outcome = await run_issue_stage(
            tmp_path,
            30,
            flow_override="refactor",
            base_config=cfg,
            offline=True,
        )
        assert outcome.succeeded is True
        assert outcome.stage_verdict is not None
        assert outcome.stage_verdict.target_label == "factory:plan-ready"

        with pytest.raises(ValueError, match="does not belong to flow 'pr'"):
            await run_issue_stage(
                tmp_path,
                30,
                flow_override="pr",
                base_config=cfg,
                offline=True,
            )


def test_status_cmd_displays_flow_and_plan_ready_gate(tmp_path: Path) -> None:
    """`status` renders the `Flow` column, `refactor-plan` actionable state, and `plan-ready` gate."""
    tasks = [
        TaskContext(
            issue_number=11,
            title="Plan seam split",
            labels=["flow:refactor", "factory:refactor-plan"],
        ),
        TaskContext(
            issue_number=12,
            title="Contract ready",
            labels=["flow:refactor", "factory:plan-ready"],
        ),
    ]
    out = StringIO()
    with (
        patch(
            "acp_factory.cli.list_all_factory_issues",
            new_callable=AsyncMock,
            return_value=tasks,
        ),
        patch(
            "acp_factory.cli.console",
            new=Console(file=out, force_terminal=False, width=200),
        ),
    ):
        status_cmd(workspace=tmp_path, fleet=False)

    rendered = out.getvalue()
    assert "Flow" in rendered
    assert "refactor" in rendered
    assert "Actionable (refactor-plan)" in rendered
    assert "Human Gate 1 (Plan Approval)" in rendered


def test_history_cmd_displays_flow_column(tmp_path: Path) -> None:
    """`history` displays the `Flow` column (`refactor` when present, `-` otherwise)."""
    runs_dir = tmp_path / ".factory" / "runs"
    for run_id, ts, extra in (
        (
            "run-refactor",
            "2026-11-24T12:00:00+00:00",
            {"flow": "refactor", "stage": "refactor-plan"},
        ),
        ("run-default", "2026-11-24T11:00:00+00:00", {"stage": "build"}),
    ):
        d = runs_dir / run_id
        d.mkdir(parents=True)
        (d / "run.json").write_text(
            json.dumps({"run_id": run_id, "timestamp": ts, "succeeded": True, **extra}),
            encoding="utf-8",
        )

    out = StringIO()
    with patch(
        "acp_factory.cli.console",
        new=Console(file=out, force_terminal=False, width=200),
    ):
        history_cmd(workspace=tmp_path, limit=10, json_output=False)

    rendered = out.getvalue()
    assert "Flow" in rendered
    assert "refactor" in rendered
    assert "refactor-plan" in rendered
