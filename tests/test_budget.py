"""Unit and integration tests for budget cap enforcement and usage telemetry."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from acp_factory.acp.client import TurnResult
from acp_factory.acp.events import TurnSummary, UsageTotals, extract_usage_metrics
from acp_factory.config import (
    BudgetConfig,
    CheckConfig,
    FactoryConfig,
    RetryConfig,
    StageConfig,
)
from acp_factory.runner.loop import (
    FactoryRunOutcome,
    check_turn_cap,
    check_usage_cap,
    run_factory,
    run_issue_stage,
)
from acp_factory.task import (
    LABEL_BUILD,
    LABEL_NEEDS_HUMAN,
    TaskContext,
    budget_usage_from_meta,
    parse_factory_meta,
    render_budget_escalation,
    set_factory_meta,
)

_FAIL_CHECK = "python3 -c 'import sys; sys.exit(1)'"


def _fake_client(usage: dict | None = None):
    """Return a FakeACPClient class plus the list of prompts it receives."""
    prompts: list[str] = []

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.tracker = AsyncMock()
            self.tracker.history = []

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def send_prompt(self, prompt: str, **kwargs: object) -> TurnResult:
            prompts.append(prompt)
            return TurnResult(
                stop_reason="end_turn",
                summary=TurnSummary(
                    prompt=prompt,
                    response_text="ok",
                    thought_text="",
                    stop_reason="end_turn",
                    duration_seconds=0.01,
                    tool_call_count=0,
                    usage=dict(usage) if usage else None,
                ),
            )

    return FakeClient, prompts


def test_extract_usage_metrics_bad_inputs() -> None:
    """Usage telemetry is defensive: malformed values default to zero, never coerced."""
    assert extract_usage_metrics(None) == (0, 0.0)
    assert extract_usage_metrics({}) == (0, 0.0)
    assert extract_usage_metrics({"size": 100}) == (0, 0.0)

    # Malformed / negative / non-finite values are rejected to zero.
    assert extract_usage_metrics({"used": "lots", "cost": {"amount": "many"}}) == (
        0,
        0.0,
    )
    assert extract_usage_metrics({"used": -5, "cost": {"amount": -1.0}}) == (0, 0.0)
    assert extract_usage_metrics(
        {"used": float("nan"), "cost": {"amount": float("inf")}}
    ) == (0, 0.0)
    assert extract_usage_metrics({"used": True, "cost": {"amount": True}}) == (0, 0.0)

    # dict-style cost and object-style cost are both supported.
    assert extract_usage_metrics(
        {"used": 10, "cost": {"amount": 0.05, "currency": "USD"}}
    ) == (
        10,
        0.05,
    )

    class Cost:
        amount = 0.05
        currency = "USD"

    assert extract_usage_metrics({"used": 10, "cost": Cost()}) == (10, 0.05)


def test_budget_breach_message_helpers() -> None:
    """Breach messages are deterministic and only fire for the configured caps."""
    unlimited = BudgetConfig()
    assert check_turn_cap(0, unlimited) is None
    assert check_usage_cap(0, 0.0, unlimited) is None

    assert check_turn_cap(1, BudgetConfig(max_turns=1)) == (
        "Budget cap breached: turn count (1) reached max_turns (1)"
    )
    assert check_usage_cap(101, 0.0, BudgetConfig(max_tokens_per_run=100)) == (
        "Budget cap breached: cumulative tokens 101 exceeded max_tokens_per_run (100)"
    )
    assert check_usage_cap(0, 1.5, BudgetConfig(max_cost_usd=1.0)) == (
        "Budget cap breached: cumulative cost $1.50 exceeded max_cost_usd ($1.00)"
    )
    # At exactly the cap, no breach is reported (limits are inclusive).
    assert (
        check_usage_cap(
            100, 1.0, BudgetConfig(max_tokens_per_run=100, max_cost_usd=1.0)
        )
        is None
    )


def test_render_budget_escalation() -> None:
    block = render_budget_escalation(
        reason="Budget cap breached: cumulative tokens 125,000 exceeded max_tokens_per_run (100,000)",
        total_tokens=125_000,
        total_cost_usd=0.85,
        turns_used=2,
    )
    assert block.startswith("## Budget Escalation")
    assert "**Cumulative Tokens:** 125,000" in block
    assert "**Cumulative Cost (USD):** $0.85" in block
    assert "**Turns Completed:** 2" in block


async def test_turn_cap_halts_loop(tmp_path: Path) -> None:
    """AC-3: cumulative turns reaching max_turns halts the loop before a second turn."""
    cfg = FactoryConfig(
        task="do work",
        budget=BudgetConfig(max_turns=1),
        checks=[CheckConfig(name="always-fail", cmd=_FAIL_CHECK)],
        retry=RetryConfig(max_attempts=3),
    )
    fake, prompts = _fake_client()
    with patch("acp_factory.runner.loop.FactoryACPClient", fake):
        outcome = await run_factory(cfg, tmp_path)

    assert outcome.succeeded is False
    assert outcome.budget_breached is True
    assert outcome.attempts_used == 1
    assert len(prompts) == 1
    assert "turn count (1) reached max_turns (1)" in (outcome.failure_reason or "")
    assert outcome.usage.turns == 1


async def test_token_cap_halts_loop(tmp_path: Path) -> None:
    """AC-4: cumulative tokens exceeding max_tokens_per_run halts the loop."""
    cfg = FactoryConfig(
        task="do work",
        budget=BudgetConfig(max_tokens_per_run=100),
        checks=[CheckConfig(name="always-fail", cmd=_FAIL_CHECK)],
        retry=RetryConfig(max_attempts=5),
    )
    fake, prompts = _fake_client({"used": 60, "size": 1000, "cost": None})
    with patch("acp_factory.runner.loop.FactoryACPClient", fake):
        outcome = await run_factory(cfg, tmp_path)

    assert outcome.succeeded is False
    assert outcome.budget_breached is True
    assert len(prompts) == 2
    assert "cumulative tokens 120 exceeded max_tokens_per_run (100)" in (
        outcome.failure_reason or ""
    )
    assert outcome.usage.tokens == 120
    assert outcome.usage.turns == 2


async def test_cost_cap_halts_loop(tmp_path: Path) -> None:
    """AC-5: cumulative cost exceeding max_cost_usd halts the loop."""
    cfg = FactoryConfig(
        task="do work",
        budget=BudgetConfig(max_cost_usd=0.4),
        checks=[CheckConfig(name="always-fail", cmd=_FAIL_CHECK)],
        retry=RetryConfig(max_attempts=5),
    )
    fake, prompts = _fake_client(
        {"used": 10, "size": 1000, "cost": {"amount": 0.25, "currency": "USD"}}
    )
    with patch("acp_factory.runner.loop.FactoryACPClient", fake):
        outcome = await run_factory(cfg, tmp_path)

    assert outcome.succeeded is False
    assert outcome.budget_breached is True
    assert len(prompts) == 2
    assert "cumulative cost $0.50 exceeded max_cost_usd ($0.40)" in (
        outcome.failure_reason or ""
    )
    assert outcome.usage.cost_usd == 0.5


async def test_budget_escalation_transitions_issue(tmp_path: Path) -> None:
    """AC-6: a budget breach updates the factory block and transitions to needs-human."""
    task = TaskContext(
        issue_number=6,
        title="budget test",
        labels=[LABEL_BUILD],
        url="https://github.com/acme/repo/issues/6",
        factory_block="## Problem\nRunaway loop.",
    )
    failed = FactoryRunOutcome(
        succeeded=False,
        exit_code=1,
        attempts_used=2,
        total_duration_seconds=1.0,
        failure_reason=(
            "Budget cap breached: cumulative tokens 100 exceeded "
            "max_tokens_per_run (50)"
        ),
        budget_breached=True,
        usage=UsageTotals(tokens=100, cost_usd=0.5, turns=2),
    )
    with (
        patch("acp_factory.runner.loop.fetch_issue", new=AsyncMock(return_value=task)),
        patch(
            "acp_factory.runner.loop.run_factory", new=AsyncMock(return_value=failed)
        ),
        patch(
            "acp_factory.runner.loop.update_issue_factory_block", new=AsyncMock()
        ) as mock_update,
        patch(
            "acp_factory.runner.loop.transition_issue_label", new=AsyncMock()
        ) as mock_transition,
    ):
        outcome = await run_issue_stage(tmp_path, 6, dry_run=False)

    assert outcome.succeeded is False
    assert outcome.budget_breached is True

    assert mock_update.await_args is not None
    updated_block = mock_update.await_args.args[1]
    assert "## Budget Escalation" in updated_block
    assert "**Cumulative Tokens:** 100" in updated_block
    meta = parse_factory_meta(updated_block)
    assert meta["stage"] == "needs-human"
    assert meta["tokens"] == "100"
    assert meta["cost"] == "0.5"
    assert meta["turns"] == "2"

    assert mock_transition.await_args is not None
    assert mock_transition.await_args.args[1] == LABEL_NEEDS_HUMAN


def test_multi_stage_budget_accumulation() -> None:
    """AC-7: cumulative totals persist and accumulate across stages via factory meta."""
    empty = "## Spec\n...\n"
    assert budget_usage_from_meta(empty) == UsageTotals(tokens=0, cost_usd=0.0, turns=0)

    block2 = set_factory_meta(
        empty, stage="build", tokens="120", cost="0.25", turns="2"
    )
    assert budget_usage_from_meta(block2) == UsageTotals(
        tokens=120, cost_usd=0.25, turns=2
    )

    block3 = set_factory_meta(
        block2, stage="needs-human", tokens="240", cost="0.5", turns="4"
    )
    assert budget_usage_from_meta(block3) == UsageTotals(
        tokens=240, cost_usd=0.5, turns=4
    )

    # Malformed meta values are rejected to zero rather than coerced into wrong data.
    bad = budget_usage_from_meta(
        "<!-- factory:meta tokens=abc cost=not-a-number turns=-3 -->"
    )
    assert bad == UsageTotals(tokens=0, cost_usd=0.0, turns=0)


async def test_run_json_records_usage(tmp_path: Path) -> None:
    """AC-8: run.json records cumulative usage and the active budget limits."""
    (tmp_path / ".factory").mkdir()
    cfg = FactoryConfig(
        task="do work",
        budget=BudgetConfig(max_tokens_per_run=100_000, max_cost_usd=1.0, max_turns=5),
    )
    fake, _ = _fake_client(
        {"used": 1250, "size": 20000, "cost": {"amount": 0.012, "currency": "USD"}}
    )
    with patch("acp_factory.runner.loop.FactoryACPClient", fake):
        outcome = await run_factory(cfg, tmp_path)

    assert outcome.succeeded is True
    assert outcome.run_dir is not None
    data = json.loads((outcome.run_dir / "run.json").read_text(encoding="utf-8"))
    assert data["usage"]["total_tokens"] == 1250
    assert data["usage"]["total_cost_usd"] == 0.012
    assert data["usage"]["total_turns"] == 1
    assert data["budget"]["max_tokens_per_run"] == 100_000
    assert data["budget"]["max_cost_usd"] == 1.0
    assert data["budget"]["max_turns"] == 5


async def test_multi_stage_seeds_initial_usage(tmp_path: Path) -> None:
    """run_issue_stage seeds cumulative usage from the issue's persisted factory meta."""
    task = TaskContext(
        issue_number=6,
        title="budget test",
        labels=[LABEL_BUILD],
        url="https://github.com/acme/repo/issues/6",
        factory_block="<!-- factory:meta stage=build tokens=100 cost=0.5 turns=3 -->\n## Spec\n...",
    )
    success = FactoryRunOutcome(
        succeeded=True,
        exit_code=0,
        attempts_used=1,
        total_duration_seconds=0.1,
        last_response_text="## Built\nSTATUS: OK",
        usage=UsageTotals(tokens=140, cost_usd=0.6, turns=4),
    )
    with (
        patch("acp_factory.runner.loop.fetch_issue", new=AsyncMock(return_value=task)),
        patch(
            "acp_factory.runner.loop.run_factory", new=AsyncMock(return_value=success)
        ) as mock_run_factory,
    ):
        outcome = await run_issue_stage(tmp_path, 6, dry_run=True)

    assert outcome.succeeded is True
    assert mock_run_factory.call_args.kwargs["initial_usage"] == UsageTotals(
        tokens=100, cost_usd=0.5, turns=3
    )
    # Success path persists the updated totals back into the verdict's factory block.
    assert outcome.stage_verdict is not None
    meta = parse_factory_meta(outcome.stage_verdict.updated_factory_block)
    assert meta["tokens"] == "140"
    assert meta["cost"] == "0.6"
    assert meta["turns"] == "4"


def test_cli_budget_flags_in_help() -> None:
    """AC-9: --max-tokens / --max-cost / --max-turns appear in the top-level help."""
    from typer.testing import CliRunner

    from acp_factory.cli import app

    runner = CliRunner()
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    for flag in ("--max-tokens", "--max-cost", "--max-turns"):
        assert flag in res.output


def test_cli_run_applies_budget_overrides() -> None:
    """The `run` command threads global budget flags into the resolved config."""
    from typer.testing import CliRunner

    from acp_factory.cli import app

    cfg = FactoryConfig(task="task")
    runner = CliRunner()
    with (
        patch("acp_factory.config.FactoryConfig.discover", return_value=cfg),
        patch(
            "acp_factory.cli.run_factory", new_callable=AsyncMock
        ) as mock_run_factory,
    ):
        mock_run_factory.return_value = FactoryRunOutcome(
            succeeded=True, exit_code=0, attempts_used=1, total_duration_seconds=0.0
        )
        res = runner.invoke(
            app,
            [
                "run",
                "--max-turns",
                "2",
                "--max-tokens",
                "100",
                "--max-cost",
                "0.5",
                "some task",
            ],
        )
        assert res.exit_code == 0
        called_cfg = mock_run_factory.call_args.args[0]
        assert called_cfg.budget.max_turns == 2
        assert called_cfg.budget.max_tokens_per_run == 100
        assert called_cfg.budget.max_cost_usd == 0.5


def test_cli_issue_and_next_expose_budget_options() -> None:
    """Findings 3: `issue` and `next` commands expose --max-tokens, --max-cost, and --max-turns."""
    from typer.testing import CliRunner

    from acp_factory.cli import app

    runner = CliRunner()
    for subcmd in ("issue", "next"):
        res = runner.invoke(app, [subcmd, "--help"])
        assert res.exit_code == 0
        for flag in ("--max-tokens", "--max-cost", "--max-turns"):
            assert flag in res.output


async def test_stage_with_prior_turns_meta_respects_per_run_turn_cap(
    tmp_path: Path,
) -> None:
    """Findings 1: a stage seeded with turns >= max_turns in factory meta still runs its turn."""
    task = TaskContext(
        issue_number=6,
        title="stage test",
        labels=["factory:triage"],
        url="https://github.com/acme/repo/issues/6",
        factory_block="<!-- factory:meta turns=3 tokens=50 cost=0.1 -->\n## Problem\nSome spec.",
    )
    fake, prompts = _fake_client()
    # Provide a minimal base config with checks=[] so attempt 1 succeeds immediately
    base_cfg = FactoryConfig(
        task="triage task",
        stages={
            "triage": StageConfig(checks=[]),
        },
    )
    with (
        patch("acp_factory.runner.loop.fetch_issue", new=AsyncMock(return_value=task)),
        patch("acp_factory.runner.loop.FactoryACPClient", fake),
        patch("acp_factory.runner.loop.update_issue_factory_block", new=AsyncMock()),
        patch("acp_factory.runner.loop.transition_issue_label", new=AsyncMock()),
    ):
        outcome = await run_issue_stage(
            tmp_path,
            6,
            base_config=base_cfg,
            dry_run=True,
            max_turns=3,
        )

    # The stage run succeeded its 1 attempt, dispatched 1 agent turn, and updated cumulative turns to 4.
    assert outcome.succeeded is True
    assert outcome.budget_breached is False
    assert len(prompts) == 1
    assert outcome.usage.turns == 4
    assert outcome.usage.tokens == 50
    assert outcome.usage.cost_usd == 0.1
