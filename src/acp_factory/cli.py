"""CLI entrypoint for acp-factory."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import FactoryConfig
from .github import list_actionable_issues, list_all_factory_issues
from .history import list_fleet_run_history, list_run_history
from .inspect import (
    compute_runs_stats,
    compute_transcript_stats,
    extract_tool_duration,
    extract_tool_exit_code,
    extract_tool_output,
    filter_tool_calls,
    load_transcript,
    resolve_transcript_path,
    search_text_lines,
)
from .runner.loop import run_branch_review, run_factory, run_issue_stage
from .runner.worktree import (
    GitSyncError,
    WorktreeManager,
    ensure_workspace_git_sync,
    resolve_main_repo_root,
)
from .task import TaskContext
from .ui.display import FactoryDisplay, console

app = typer.Typer(
    name="acp-factory",
    help="ACP-driven automated coding runner with multi-stage workflows, personas, APM skills, and self-healing retry loops (powered by acpterm).",
    add_completion=False,
    no_args_is_help=False,
)


@app.callback()
def _budget_callback(
    ctx: typer.Context,
    max_tokens: Annotated[
        int | None,
        typer.Option(
            "--max-tokens",
            min=1,
            help="Cumulative token cap across all turns in a run (overrides the budget configuration section).",
        ),
    ] = None,
    max_cost: Annotated[
        float | None,
        typer.Option(
            "--max-cost",
            min=0.0,
            help="Cumulative cost cap in USD across all turns in a run (overrides the budget configuration section).",
        ),
    ] = None,
    max_turns: Annotated[
        int | None,
        typer.Option(
            "--max-turns",
            min=1,
            help="Maximum number of agent turns allowed in a run (overrides the budget configuration section).",
        ),
    ] = None,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            "-q",
            help="Keep banners and show a spinner + truncated response instead of streaming ACP output.",
        ),
    ] = False,
) -> None:
    """Execution budget guardrails overriding the `[budget]` configuration section."""
    ctx.obj = {
        "max_tokens": max_tokens,
        "max_cost": max_cost,
        "max_turns": max_turns,
        "quiet": quiet,
    }


def _quiet_display(ctx: typer.Context) -> FactoryDisplay | None:
    """Return a quiet FactoryDisplay when `-q`/`--quiet` is set, else None."""
    if (ctx.obj or {}).get("quiet"):
        return FactoryDisplay(quiet=True)
    return None


def _resolve_use_fleet(
    fleet: bool | None,
    fleet_config_file: Path | None,
    base_cfg: FactoryConfig,
) -> bool:
    """Decide whether fleet mode is active for `next`, `status`, and `history`.

    An explicit `--fleet-config` enables fleet mode unless `--no-fleet` is passed;
    otherwise an explicit `--fleet`/`--no-fleet` wins, falling back to whether the
    resolved config already declares a `[fleet]` section.
    """
    if fleet_config_file is not None:
        return fleet is not False
    if fleet is not None:
        return fleet
    return base_cfg.fleet is not None


STARTER_CONFIG_YAML = """\
# factory.yaml — acp-factory configuration
version: "1"

agent:
  # Any agent alias configured in ~/.acpterm/config.json (e.g. "opencode", "kiro", "agy")
  # or an explicit command list: command: ["kiro-cli", "acp"]
  name: "opencode"
  timeout: 600

setup:
  - "uv sync"

task: "Describe the feature or bugfix to implement here"

checks:
  - name: "Lint & Format"
    cmd: "uv run ruff check ."
  - name: "Unit Tests"
    cmd: "uv run pytest"

retry:
  max_attempts: 3
  keep_worktree_on_failure: true

budget:
  max_tokens_per_run: 100000
  max_cost_usd: 2.00
  # max_turns: 10  # Optional per-run turn limit (omitted by default)
"""


MULTI_STAGE_CONFIG_TOML = """\
# .factory/config.toml — Multi-stage acp-factory configuration
version = "1"

[agent]
name = "opencode"
timeout = 600

[retry]
max_attempts = 3
keep_worktree_on_failure = true

[budget]
max_tokens_per_run = 100000
max_cost_usd = 2.00
# max_turns = 10  # Optional per-run turn limit (omitted by default)

[stages.triage]
agent = "opencode"
mode = "plan"
read_only = true
persona = "personas/triage.md"
skills = ["issue-triage"]
prompt = "prompts/TRIAGE.md"
checks = ["validate-triage"]

[stages.spec]
agent = "opencode"
mode = "plan"
read_only = true
persona = "personas/architect.md"
skills = ["spec-writer"]
prompt = "prompts/SPEC.md"
checks = ["validate-spec"]

[stages.build]
agent = "opencode"
read_only = false
pre = ["uv sync --frozen"]
persona = "personas/implementer.md"
skills = ["tdd-builder"]
prompt = "prompts/BUILD.md"
checks = ["lint", "test"]

[stages.review]
agent = "opencode"
read_only = true
persona = "personas/reviewer.md"
skills = ["code-reviewer", "security-auditor"]
prompt = "prompts/REVIEW.md"
checks = ["validate-review"]

[stages.retro]
agent = "opencode"
mode = "plan"
read_only = true
persona = "personas/architect.md"
skills = ["retro"]
prompt = "prompts/RETRO.md"
checks = []

[checks.validate-triage]
command = "uv run \\"$FACTORY_SKILLS_DIR/issue-triage/scripts/validate_triage.py\\" \\"$FACTORY_RESPONSE_FILE\\""

[checks.validate-spec]
command = "uv run \\"$FACTORY_SKILLS_DIR/spec-writer/scripts/validate_spec.py\\" \\"$FACTORY_RESPONSE_FILE\\""

[checks.validate-review]
command = "uv run \\"$FACTORY_SKILLS_DIR/code-reviewer/scripts/validate_review.py\\" \\"$FACTORY_RESPONSE_FILE\\""

[checks.validate-security]
command = "uv run \\"$FACTORY_SKILLS_DIR/security-auditor/scripts/validate_security.py\\" \\"$FACTORY_RESPONSE_FILE\\""

[checks.lint]
command = "git diff --name-only --diff-filter=ACMR main -- '*.py' | xargs -r uv run --frozen ruff check"

[checks.test]
command = "uv run --frozen pytest"

[checks.review]
persona = "personas/reviewer.md"
skills = ["code-reviewer"]
prompt = "prompts/REVIEW.md"

[checks.security]
persona = "personas/security.md"
skills = ["security-auditor"]
prompt = "prompts/SECURITY.md"
"""


STARTER_APM_YML = """\
name: factory-workspace
version: 0.1.0
description: APM configuration for acp-factory skills and MCP dependencies
dependencies:
  apm: []
  mcp: []
"""


DEFAULT_PERSONAS: dict[str, str] = {
    "triage.md": (
        "You are a senior engineering triage lead. You ground every incoming ticket in the actual "
        "repository code before anyone spends time specifying or building it.\n\n"
        "Cognitive Stance & Judgment:\n"
        "- Skeptical of ticket claims: treat bug reports and feature descriptions as hypotheses to "
        "verify against the source code and existing issues/PRs.\n"
        "- Protect reviewability: one ticket must equal one cohesive, reviewable pull request "
        "(roughly under ~400 changed lines). If a ticket spans independent subsystems, propose a "
        "build-order split (`STATUS: TOO_BIG`) rather than letting an oversized ticket proceed.\n"
        "- Privacy & precision: cite verified repository-relative `path:line` references only; "
        "never expose local host paths (`/home/...`, `/Users/...`) or conversational chatter."
    ),
    "architect.md": (
        "You are a pragmatic staff software architect. You design the smallest root-cause change "
        "that solves the problem cleanly and can be verified deterministically.\n\n"
        "Cognitive Stance & Judgment:\n"
        "- Root-cause minimalism: prefer extending existing modules and data structures over "
        "introducing new layers or abstractions.\n"
        "- Falsifiable contracts: every behavior you specify must translate into a deterministic "
        "local check (`AC-n`) and, for user-facing CLI/config changes, a concrete `## How to Test` "
        "guide and documentation check.\n"
        "- Cost-to-undo escalation filter: never block on humans for minor, reversible, code-local "
        "choices (record those under `## Assumptions`). Escalate in `## Open Questions` "
        "(`STATUS: NEEDS_HUMAN`) ONLY when nothing in the ticket, comments, or codebase settles a "
        "choice AND it would be costly to undo (unapproved dependencies, missing project-wide "
        "tooling, breaking public contracts, or cloud/IAM/cost footprint).\n"
        "- Privacy & precision: never expose local machine paths (`/home/...`, `/Users/...`) or "
        "include conversational preamble/postscript."
    ),
    "implementer.md": (
        "You are a disciplined TDD staff engineer. You value small, surgical commits backed by "
        "tests that fail when the production fix is reverted.\n\n"
        "Cognitive Stance & Judgment:\n"
        "- Red-Green-Refactor: turn each `AC-n` acceptance criterion into a failing test or "
        "deterministic check first, then write the minimal code to make it pass.\n"
        "- Defensive boundary handling: probe realistic bad and borderline inputs (empty strings, "
        "missing fields, `NaN`/`Inf`/`null`, out-of-range numbers, malformed paths) and reject "
        "them explicitly rather than coercing silently.\n"
        "- Strict scope discipline: stay within `## Files to Touch`, never add unapproved "
        "dependencies, commit locally on the issue branch without pushing, and never leak local "
        "host paths (`/home/...`, `/Users/...`)."
    ),
    "reviewer.md": (
        "You are a skeptical staff code and security reviewer. Nothing reaches a pull request "
        "until you are convinced the implementation is sound.\n\n"
        "Cognitive Stance & Judgment:\n"
        "- Adversarial verification: inspect the actual `git diff` and full file context as if an "
        "untrusted author wrote it. Treat commit messages and summaries as claims to verify, "
        "never as proof.\n"
        "- Test skepticism: verify that new tests exercise real behavior and would fail if the "
        "production change were reverted; flag tautological or mock-only tests as at least `[major]`.\n"
        "- High-signal grading: only `[blocking]` and `[major]` defects trigger `STATUS: REVISION_NEEDED`. "
        "Drop subjective style opinions not backed by `AGENTS.md` or the specification, and never "
        "expose local host paths (`/home/...`, `/Users/...`)."
    ),
    "security.md": (
        "You are an offensive security auditor. You evaluate changes through a single lens: what "
        "an attacker or untrusted caller can do with them.\n\n"
        "Cognitive Stance & Judgment:\n"
        "- Source-to-sink tracing: trace every untrusted input (CLI args, configs, env vars, "
        "files, network payloads) to its execution, filesystem, serialization, or query sink.\n"
        "- Exploit-proven findings: never report theoretical noise—prove every `[blocking]` or "
        "`[major]` vulnerability with a concrete `Exploit / Trigger` input.\n"
        "- Read-only & private: never modify files, commit, or push, and never expose local "
        "machine paths (`/home/...`, `/Users/...`)."
    ),
}


DEFAULT_STAGE_PROMPTS: dict[str, str] = {
    "TRIAGE.md": (
        "Execute the `issue-triage` skill against the supplied ticket and codebase:\n"
        "1. Ground all claims in the repository code, check open/recent issues and PRs for duplicates "
        "(`DUPLICATE` vs `ALSO_FIXES`), verify single-PR sizing (<400 changed lines), and note "
        "available verification tooling.\n"
        "2. Output ONLY the completed triage report matching `skills/issue-triage/templates/triage-report.md` "
        "(starting with `### Codebase Inspection` and ending with `STATUS: <OK | DUPLICATE | TOO_BIG | NEEDS_HUMAN>`)."
    ),
    "SPEC.md": (
        "Execute the `spec-writer` skill for the supplied ticket, triage notes, and human comments:\n"
        "1. Apply the two-tier ambiguity filter (`## Assumptions` for reversible code-local choices vs. "
        "`## Open Questions` + `STATUS: NEEDS_HUMAN` for costly-to-undo or unapproved dependency/contract/tooling choices).\n"
        "2. If an existing specification is present in the factory block, operate in Incremental Revision Mode "
        "(preserve stable `AC-n` IDs, fold human answers into `## Decisions`, and append new criteria).\n"
        "3. Include a documentation `AC-n` row and a concrete `## How to Test` guide whenever user-facing CLI "
        "commands, flags, or configuration options change.\n"
        "4. Output ONLY the complete specification matching `skills/spec-writer/templates/spec.md` "
        "(starting with `# Specification` and ending with `STATUS: <SPEC_READY | NEEDS_HUMAN>`)."
    ),
    "BUILD.md": (
        "Execute the `tdd-builder` skill to implement the approved specification:\n"
        "1. Read `AGENTS.md` / `CLAUDE.md` and turn each `AC-n` row from `## Acceptance Criteria` into a "
        "failing test or deterministic check first.\n"
        "2. Implement the minimal root-cause change strictly within `## Files to Touch`, probing bad and "
        "borderline inputs so invalid data is explicitly rejected.\n"
        "3. If `## Latest PR Review Findings` or `## PR Review Feedback` / `## Human Review Comments` are "
        "present, fixing every actionable finding (including `[blocking]`, `[major]`, and `[minor]` items) "
        "is part of your job — add regression tests where applicable and fix them in code.\n"
        "4. Self-review `git diff` against the 7-point quality standard and commit your changes locally "
        "on the current branch without pushing."
    ),
    "REVIEW.md": (
        "Execute the `code-reviewer` and `security-auditor` skills against the workspace diff (`git diff main...HEAD`). "
        "Do not modify any files:\n"
        "1. Classify the change risk (`LOW | MEDIUM | HIGH`), verify every `AC-n` row and test effectiveness, "
        "check user-facing `## How to Test` and docs coverage, and audit untrusted inputs and edge cases.\n"
        "2. Output ONLY the structured review report matching `skills/code-reviewer/templates/review-report.md` "
        "(starting with `### Risk Classification:` and ending with `STATUS: <ACCEPTED | REVISION_NEEDED | NEEDS_HUMAN>`)."
    ),
    "SECURITY.md": (
        "Execute the `security-auditor` skill against the workspace diff (`git diff main...HEAD`). Do not modify any files:\n"
        "1. Trace every external or untrusted input from source to sink across Injection, Access Control, "
        "Secrets/Host-Path Exposure, Unsafe Defaults, and Dependencies, proving each finding with a concrete `Exploit / Trigger`.\n"
        "2. Output ONLY the structured security report matching `skills/security-auditor/templates/security-report.md` "
        "(starting with `### Security Audit Summary` and ending with `STATUS: <ACCEPTED | REVISION_NEEDED>`)."
    ),
    "RETRO.md": (
        "Execute the `retro` skill against this repository's recent `.factory/runs/` artifacts (`run.json`, "
        "`attempt-*/check-*.log`, `attempt-*/transcript.summary.json` / `transcript.json`), `AGENTS.md`, and `.factory/config.toml`. Do not modify any files:\n"
        "1. Inspect recent run directories under `.factory/runs/` (sorted by newest first: read `run.json`, `attempt-*/check-*.log`, and `attempt-*/transcript.summary.json` for runs with `attempts_used > 1`, failed checks, or high tool/turn counts). Tip: Use `jq` to quickly parse tool frequencies, failed actions, or denied guardrail permissions without overloading context (e.g. `jq '.permissions[] | select(.status == \"denied\")' attempt-*/transcript.summary.json`). Fall back to `attempt-*-transcript.md` only when deep turn-level debugging context is strictly required.\n"
        "2. Evaluate across the 7 retrospective categories (Navigation, Automated Checks, Coding Standards & Reviewer Rubric, "
        "`AGENTS.md` Diet, Tool Economy, No-ops, and Information Access — including any denied permissions requiring `[guardrails.allowed_paths]` adjustments in `.factory/config.toml`).\n"
        "3. Output a ranked Markdown retrospective report (`HIGH`, `MEDIUM`, `LOW`) with concrete evidence citations and unified `diff` proposals."
    ),
}


@app.command("run")
def run_cmd(
    ctx: typer.Context,
    task_arg: Annotated[
        str | None,
        typer.Argument(
            help="Optional task description (overrides 'task' in configuration)."
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Path to .factory/config.toml or factory.yaml configuration file.",
        ),
    ] = None,
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Target workspace directory.",
        ),
    ] = Path("."),
    stage: Annotated[
        str | None,
        typer.Option(
            "--stage",
            "-s",
            help="Named stage to execute (e.g. 'triage', 'spec', 'build', 'review').",
        ),
    ] = None,
    task_opt: Annotated[
        str | None,
        typer.Option(
            "--task",
            "-t",
            help="Task prompt override.",
        ),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            "-a",
            help="acpterm agent name (e.g. 'opencode', 'kiro', 'agy').",
        ),
    ] = None,
    agent_cmd: Annotated[
        str | None,
        typer.Option(
            "--agent-cmd",
            help="Explicit ACP agent command string (e.g. 'kiro-cli acp' or 'copilot acp').",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            "-m",
            help="Override the active LLM model for the ACP session.",
        ),
    ] = None,
    mode: Annotated[
        str | None,
        typer.Option(
            "--mode",
            help="Override the active agent mode for the ACP session.",
        ),
    ] = None,
    read_only: Annotated[
        bool | None,
        typer.Option(
            "--read-only/--no-read-only",
            help="Run the agent in read-only mode (disables file writes).",
        ),
    ] = None,
    persona: Annotated[
        str | None,
        typer.Option(
            "--persona",
            help="Path to a persona Markdown file (or inline role instructions).",
        ),
    ] = None,
    skill: Annotated[
        list[str] | None,
        typer.Option(
            "--skill",
            help="Skill path (SKILL.md) or APM skill package name to attach (repeatable).",
        ),
    ] = None,
    setup: Annotated[
        list[str] | None,
        typer.Option(
            "--setup",
            help="Pre-execution setup shell command(s) (repeatable, e.g. --setup 'uv sync').",
        ),
    ] = None,
    check: Annotated[
        list[str] | None,
        typer.Option(
            "--check",
            "-k",
            help="Validation check name(s) or shell command(s) to run sequentially (repeatable).",
        ),
    ] = None,
    review: Annotated[
        list[str] | None,
        typer.Option(
            "--review",
            help="Agent review prompt check(s) (repeatable).",
        ),
    ] = None,
    max_attempts: Annotated[
        int | None,
        typer.Option(
            "--max-attempts",
            "--attempts",
            "-n",
            help="Maximum worker retry attempts.",
        ),
    ] = None,
    timeout: Annotated[
        int | None,
        typer.Option(
            "--timeout",
            help="Per-turn agent timeout in seconds.",
        ),
    ] = None,
    worktree: Annotated[
        bool | None,
        typer.Option(
            "--worktree/--no-worktree",
            help="Run inside an isolated temporary git worktree.",
        ),
    ] = None,
    keep_worktree: Annotated[
        bool | None,
        typer.Option(
            "--keep-worktree",
            help="Always preserve the temporary git worktree after completion.",
        ),
    ] = None,
    export: Annotated[
        Path | None,
        typer.Option(
            "--export",
            help="Export Markdown session transcripts to the specified file path.",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Log raw ACP JSON-RPC messages.",
        ),
    ] = False,
    dry_run: Annotated[
        bool | None,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Disable automatic GitHub writes; save output files and print manual gh commands.",
        ),
    ] = None,
    max_tokens: Annotated[
        int | None,
        typer.Option(
            "--max-tokens",
            min=1,
            help="Cumulative token cap across all turns in a run (overrides the budget configuration section).",
        ),
    ] = None,
    max_cost: Annotated[
        float | None,
        typer.Option(
            "--max-cost",
            min=0.0,
            help="Cumulative cost cap in USD across all turns in a run (overrides the budget configuration section).",
        ),
    ] = None,
    max_turns: Annotated[
        int | None,
        typer.Option(
            "--max-turns",
            min=1,
            help="Maximum number of agent turns allowed in a run (overrides the budget configuration section).",
        ),
    ] = None,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help="Skip pre-flight git fetch and upstream branch sync.",
        ),
    ] = False,
) -> None:
    """Execute the [Setup -> Worker Agent -> Checks -> Feedback Loop] workflow."""
    resolved_workspace = workspace.resolve()
    if not offline:
        try:
            asyncio.run(ensure_workspace_git_sync(resolved_workspace, offline=offline))
        except GitSyncError as exc:
            console.print(f"[bold red]Git Sync Error:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc

    try:
        cfg = FactoryConfig.discover(resolved_workspace, config_file)
        effective_task = task_opt or task_arg
        budget = ctx.obj or {}
        eff_max_tokens = (
            max_tokens if max_tokens is not None else budget.get("max_tokens")
        )
        eff_max_cost = max_cost if max_cost is not None else budget.get("max_cost")
        eff_max_turns = max_turns if max_turns is not None else budget.get("max_turns")
        cfg = cfg.with_overrides(
            stage=stage,
            workspace=resolved_workspace,
            task=effective_task,
            agent_name=agent,
            agent_command=agent_cmd,
            model=model,
            mode=mode,
            read_only=read_only,
            persona=persona,
            skills=skill,
            timeout=timeout,
            checks=check,
            review_prompts=review,
            setup_cmds=setup,
            max_attempts=max_attempts,
            max_tokens=eff_max_tokens,
            max_cost=eff_max_cost,
            max_turns=eff_max_turns,
            worktree=worktree,
            keep_worktree=keep_worktree,
            dry_run=dry_run,
        )
    except Exception as exc:
        console.print(f"[bold red]Configuration Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    if not cfg.task.strip():
        console.print(
            "[bold red]Error:[/bold red] No task specified. Provide a task in configuration, "
            "select a `--stage` with a prompt file, or pass a task via CLI "
            '(e.g. `acp-factory run "Implement feature X" --stage build`).'
        )
        raise typer.Exit(code=1)

    try:
        outcome = asyncio.run(
            run_factory(
                cfg,
                resolved_workspace,
                display=_quiet_display(ctx),
                verbose=verbose,
                export_transcript=export,
            )
        )
    except KeyboardInterrupt as exc:
        console.print(
            "\n[yellow]Interrupted by user (SIGINT). Child processes cleaned up.[/yellow]"
        )
        raise typer.Exit(code=130) from exc

    raise typer.Exit(code=outcome.exit_code)


@app.command("issue")
def issue_cmd(
    ctx: typer.Context,
    issue_number: Annotated[
        int,
        typer.Argument(help="GitHub Issue number to process (e.g. 42)."),
    ],
    stage: Annotated[
        str | None,
        typer.Option(
            "--stage",
            "-s",
            help="Explicit stage override (defaults to the issue's current `factory:*` label).",
        ),
    ] = None,
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Target workspace directory."),
    ] = Path("."),
    config_file: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to configuration file."),
    ] = None,
    dry_run: Annotated[
        bool | None,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Run the ACP stage live, save output files locally, and print manual gh commands without mutating GitHub.",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Log raw ACP JSON-RPC messages."),
    ] = False,
    max_tokens: Annotated[
        int | None,
        typer.Option(
            "--max-tokens",
            min=1,
            help="Cumulative token cap across all turns in a run (overrides the budget configuration section).",
        ),
    ] = None,
    max_cost: Annotated[
        float | None,
        typer.Option(
            "--max-cost",
            min=0.0,
            help="Cumulative cost cap in USD across all turns in a run (overrides the budget configuration section).",
        ),
    ] = None,
    max_turns: Annotated[
        int | None,
        typer.Option(
            "--max-turns",
            min=1,
            help="Maximum number of agent turns allowed in a run (overrides the budget configuration section).",
        ),
    ] = None,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help="Skip pre-flight git fetch and upstream branch sync.",
        ),
    ] = False,
) -> None:
    """Inspect a GitHub Issue's `factory:*` label, run its stage, update its body in-place, and transition its label."""
    try:
        budget = ctx.obj or {}
        eff_max_tokens = (
            max_tokens if max_tokens is not None else budget.get("max_tokens")
        )
        eff_max_cost = max_cost if max_cost is not None else budget.get("max_cost")
        eff_max_turns = max_turns if max_turns is not None else budget.get("max_turns")
        outcome = asyncio.run(
            run_issue_stage(
                workspace.resolve(),
                issue_number,
                stage_override=stage,
                config_file=config_file,
                dry_run=dry_run,
                display=_quiet_display(ctx),
                verbose=verbose,
                max_tokens=eff_max_tokens,
                max_cost=eff_max_cost,
                max_turns=eff_max_turns,
                offline=offline,
            )
        )
    except GitSyncError as exc:
        console.print(f"[bold red]Git Sync Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[bold red]Issue Stage Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    if outcome.stage_verdict:
        v = outcome.stage_verdict
        console.print(
            f"[bold green]✓ Issue #{issue_number}[/bold green] stage [cyan]{v.stage}[/cyan] "
            f"-> label [bold yellow]{v.target_label}[/bold yellow] ({v.summary})"
        )
    elif outcome.failure_reason:
        console.print(
            f"[bold red]Issue #{issue_number} stage failed:[/bold red] {outcome.failure_reason}"
        )
    if outcome.report_path:
        console.print(f"[bold cyan]Review Report:[/bold cyan] {outcome.report_path}")
    if outcome.build_notes_path:
        console.print(f"[bold cyan]Build Notes:[/bold cyan] {outcome.build_notes_path}")
    raise typer.Exit(code=outcome.exit_code)


@app.command("review")
def review_cmd(
    ctx: typer.Context,
    description: Annotated[
        str | None,
        typer.Argument(
            help="Optional description of the change (checked as a claim against the diff)."
        ),
    ] = None,
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Target workspace directory."),
    ] = Path("."),
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            "-b",
            help="Branch to review (defaults to currently checked-out branch in workspace).",
        ),
    ] = None,
    base: Annotated[
        str,
        typer.Option(
            "--base",
            help="Base branch to compare against.",
        ),
    ] = "main",
    create_pr: Annotated[
        bool,
        typer.Option(
            "--create-pr/--no-create-pr",
            help="Push branch and open/update a standalone GitHub Pull Request if review passes.",
        ),
    ] = False,
    title: Annotated[
        str | None,
        typer.Option(
            "--title",
            help="Explicit Pull Request title when used with --create-pr.",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to configuration file."),
    ] = None,
    dry_run: Annotated[
        bool | None,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Run the review live, save the PR/review body locally, and print manual git/gh commands.",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Log raw ACP JSON-RPC messages."),
    ] = False,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help="Skip pre-flight git fetch and upstream branch sync.",
        ),
    ] = False,
) -> None:
    """Review a working branch without a GitHub Issue and optionally open a standalone Pull Request."""
    try:
        outcome = asyncio.run(
            run_branch_review(
                workspace.resolve(),
                description=description,
                branch=branch,
                base=base,
                create_pr=create_pr,
                pr_title=title,
                config_file=config_file,
                dry_run=dry_run,
                display=_quiet_display(ctx),
                verbose=verbose,
                offline=offline,
            )
        )
    except GitSyncError as exc:
        console.print(f"[bold red]Git Sync Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[bold red]Branch Review Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    if outcome.stage_verdict:
        v = outcome.stage_verdict
        color = "green" if v.status == "accepted" else "yellow"
        console.print(
            f"[bold {color}]Review Verdict:[/bold {color}] [cyan]{v.status}[/cyan] ({v.summary})"
        )
    elif outcome.failure_reason:
        console.print(f"[bold red]Review failed:[/bold red] {outcome.failure_reason}")
    if outcome.report_path:
        console.print(f"[bold cyan]Review Report:[/bold cyan] {outcome.report_path}")
    raise typer.Exit(code=outcome.exit_code)


@app.command("next")
def next_cmd(
    ctx: typer.Context,
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Target workspace directory."),
    ] = Path("."),
    config_file: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to configuration file."),
    ] = None,
    fleet: Annotated[
        bool | None,
        typer.Option(
            "--fleet/--no-fleet",
            help="Run one fleet pass across all 'fleet.repositories' entries (auto-enabled when a fleet section is configured).",
        ),
    ] = None,
    fleet_config_file: Annotated[
        Path | None,
        typer.Option(
            "--fleet-config",
            help="Path to an operator-level fleet TOML file (overrides repo-local and user-level fleet definitions).",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            "-n",
            min=1,
            help="Maximum number of actionable issues to process across the run (defaults to 1 without --all, or all with --all).",
        ),
    ] = None,
    all_issues: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Process all currently actionable issues sequentially instead of just the first one.",
        ),
    ] = False,
    dry_run: Annotated[
        bool | None,
        typer.Option(
            "--dry-run/--no-dry-run",
            help="Run the ACP stage live, save output files locally, and print manual gh commands without mutating GitHub.",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Log raw ACP JSON-RPC messages."),
    ] = False,
    max_tokens: Annotated[
        int | None,
        typer.Option(
            "--max-tokens",
            min=1,
            help="Cumulative token cap across all turns in a run (overrides the budget configuration section).",
        ),
    ] = None,
    max_cost: Annotated[
        float | None,
        typer.Option(
            "--max-cost",
            min=0.0,
            help="Cumulative cost cap in USD across all turns in a run (overrides the budget configuration section).",
        ),
    ] = None,
    max_turns: Annotated[
        int | None,
        typer.Option(
            "--max-turns",
            min=1,
            help="Maximum number of agent turns allowed in a run (overrides the budget configuration section).",
        ),
    ] = None,
    offline: Annotated[
        bool,
        typer.Option(
            "--offline",
            help="Skip pre-flight git fetch and upstream branch sync.",
        ),
    ] = False,
) -> None:
    """Find open GitHub Issues with actionable `factory:*` labels and run their current stage."""
    resolved_workspace = workspace.resolve()

    async def _run_next() -> int:
        budget = ctx.obj or {}
        eff_max_tokens = (
            max_tokens if max_tokens is not None else budget.get("max_tokens")
        )
        eff_max_cost = max_cost if max_cost is not None else budget.get("max_cost")
        eff_max_turns = max_turns if max_turns is not None else budget.get("max_turns")
        base_cfg = FactoryConfig.discover(resolved_workspace, config_file)
        use_fleet = _resolve_use_fleet(fleet, fleet_config_file, base_cfg)

        if use_fleet:
            from .runner.loop import run_fleet_next

            base_cfg = base_cfg.with_fleet_overlay(fleet_config_file).with_overrides(
                max_tokens=eff_max_tokens,
                max_cost=eff_max_cost,
                max_turns=eff_max_turns,
            )
            effective_limit = (
                limit if limit is not None else (None if all_issues else 1)
            )
            results = await run_fleet_next(
                base_cfg,
                limit=effective_limit,
                dry_run=dry_run,
                verbose=verbose,
                display=_quiet_display(ctx),
            )
            if not results:
                console.print(
                    "[dim]No fleet repositories configured ([fleet.repositories] is empty).[/dim]"
                )
                return 0
            exit_code = 0
            for res in results:
                if res.error:
                    console.print(
                        f"[bold red]✗ {res.repo_slug}[/bold red]: {res.error}"
                    )
                    exit_code = 1
                else:
                    console.print(
                        f"[bold green]✓ {res.repo_slug}[/bold green]: "
                        f"{res.issues_processed} issue(s) advanced"
                    )
            return exit_code

        tasks = await list_actionable_issues(resolved_workspace)
        if not tasks:
            console.print(
                "[dim]No open GitHub issues with actionable `factory:*` labels "
                "(triage, spec, build, review, done) found.[/dim]"
            )
            return 0

        if limit is not None:
            targets = tasks[:limit]
        elif all_issues:
            targets = tasks
        else:
            targets = tasks[:1]

        exit_code = 0
        for t in targets:
            console.print(
                f"\n[bold cyan]→ Processing Issue #{t.issue_number}:[/bold cyan] "
                f"{t.title} [dim](stage: {t.current_stage})[/dim]"
            )
            outcome = await run_issue_stage(
                resolved_workspace,
                t.issue_number,
                config_file=config_file,
                dry_run=dry_run,
                display=_quiet_display(ctx),
                verbose=verbose,
                max_tokens=eff_max_tokens,
                max_cost=eff_max_cost,
                max_turns=eff_max_turns,
                offline=offline,
            )
            if outcome.stage_verdict:
                v = outcome.stage_verdict
                console.print(
                    f"[bold green]✓ Issue #{t.issue_number}[/bold green] "
                    f"-> [bold yellow]{v.target_label}[/bold yellow] ({v.summary})"
                )
            if outcome.report_path:
                console.print(
                    f"[bold cyan]Review Report:[/bold cyan] {outcome.report_path}"
                )
            if outcome.build_notes_path:
                console.print(
                    f"[bold cyan]Build Notes:[/bold cyan] {outcome.build_notes_path}"
                )
            if outcome.exit_code != 0:
                exit_code = outcome.exit_code
                if not all_issues and limit is None:
                    break
        return exit_code

    try:
        rc = asyncio.run(_run_next())
    except GitSyncError as exc:
        console.print(f"[bold red]Git Sync Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    raise typer.Exit(code=rc)


def _state_description(t: TaskContext) -> str:
    """Render a human-gate/stage description for a task's `factory:*` labels."""
    fac_labels = [lbl for lbl in t.labels if lbl.startswith("factory:")]
    if t.current_stage:
        return f"[green]Actionable ({t.current_stage})[/green]"
    if "factory:spec-ready" in fac_labels:
        return "[bold magenta]🛑 Human Gate 1 (Spec Approval)[/bold magenta]"
    if "factory:pr-ready" in fac_labels:
        return "[bold magenta]🛑 Human Gate 2 (PR Review & Merge)[/bold magenta]"
    if "factory:needs-human" in fac_labels:
        return "[bold red]⚠️ Needs Human Input[/bold red]"
    if "factory:completed" in fac_labels:
        return "[dim]Completed[/dim]"
    return "[dim]Idle[/dim]"


@app.command("status")
def status_cmd(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Target workspace directory."),
    ] = Path("."),
    config_file: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to configuration file."),
    ] = None,
    fleet: Annotated[
        bool | None,
        typer.Option(
            "--fleet/--no-fleet",
            help="Show a per-repository fleet status table (auto-enabled when a fleet section is configured).",
        ),
    ] = None,
    fleet_config_file: Annotated[
        Path | None,
        typer.Option(
            "--fleet-config",
            help="Path to an operator-level fleet TOML file (overrides repo-local and user-level fleet definitions).",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            "-n",
            min=1,
            help="Maximum number of issues to display per repository (defaults to 50).",
        ),
    ] = None,
) -> None:
    """Display open `factory:*` GitHub Issues, their current stage/human gate, and local `.worktrees/`."""
    resolved_workspace = workspace.resolve()

    async def _show_status() -> None:
        base_cfg = FactoryConfig.discover(resolved_workspace, config_file)
        use_fleet = _resolve_use_fleet(fleet, fleet_config_file, base_cfg)

        if use_fleet:
            from .runner.loop import collect_fleet_status

            base_cfg = base_cfg.with_fleet_overlay(fleet_config_file)
            table = Table(title="acp-factory Fleet Status")
            table.add_column("Repository", style="bold cyan")
            table.add_column("Issue", justify="right", style="cyan")
            table.add_column("Title")
            table.add_column("Factory Label(s)", style="yellow")
            table.add_column("State / Gate")

            repo_cols = await collect_fleet_status(base_cfg, limit=limit or 50)
            any_rows = False
            for repo_slug, tasks, error in repo_cols:
                if error:
                    table.add_row(
                        repo_slug, "-", "-", "-", f"[bold red]Error: {error}[/bold red]"
                    )
                    any_rows = True
                    continue
                if not tasks:
                    table.add_row(repo_slug, "-", "-", "-", "[dim]Idle[/dim]")
                    any_rows = True
                    continue
                for t in tasks:
                    fac_labels = [lbl for lbl in t.labels if lbl.startswith("factory:")]
                    state_desc = _state_description(t)
                    table.add_row(
                        repo_slug,
                        f"#{t.issue_number}",
                        t.title,
                        ", ".join(fac_labels),
                        state_desc,
                    )
                    any_rows = True
            console.print(table if any_rows else "[dim]No fleet issues found.[/dim]")
            return

        tasks = await list_all_factory_issues(resolved_workspace, limit=limit or 50)
        main_root = resolve_main_repo_root(resolved_workspace)
        table = Table(title="acp-factory GitHub Issues & Worktrees")
        table.add_column("Issue", style="bold cyan", justify="right")
        table.add_column("Title")
        table.add_column("Factory Label(s)", style="yellow")
        table.add_column("State / Gate")
        table.add_column("Local Worktree")

        for t in tasks:
            fac_labels = [lbl for lbl in t.labels if lbl.startswith("factory:")]
            state_desc = _state_description(t)

            wt_path = WorktreeManager.issue_worktree_path(main_root, t.issue_number)
            wt_status = str(wt_path.relative_to(main_root)) if wt_path.exists() else "-"
            table.add_row(
                f"#{t.issue_number}",
                t.title,
                ", ".join(fac_labels),
                state_desc,
                wt_status,
            )

        if not tasks:
            console.print(
                "[dim]No open GitHub issues with `factory:*` labels found.[/dim]"
            )
        else:
            console.print(table)

    try:
        asyncio.run(_show_status())
    except Exception as exc:
        console.print(f"[bold red]Status Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("init")
def init_cmd(
    path: Annotated[
        Path,
        typer.Option(
            "--path",
            "-p",
            help="Target directory or file path for initialization.",
        ),
    ] = Path("."),
    multi_stage: Annotated[
        bool,
        typer.Option(
            "--multi-stage",
            help="Scaffold .factory/config.toml with stage personas, prompts, and apm.yml.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Overwrite existing configuration files if present.",
        ),
    ] = False,
) -> None:
    """Create starter acp-factory configuration (single factory.yaml or .factory/ multi-stage layout)."""
    if multi_stage:
        base_dir = (
            path.resolve()
            if path.is_dir() or not path.suffix
            else path.resolve().parent
        )
        factory_dir = base_dir / ".factory"
        config_toml = factory_dir / "config.toml"
        if config_toml.exists() and not force:
            console.print(
                f"[yellow]File already exists:[/yellow] {config_toml} (use --force to overwrite)"
            )
            raise typer.Exit(code=1)

        (factory_dir / "personas").mkdir(parents=True, exist_ok=True)
        (factory_dir / "prompts").mkdir(parents=True, exist_ok=True)
        (factory_dir / "skills").mkdir(parents=True, exist_ok=True)

        config_toml.write_text(MULTI_STAGE_CONFIG_TOML, encoding="utf-8")
        console.print(f"[bold green]✓ Created[/bold green] {config_toml}")

        for name, content in DEFAULT_PERSONAS.items():
            p_file = factory_dir / "personas" / name
            if not p_file.exists() or force:
                p_file.write_text(content + "\n", encoding="utf-8")
                console.print(f"[bold green]✓ Created[/bold green] {p_file}")

        for name, content in DEFAULT_STAGE_PROMPTS.items():
            pr_file = factory_dir / "prompts" / name
            if not pr_file.exists() or force:
                pr_file.write_text(content + "\n", encoding="utf-8")
                console.print(f"[bold green]✓ Created[/bold green] {pr_file}")

        apm_file = base_dir / "apm.yml"
        if not apm_file.exists() or force:
            apm_file.write_text(STARTER_APM_YML, encoding="utf-8")
            console.print(f"[bold green]✓ Created[/bold green] {apm_file}")
        return

    target = (path / "factory.yaml").resolve() if path.is_dir() else path.resolve()
    if target.exists() and not force:
        console.print(
            f"[yellow]File already exists:[/yellow] {target} (use --force to overwrite)"
        )
        raise typer.Exit(code=1)

    target.write_text(STARTER_CONFIG_YAML, encoding="utf-8")
    console.print(f"[bold green]✓ Created[/bold green] {target}")


@app.command("history")
def history_cmd(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Target workspace directory."),
    ] = Path("."),
    config_file: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to configuration file."),
    ] = None,
    fleet: Annotated[
        bool | None,
        typer.Option(
            "--fleet/--no-fleet",
            help="Aggregate run history from all 'fleet.repositories' entries (auto-enabled when a fleet section is configured).",
        ),
    ] = None,
    fleet_config_file: Annotated[
        Path | None,
        typer.Option(
            "--fleet-config",
            help="Path to an operator-level fleet TOML file (overrides repo-local and user-level fleet definitions).",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            "-n",
            min=1,
            help="Maximum number of runs to display after merging fleet results (defaults to 50).",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Print run metadata as JSON instead of a table.",
        ),
    ] = False,
) -> None:
    """Show a chronological history of local factory runs (`.factory/runs/*/run.json`).

    Use `--fleet` to merge runs from every repository in the fleet configuration into
    a single timeline, newest first.
    """
    resolved_workspace = workspace.resolve()
    base_cfg = FactoryConfig.discover(resolved_workspace, config_file)
    use_fleet = _resolve_use_fleet(fleet, fleet_config_file, base_cfg)

    if use_fleet:
        records = list_fleet_run_history(base_cfg.with_fleet_overlay(fleet_config_file))
    else:
        records = list_run_history(resolved_workspace)

    limited = records[: limit or 50]
    if json_output:
        console.print(json.dumps(limited, indent=2))
        return

    if not records:
        if use_fleet:
            console.print("[dim]No factory runs found across fleet repositories.[/dim]")
        else:
            console.print("[dim]No factory runs found under `.factory/runs/`.[/dim]")
        return

    table = Table(title="acp-factory Run History")
    table.add_column("Run ID", style="dim")
    table.add_column("Stage", style="cyan", no_wrap=True)
    table.add_column("Model")
    table.add_column("Issue", justify="right", style="bold cyan", no_wrap=True)
    table.add_column("Repo")
    table.add_column("Branch", style="yellow")
    table.add_column("Attempts", justify="right", no_wrap=True)
    table.add_column("Dur", justify="right", no_wrap=True)
    table.add_column("Turns", justify="right", no_wrap=True)
    table.add_column("Tokens", justify="right", no_wrap=True)
    table.add_column("Cost", justify="right", no_wrap=True)
    table.add_column("Result", no_wrap=True)

    for rec in limited:
        issue = rec.get("issue_number")
        dur = rec.get("duration_seconds")
        usage = rec.get("usage") or {}
        telemetry = usage.get("telemetry_available") is True
        turns = usage.get("total_turns")
        tokens = usage.get("total_tokens")
        cost = usage.get("total_cost_usd")
        result = rec.get("succeeded")
        if result is True:
            result_cell = "[green]✓[/green]"
        elif result is False:
            result_cell = "[red]✗[/red]"
        else:
            result_cell = "-"
        turns_cell = str(turns) if isinstance(turns, int) else "-"
        tokens_cell = (
            f"{int(tokens):,}"
            if telemetry and isinstance(tokens, (int, float))
            else "-"
        )
        cost_cell = (
            f"${cost:.4f}" if telemetry and isinstance(cost, (int, float)) else "-"
        )
        model_cell = str(rec.get("model") or "-")
        attempts_used = rec.get("attempts_used")
        max_attempts = rec.get("max_attempts")
        attempts_used_is_int = isinstance(attempts_used, int) and not isinstance(
            attempts_used, bool
        )
        max_attempts_is_int = isinstance(max_attempts, int) and not isinstance(
            max_attempts, bool
        )
        if attempts_used_is_int and max_attempts_is_int:
            attempts_cell = f"{attempts_used}/{max_attempts}"
        elif attempts_used_is_int:
            attempts_cell = str(attempts_used)
        else:
            attempts_cell = "-"
        table.add_row(
            str(rec.get("run_id") or "-"),
            str(rec.get("stage") or "-"),
            model_cell,
            f"#{issue}" if issue is not None else "-",
            str(rec.get("repo_slug") or "-"),
            str(rec.get("branch") or "-"),
            attempts_cell,
            f"{dur:.0f}s" if isinstance(dur, (int, float)) else "-",
            turns_cell,
            tokens_cell,
            cost_cell,
            result_cell,
        )
    console.print(table)


def _build_tool_calls_table(
    title: str, rows: list[tuple[int, dict[str, Any]]]
) -> Table:
    """Build a Rich summary table for matched tool calls."""
    table = Table(title=title)
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Kind", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Exit", justify="right", no_wrap=True)
    table.add_column("Dur", justify="right", no_wrap=True)
    table.add_column("Title")
    for idx, tc in rows:
        ec = extract_tool_exit_code(tc)
        dur = extract_tool_duration(tc)
        table.add_row(
            str(idx),
            str(tc.get("kind") or "-"),
            str(tc.get("status") or "-"),
            str(ec) if ec is not None else "-",
            f"{dur:.2f}s" if dur is not None else "-",
            str(tc.get("title") or "-"),
        )
    return table


@app.command("inspect")
def inspect_cmd(
    target: Annotated[
        str | None,
        typer.Argument(
            help="Run ID (or prefix), run/attempt directory, or transcript JSON file. Defaults to the latest run.",
        ),
    ] = None,
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Target workspace directory."),
    ] = Path("."),
    attempt: Annotated[
        int,
        typer.Option("--attempt", "-a", min=1, help="Attempt number to inspect."),
    ] = 1,
    thoughts: Annotated[
        bool,
        typer.Option("--thoughts", "-t", help="Show agent thought text."),
    ] = False,
    calls: Annotated[
        bool,
        typer.Option("--calls", "-c", help="List tool calls."),
    ] = False,
    call_index: Annotated[
        int | None,
        typer.Option(
            "--call",
            min=0,
            help="Inspect a single tool call by index in full detail.",
        ),
    ] = None,
    response: Annotated[
        bool,
        typer.Option("--response", "-r", help="Show agent response text."),
    ] = False,
    kind: Annotated[
        str | None,
        typer.Option(
            "--kind",
            "-k",
            help="Filter tool calls by kind (e.g. execute, read, edit).",
        ),
    ] = None,
    failed: Annotated[
        bool,
        typer.Option(
            "--failed",
            help="Only show failed or non-zero-exit tool calls.",
        ),
    ] = False,
    grep: Annotated[
        str | None,
        typer.Option(
            "--grep",
            "-g",
            help="Case-insensitive regex search across thoughts, tool calls, and response.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the selected view as JSON."),
    ] = False,
) -> None:
    """Explore thoughts, tool calls, and responses from a local transcript."""
    try:
        transcript_path = resolve_transcript_path(
            target, workspace=workspace, attempt=attempt
        )
        data = load_transcript(transcript_path)
    except Exception as exc:
        console.print(f"[bold red]Inspect Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    raw_calls = data.get("tool_calls")
    tool_calls: list[dict[str, Any]] = (
        [tc for tc in raw_calls if isinstance(tc, dict)]
        if isinstance(raw_calls, list)
        else []
    )
    thought_text = str(data.get("thought_text") or data.get("thought_preview") or "")
    response_text = str(data.get("response_text") or data.get("response_preview") or "")

    if call_index is not None:
        if call_index >= len(tool_calls):
            console.print(
                f"[bold red]Inspect Error:[/bold red] Tool call index {call_index} "
                f"out of range (0..{max(len(tool_calls) - 1, 0)})."
            )
            raise typer.Exit(code=1)
        tc = tool_calls[call_index]
        if json_output:
            typer.echo(json.dumps(tc, indent=2))
            return
        exit_code = extract_tool_exit_code(tc)
        exit_str = f" (exit {exit_code})" if exit_code is not None else ""
        dur = extract_tool_duration(tc)
        dur_str = f" | duration: {dur:.3f}s" if dur is not None else ""
        header = (
            f"id: {tc.get('id')}\n"
            f"kind: {tc.get('kind')} | status: {tc.get('status')}{exit_str}{dur_str}\n"
            f"title: {tc.get('title')}"
        )
        if tc.get("raw_input"):
            header += f"\nraw_input: {json.dumps(tc.get('raw_input'))}"
        output = extract_tool_output(tc)
        body = f"{header}\n\n{output}" if output else header
        console.print(
            Panel(
                Text(body),
                title=f"Tool Call #{call_index}",
                border_style="cyan",
            )
        )
        return

    matched_calls = filter_tool_calls(tool_calls, kind=kind, failed=failed, grep=grep)

    if grep is not None and not (thoughts or calls or response):
        thought_hits = search_text_lines(thought_text, grep) if thought_text else []
        response_hits = search_text_lines(response_text, grep) if response_text else []
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "transcript": str(transcript_path),
                        "pattern": grep,
                        "thought_matches": [
                            {"line": ln, "text": txt} for ln, txt in thought_hits
                        ],
                        "tool_call_matches": [
                            {"index": idx, **tc} for idx, tc in matched_calls
                        ],
                        "response_matches": [
                            {"line": ln, "text": txt} for ln, txt in response_hits
                        ],
                    },
                    indent=2,
                )
            )
            return
        console.print(f"[dim]Transcript:[/dim] {transcript_path}")
        if thought_hits:
            console.print(
                Panel(
                    Text("\n".join(f"{ln}: {txt}" for ln, txt in thought_hits)),
                    title=f"Thoughts matching /{grep}/ ({len(thought_hits)})",
                    border_style="magenta",
                )
            )
        if matched_calls:
            console.print(
                _build_tool_calls_table(
                    f"Tool Calls matching /{grep}/ ({len(matched_calls)})",
                    matched_calls,
                )
            )
        if response_hits:
            console.print(
                Panel(
                    Text("\n".join(f"{ln}: {txt}" for ln, txt in response_hits)),
                    title=f"Response matching /{grep}/ ({len(response_hits)})",
                    border_style="green",
                )
            )
        if not (thought_hits or matched_calls or response_hits):
            console.print(f"[dim]No matches for /{grep}/.[/dim]")
        return

    if json_output:
        payload: dict[str, Any] = {"transcript": str(transcript_path)}
        if thoughts:
            payload["thought_text"] = thought_text
        if response:
            payload["response_text"] = response_text
        if calls or not (thoughts or response):
            payload["tool_calls"] = [{"index": idx, **tc} for idx, tc in matched_calls]
        typer.echo(json.dumps(payload, indent=2))
        return

    console.print(f"[dim]Transcript:[/dim] {transcript_path}")

    if thoughts:
        console.print(
            Panel(
                Text(thought_text or "(empty)"),
                title="Thoughts",
                border_style="magenta",
            )
        )

    if response:
        console.print(
            Panel(
                Text(response_text or "(empty)"),
                title="Response",
                border_style="green",
            )
        )

    if calls or not (thoughts or response):
        if not matched_calls:
            console.print("[dim]No matching tool calls.[/dim]")
            return
        console.print(
            _build_tool_calls_table(
                f"Tool Calls ({len(matched_calls)}/{len(tool_calls)})",
                matched_calls,
            )
        )


@app.command("stats")
def stats_cmd(
    target: Annotated[
        str | None,
        typer.Argument(
            help="Optional run ID (or prefix), run/attempt dir, or transcript JSON file. Omit to aggregate across recent runs.",
        ),
    ] = None,
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Target workspace directory."),
    ] = Path("."),
    attempt: Annotated[
        int,
        typer.Option(
            "--attempt", "-a", min=1, help="Attempt number (when target is given)."
        ),
    ] = 1,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", min=1, help="Max recent runs to aggregate."),
    ] = 20,
    top: Annotated[
        int,
        typer.Option(
            "--top", min=1, help="Number of top slowest/largest items to show."
        ),
    ] = 10,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print stats as JSON."),
    ] = False,
) -> None:
    """Analyze run durations, tool-call mix, slowest calls, and largest outputs."""
    if target is not None:
        try:
            transcript_path = resolve_transcript_path(
                target, workspace=workspace, attempt=attempt
            )
            data = load_transcript(transcript_path)
        except Exception as exc:
            console.print(f"[bold red]Stats Error:[/bold red] {exc}")
            raise typer.Exit(code=1) from exc

        stats = compute_transcript_stats(data, top=top)
        if json_output:
            typer.echo(
                json.dumps({"transcript": str(transcript_path), **stats}, indent=2)
            )
            return

        dur = stats.get("duration_seconds")
        dur_str = f"{dur:.2f}s" if isinstance(dur, (int, float)) else "-"
        bd = stats["estimated_tokens_breakdown"]
        console.print(
            f"[dim]Transcript:[/dim] {transcript_path}\n"
            f"[bold]Turn Duration:[/bold] {dur_str} | "
            f"[bold]Tool Calls:[/bold] {stats['tool_call_count']} "
            f"([red]{stats['failed_count']} failed[/red]) | "
            f"[bold]Output:[/bold] {stats['total_output_chars']:,} chars\n"
            f"[bold]Tokens:[/bold] {stats['tokens']:,} "
            f"[dim](~{stats['estimated_tokens']:,} est.)[/dim] | "
            f"[bold]Cost:[/bold] ${stats['cost_usd']:.4f}\n"
            f"[dim]~Est. Breakdown: prompt {bd['prompt']:,} | "
            f"thought {bd['thought']:,} | "
            f"response {bd['response']:,} | "
            f"tool output {bd['tool_output']:,}[/dim]"
        )

        kind_table = Table(title="Tool Calls by Kind")
        kind_table.add_column("Kind", style="cyan", no_wrap=True)
        kind_table.add_column("Count", justify="right")
        kind_table.add_column("Failed", justify="right")
        kind_table.add_column("Total Dur", justify="right")
        kind_table.add_column("Max Dur", justify="right")
        kind_table.add_column("Output Chars", justify="right")
        kind_table.add_column("~Est. Tok", justify="right", style="dim")
        for row in stats["by_kind"]:
            tot_d = (
                f"{row['total_duration_seconds']:.2f}s" if row["timed_count"] else "-"
            )
            max_d = (
                f"{row['max_duration_seconds']:.2f}s"
                if row["max_duration_seconds"] is not None
                else "-"
            )
            kind_table.add_row(
                row["kind"],
                str(row["count"]),
                str(row["failed"]),
                tot_d,
                max_d,
                f"{row['total_output_chars']:,}",
                f"~{row['estimated_tokens']:,}",
            )
        console.print(kind_table)

        if stats["slowest_calls"]:
            slow_table = Table(title=f"Top {len(stats['slowest_calls'])} Slowest Calls")
            slow_table.add_column("#", justify="right", style="dim")
            slow_table.add_column("Kind", style="cyan", no_wrap=True)
            slow_table.add_column("Dur", justify="right", style="yellow")
            slow_table.add_column("Title")
            for row in stats["slowest_calls"]:
                slow_table.add_row(
                    str(row["index"]),
                    row["kind"],
                    f"{row['duration_seconds']:.3f}s",
                    row["title"],
                )
            console.print(slow_table)

        if stats["largest_calls"]:
            big_table = Table(
                title=f"Top {len(stats['largest_calls'])} Largest Outputs"
            )
            big_table.add_column("#", justify="right", style="dim")
            big_table.add_column("Kind", style="cyan", no_wrap=True)
            big_table.add_column("Chars", justify="right", style="magenta")
            big_table.add_column("~Est. Tok", justify="right", style="dim")
            big_table.add_column("Title")
            for row in stats["largest_calls"]:
                big_table.add_row(
                    str(row["index"]),
                    row["kind"],
                    f"{row['output_chars']:,}",
                    f"~{row['output_chars'] // 4:,}",
                    row["title"],
                )
            console.print(big_table)
        return

    agg = compute_runs_stats(workspace.resolve(), limit=limit, top=top)
    if json_output:
        typer.echo(json.dumps(agg, indent=2))
        return

    if agg["total_runs"] == 0:
        console.print("[dim]No factory runs found under `.factory/runs/`.[/dim]")
        return

    def _tok_cell(tokens: int, est_tokens: int) -> str:
        return f"{tokens:,}" if tokens > 0 else f"~{est_tokens:,}"

    def _result_icon(succeeded: bool | None) -> str:
        if succeeded is True:
            return "[green]✓[/green]"
        if succeeded is False:
            return "[red]✗[/red]"
        return "-"

    avg_tok = _tok_cell(agg["avg_tokens_per_run"], agg["avg_estimated_tokens_per_run"])
    console.print(
        f"[bold]Runs:[/bold] {agg['total_runs']} "
        f"([green]{agg['succeeded_runs']} passed[/green], "
        f"[red]{agg['failed_runs']} failed[/red]) | "
        f"[bold]Total Dur:[/bold] {agg['total_duration_seconds']:.1f}s "
        f"(avg {agg['avg_duration_seconds']:.1f}s)\n"
        f"[bold]Tokens:[/bold] {agg['total_tokens']:,} "
        f"[dim](~{agg['total_estimated_tokens']:,} est.)[/dim] | "
        f"[bold]Avg/run:[/bold] {avg_tok} | "
        f"[bold]Telemetry:[/bold] {agg['telemetry_runs']}/{agg['total_runs']} runs | "
        f"[bold]Cost:[/bold] ${agg['total_cost_usd']:.4f}"
    )

    stage_table = Table(title="Stats by Stage")
    stage_table.add_column("Stage", style="cyan", no_wrap=True)
    stage_table.add_column("Runs", justify="right")
    stage_table.add_column("Passed", justify="right")
    stage_table.add_column("Avg Dur", justify="right")
    stage_table.add_column("Avg Attempts", justify="right")
    stage_table.add_column("Tokens", justify="right", style="magenta")
    stage_table.add_column("Avg Tok", justify="right", style="dim")
    stage_table.add_column("Cost", justify="right", style="green")
    for sb in agg["by_stage"]:
        stage_table.add_row(
            sb["stage"],
            str(sb["runs"]),
            str(sb["succeeded"]),
            f"{sb['avg_duration_seconds']:.1f}s",
            f"{sb['avg_attempts']:.2f}",
            _tok_cell(sb["total_tokens"], sb["total_estimated_tokens"]),
            _tok_cell(sb["avg_tokens"], sb["avg_estimated_tokens"]),
            f"${sb['total_cost_usd']:.4f}",
        )
    console.print(stage_table)

    if agg["slowest_runs"]:
        runs_table = Table(title=f"Top {len(agg['slowest_runs'])} Slowest Runs")
        runs_table.add_column("Run ID", style="dim")
        runs_table.add_column("Stage", style="cyan", no_wrap=True)
        runs_table.add_column("Model")
        runs_table.add_column("Dur", justify="right", style="yellow")
        runs_table.add_column("Tokens", justify="right", style="magenta")
        runs_table.add_column("Cost", justify="right", style="green")
        runs_table.add_column("Result", no_wrap=True)
        for r in agg["slowest_runs"]:
            runs_table.add_row(
                r["run_id"],
                r["stage"],
                r["model"],
                f"{r['duration_seconds']:.1f}s",
                _tok_cell(r["tokens"], r["estimated_tokens"]),
                f"${r['cost_usd']:.4f}",
                _result_icon(r["succeeded"]),
            )
        console.print(runs_table)

    if agg["top_token_runs"]:
        tok_table = Table(title=f"Top {len(agg['top_token_runs'])} Highest-Token Runs")
        tok_table.add_column("Run ID", style="dim")
        tok_table.add_column("Stage", style="cyan", no_wrap=True)
        tok_table.add_column("Model")
        tok_table.add_column("Tokens", justify="right", style="magenta")
        tok_table.add_column("~Est. Tok", justify="right", style="dim")
        tok_table.add_column("Cost", justify="right", style="green")
        tok_table.add_column("Result", no_wrap=True)
        for r in agg["top_token_runs"]:
            tok_table.add_row(
                r["run_id"],
                r["stage"],
                r["model"],
                f"{r['tokens']:,}" if r["tokens"] > 0 else "-",
                f"~{r['estimated_tokens']:,}",
                f"${r['cost_usd']:.4f}",
                _result_icon(r["succeeded"]),
            )
        console.print(tok_table)

    if agg["slowest_calls"]:
        calls_table = Table(title=f"Top {len(agg['slowest_calls'])} Slowest Tool Calls")
        calls_table.add_column("Run ID", style="dim")
        calls_table.add_column("Attempt", style="dim")
        calls_table.add_column("Kind", style="cyan", no_wrap=True)
        calls_table.add_column("Dur", justify="right", style="yellow")
        calls_table.add_column("Title")
        for c in agg["slowest_calls"]:
            calls_table.add_row(
                c["run_id"],
                c["attempt"],
                c["kind"],
                f"{c['duration_seconds']:.3f}s",
                c["title"],
            )
        console.print(calls_table)
    elif agg["largest_calls"]:
        big_table = Table(title=f"Top {len(agg['largest_calls'])} Largest Tool Outputs")
        big_table.add_column("Run ID", style="dim")
        big_table.add_column("Attempt", style="dim")
        big_table.add_column("Kind", style="cyan", no_wrap=True)
        big_table.add_column("Chars", justify="right", style="magenta")
        big_table.add_column("~Est. Tok", justify="right", style="dim")
        big_table.add_column("Title")
        for c in agg["largest_calls"]:
            big_table.add_row(
                c["run_id"],
                c["attempt"],
                c["kind"],
                f"{c['output_chars']:,}",
                f"~{c['output_chars'] // 4:,}",
                c["title"],
            )
        console.print(big_table)


def main() -> None:
    """Entrypoint for the `acp-factory` CLI."""
    app()


if __name__ == "__main__":
    main()
