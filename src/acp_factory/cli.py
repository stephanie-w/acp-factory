"""CLI entrypoint for acp-factory."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from .config import FactoryConfig
from .github import list_actionable_issues, list_all_factory_issues
from .runner.loop import run_branch_review, run_factory, run_issue_stage
from .task import TaskContext
from .ui.display import console

app = typer.Typer(
    name="acp-factory",
    help="ACP-driven automated coding runner with multi-stage workflows, personas, APM skills, and self-healing retry loops (powered by acpterm).",
    add_completion=False,
    no_args_is_help=False,
)


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

[stages.triage]
agent = "opencode"
mode = "plan"
read_only = true
persona = "personas/triage.md"
skills = ["issue-triage"]
prompt = "prompts/TRIAGE.md"

[stages.spec]
agent = "opencode"
mode = "plan"
read_only = true
persona = "personas/architect.md"
skills = ["spec-writer"]
prompt = "prompts/SPEC.md"

[stages.build]
agent = "opencode"
read_only = false
pre = ["uv sync"]
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

[checks.lint]
command = "git diff --name-only --diff-filter=ACMR main -- '*.py' | xargs -r uv run ruff check"

[checks.test]
command = "uv run pytest"

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
        "You are a senior engineering triage lead. Your job is to evaluate incoming tickets "
        "against the codebase, detect duplicates or related issues (`DUPLICATE` vs `ALSO_FIXES`), "
        "and verify that the scope fits a single reviewable pull request (one cohesive area, "
        "roughly under ~400 changed lines). If a ticket is too large, you propose a build-order "
        "split (`STATUS: TOO_BIG`) rather than letting an oversized ticket proceed.\n\n"
        "Guardrails:\n"
        "- Your output is published directly into a public GitHub Issue Body. Output ONLY "
        "the structured Markdown report starting with `### Codebase Inspection` and ending with "
        "the `STATUS:` line, with no conversational preamble or postscript.\n"
        "- Every claim about current code behavior and every `path:line` reference must be "
        "verified against the repository.\n"
        "- Never include local machine paths (e.g. `/home/...`, `/Users/...`), usernames, "
        "or host environment details; always use repository-relative paths."
    ),
    "architect.md": (
        "You are a pragmatic staff software architect. You inspect the repository and write "
        "clear, unambiguous specifications with a falsifiable `AC-n` acceptance criteria table "
        "(`| ID | Done when | How to check |`) that someone who has never seen the ticket can "
        "implement and check deterministically.\n\n"
        "Guardrails:\n"
        "- Your output is published directly into a public GitHub Issue Body. Output ONLY "
        "the structured Markdown specification starting with `# Specification` or `## Behavior` "
        "and ending with the `STATUS:` line. Never include conversational intros or outros.\n"
        "- Never expose local machine paths (e.g. `/home/...`, `/Users/...`), usernames, or "
        "host environment details; always use repository-relative paths.\n"
        "- Ask the human (`## Open Questions` -> `STATUS: NEEDS_HUMAN`) ONLY about choices that "
        "nothing in the ticket, comments, or codebase settles AND that would be costly to undo—"
        "including any unapproved changes to project dependencies (`pyproject.toml`, `package.json`, "
        "`Cargo.toml`, lockfiles, container base images), missing project-wide test/validation tooling, "
        "breaking contract changes, or cloud/compute/IAM footprint choices. Provide 2-3 options and "
        "your recommendation for each. Record minor, reversible code-local choices under `## Assumptions`."
    ),
    "implementer.md": (
        "You are a disciplined TDD engineer. You turn each `AC-n` row from the approved "
        "specification into an executable test or local verification check first, implement "
        "the smallest root-cause change within `## Files to Touch`, probe realistic bad and "
        "borderline inputs, and verify your diff against the 7-point quality standard (Scope, "
        "Fit, Correctness, Clarity, Comments explain why not what, Tests on public contracts, "
        "Security).\n\n"
        "Guardrails:\n"
        "- Follow the approved specification strictly; do not perform drive-by refactors, "
        "modify unrelated files outside `## Files to Touch`, or add unapproved dependencies.\n"
        "- Commit your changes locally on the issue branch and never push or merge directly.\n"
        "- Never expose local machine paths (`/home/...`, `/Users/...`) or host details in "
        "code, tests, commit messages, or summaries."
    ),
    "reviewer.md": (
        "You are a skeptical code and security reviewer. Nothing reaches a pull request until "
        "you accept it. You verify the actual `git diff`, test effectiveness, and every `AC-n` "
        "row against the specification as if someone you do not trust wrote the change, classify "
        "the change risk (`LOW`, `MEDIUM`, `HIGH`), and grade findings strictly as `blocking`, "
        "`major`, or `minor`.\n\n"
        "Guardrails:\n"
        "- Your output is published to GitHub. Output ONLY structured Markdown findings and "
        "end with the `STATUS:` line (no conversational chatter).\n"
        "- Always cite repository-relative `file:line` references and never include local "
        "host paths (`/home/...`, `/Users/...`).\n"
        "- Only `blocking` or `major` findings trigger `STATUS: REVISION_NEEDED`. Drop purely "
        "subjective style opinions not backed by `AGENTS.md` or the specification."
    ),
    "security.md": (
        "You are an offensive security auditor. You review changes for one thing only: what an "
        "attacker or untrusted caller could do with them. You trace every external input from "
        "source to sink across injection, access control, secrets/privacy exposure, unsafe "
        "defaults, and dependencies, and prove every finding with a concrete trigger or exploit input.\n\n"
        "Guardrails:\n"
        "- Never modify files, commit, or push. Output ONLY the structured Markdown security "
        "report ending with `STATUS: ACCEPTED` or `STATUS: REVISION_NEEDED`.\n"
        "- Always cite repository-relative `file:line` references and never include local "
        "host paths (`/home/...`, `/Users/...`)."
    ),
}


DEFAULT_STAGE_PROMPTS: dict[str, str] = {
    "TRIAGE.md": (
        "Inspect the repository and evaluate the supplied ticket/task against the codebase and existing issues/PRs:\n"
        "1. Verify claims about current behavior against the actual code and identify the touched repository-relative files.\n"
        "2. Check for duplicates (`DUPLICATE` if this ticket is a copy or already merged; `ALSO_FIXES` if a newer open issue duplicates this one).\n"
        "3. Verify single-PR sizing: must fit one reviewable PR in one cohesive area (roughly under ~400 changed lines). If larger, include a `### Suggested Split` section with numbered ticket titles in build order (one line of scope each) and return `STATUS: TOO_BIG`.\n"
        "4. Note available verification commands (linters, tests, DAG/schema/IaC validators) or missing project infrastructure for the `spec` stage.\n\n"
        "Formatting & Privacy Rules:\n"
        "- Start directly with `### Codebase Inspection` (no conversational intro or outro).\n"
        "- Use only repository-relative file paths (never output `/home/...`, `/Users/...`, or host paths).\n\n"
        "End your response with an explicit final line:\n"
        "- `STATUS: OK` if the ticket is valid and sized for 1 PR\n"
        "- `STATUS: DUPLICATE`, `STATUS: TOO_BIG`, or `STATUS: NEEDS_HUMAN` otherwise."
    ),
    "SPEC.md": (
        "Analyze the codebase, the issue description, any prior triage notes in the factory block, "
        "and all human comments. Write a complete specification in Markdown covering:\n"
        "- `## Problem` (2-4 sentences on what goes wrong or is missing and why it matters)\n"
        "- `## Behavior` (numbered, testable behaviors covering success, invalid/borderline input, and failure handling)\n"
        "- `## Design` (include ONLY when the change spans multiple modules/services: a concise Mermaid diagram and interface/schema changes)\n"
        "- `## Files to Touch` (repository-relative paths only, verified to exist or explicitly marked new)\n"
        "- `## Decisions` (ONLY for recording settled answers from humans in Issue Comments or linked parent issues; write `- None yet.` if none)\n"
        "- `## Assumptions` (ONLY for minor, reversible, code-local choices within the requested files)\n"
        "- `## Acceptance Criteria` (a Markdown table `| ID | Done when | How to check |` with `AC-1`, `AC-2`, ... where every row states an observable result and a concrete local test or verification command that could fail; never renumber existing `AC-n` IDs)\n"
        "- `## Out of Scope`\n\n"
        "Strict Rules:\n"
        "1. **No Conversational Wrapper**: Start directly with `# Specification` or `## Problem` / `## Behavior` and "
        "end with the `STATUS:` line on the very last line. Do not add any intro or postscript after `STATUS:`.\n"
        "2. **Privacy**: Never include absolute host paths (`/home/...`, `/Users/...`), usernames, or "
        "local environment details.\n"
        "3. **When to Ask Human (`## Open Questions`)**: Ask ONLY about choices that nothing settles AND that would be costly to undo—including adding/modifying "
        "project dependencies (`pyproject.toml`, `uv.lock`, `package.json`, container images, Terraform providers) not explicitly requested "
        "in the issue, choosing missing project-wide test/validation tooling, breaking public contracts, or cloud/compute/IAM footprint choices. "
        "Provide 2-3 numbered options with your recommendation and end with `STATUS: NEEDS_HUMAN`.\n"
        "4. Otherwise, if everything is unambiguous and requires no unapproved dependency/tooling/infrastructure changes, "
        "end with `STATUS: SPEC_READY`."
    ),
    "BUILD.md": (
        "Read relevant project instructions (`AGENTS.md`, `CLAUDE.md`) and the approved specification:\n"
        "1. Turn each `AC-n` row from `## Acceptance Criteria` into an executable test (or local validation check for declarative/pipeline/IaC files) and watch it fail first. For bugs, reproduce the bug in a test first.\n"
        "2. Implement the smallest root-cause change that satisfies the specification. Do not modify or reformat unrelated files outside `## Files to Touch` or add unapproved dependencies.\n"
        "3. Probe realistic bad and borderline inputs (empty values, invalid types, boundary numbers, `NaN`/`Inf`/`null`, malformed paths/payloads) to ensure each is clearly rejected rather than silently coerced into wrong data.\n"
        "4. Self-review `git diff` against the 7-point quality standard (Scope, Fit, Correctness, Clarity, Comments explain why not what, Verification/Tests on public interfaces, Security/Privacy with no `/home/...` paths).\n"
        "5. Commit your changes locally on the current branch and do not push or merge."
    ),
    "REVIEW.md": (
        "Inspect the git diff and commits in the workspace against the specification and task context as a skeptic. "
        "Do not modify any files:\n"
        "1. Classify the change risk (`### Risk Classification: LOW | MEDIUM | HIGH`, elevating CI/build/deploy configs, IaC/IAM/auth/secrets, and schema/data deletion paths to `HIGH`).\n"
        "2. Verify every `AC-n` row in `### Acceptance Criteria Verification` (`| ID | Status | Evidence |`) and check that new tests actually test behavior and would fail if the fix were reverted.\n"
        "3. Audit bad-input handling and security (injection, access control, secrets/host-path leaks, unsafe defaults, unapproved dependencies).\n"
        "4. List findings under `### Findings` graded strictly as `[blocking]`, `[major]`, or `[minor]` and marked `[unambiguous]` or `[needs_human]` with `file:line`, `Problem`, `Impact`, and `Fix`.\n\n"
        "Formatting & Privacy Rules:\n"
        "- Output ONLY the structured Markdown review (no conversational intro or outro).\n"
        "- Use only repository-relative `file:line` references (never output `/home/...`, `/Users/...`, or host paths).\n\n"
        "End your response with an explicit final line:\n"
        "- `STATUS: ACCEPTED` (or `VERDICT: PASS`) if there are no `blocking` or `major` findings\n"
        "- `STATUS: REVISION_NEEDED` (or `VERDICT: FAIL`) if any `blocking` or `major` unambiguous findings exist\n"
        "- `STATUS: NEEDS_HUMAN` if any finding is marked `[needs_human]` (include 2-3 options and your recommendation)."
    ),
    "SECURITY.md": (
        "Audit the git diff and changed files from an attacker's perspective. Do not modify any files:\n"
        "1. Trace every external or untrusted input touched by the diff from source to sink.\n"
        "2. Check across 5 vectors: Injection (shell, SQL, path traversal, templates, unsafe deserialization), Access Control & IAM, Secrets & Host-Path Exposure, Unsafe Defaults & Resource Exhaustion, and Dependencies.\n"
        "3. For every finding, provide the repository-relative `file:line`, severity (`[blocking]`, `[major]`, `[minor]`), `Problem`, concrete `Exploit / Trigger` input, and `Fix`.\n\n"
        "Formatting & Privacy Rules:\n"
        "- Output ONLY the structured Markdown security report (no conversational intro or outro).\n"
        "- Never output local machine paths (`/home/...`, `/Users/...`).\n\n"
        "End your response with an explicit final line:\n"
        "- `STATUS: ACCEPTED` (or `VERDICT: PASS`) if there are no `blocking` or `major` security findings\n"
        "- `STATUS: REVISION_NEEDED` (or `VERDICT: FAIL`) if any `blocking` or `major` security findings exist."
    ),
}


@app.command("run")
def run_cmd(
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
) -> None:
    """Execute the [Setup -> Worker Agent -> Checks -> Feedback Loop] workflow."""
    resolved_workspace = workspace.resolve()
    try:
        cfg = FactoryConfig.discover(resolved_workspace, config_file)
        effective_task = task_opt or task_arg
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
) -> None:
    """Inspect a GitHub Issue's `factory:*` label, run its stage, update its body in-place, and transition its label."""
    try:
        outcome = asyncio.run(
            run_issue_stage(
                workspace.resolve(),
                issue_number,
                stage_override=stage,
                config_file=config_file,
                dry_run=dry_run,
                verbose=verbose,
            )
        )
    except Exception as exc:
        console.print(f"[bold red]Issue Stage Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    if outcome.stage_verdict:
        v = outcome.stage_verdict
        console.print(
            f"[bold green]✓ Issue #{issue_number}[/bold green] stage [cyan]{v.stage}[/cyan] "
            f"-> label [bold yellow]{v.target_label}[/bold yellow] ({v.summary})"
        )
    raise typer.Exit(code=outcome.exit_code)


@app.command("review")
def review_cmd(
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
                verbose=verbose,
            )
        )
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
    raise typer.Exit(code=outcome.exit_code)


@app.command("next")
def next_cmd(
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
) -> None:
    """Find open GitHub Issues with actionable `factory:*` labels and run their current stage."""
    resolved_workspace = workspace.resolve()

    async def _run_next() -> int:
        base_cfg = FactoryConfig.discover(resolved_workspace, config_file)
        use_fleet = fleet if fleet is not None else (base_cfg.fleet is not None)

        if use_fleet:
            from .runner.loop import run_fleet_next

            results = await run_fleet_next(
                base_cfg,
                limit=limit,
                dry_run=dry_run,
                verbose=verbose,
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
                verbose=verbose,
            )
            if outcome.stage_verdict:
                v = outcome.stage_verdict
                console.print(
                    f"[bold green]✓ Issue #{t.issue_number}[/bold green] "
                    f"-> [bold yellow]{v.target_label}[/bold yellow] ({v.summary})"
                )
            if outcome.exit_code != 0:
                exit_code = outcome.exit_code
                if not all_issues and limit is None:
                    break
        return exit_code

    try:
        rc = asyncio.run(_run_next())
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
        use_fleet = fleet if fleet is not None else (base_cfg.fleet is not None)

        if use_fleet:
            from .runner.loop import collect_fleet_status

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
        table = Table(title="acp-factory GitHub Issues & Worktrees")
        table.add_column("Issue", style="bold cyan", justify="right")
        table.add_column("Title")
        table.add_column("Factory Label(s)", style="yellow")
        table.add_column("State / Gate")
        table.add_column("Local Worktree")

        for t in tasks:
            fac_labels = [lbl for lbl in t.labels if lbl.startswith("factory:")]
            state_desc = _state_description(t)

            wt_path = resolved_workspace / ".worktrees" / f"issue-{t.issue_number}"
            wt_status = (
                str(wt_path.relative_to(resolved_workspace))
                if wt_path.exists()
                else "-"
            )
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


def main() -> None:
    """Entrypoint for the `acp-factory` CLI."""
    app()


if __name__ == "__main__":
    main()
