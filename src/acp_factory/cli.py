"""CLI entrypoint for acp-factory."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from .config import FactoryConfig
from .github import list_actionable_issues, list_all_factory_issues
from .runner.loop import run_factory, run_issue_stage
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
prompt = "prompts/TRIAGE.md"

[stages.spec]
agent = "opencode"
mode = "plan"
read_only = true
persona = "personas/architect.md"
prompt = "prompts/SPEC.md"

[stages.build]
agent = "opencode"
read_only = false
pre = ["uv sync"]
persona = "personas/implementer.md"
prompt = "prompts/BUILD.md"
checks = ["lint", "test"]

[stages.review]
agent = "opencode"
read_only = true
persona = "personas/reviewer.md"
prompt = "prompts/REVIEW.md"

[checks.lint]
command = "git diff --name-only --diff-filter=ACMR main -- '*.py' | xargs -r uv run ruff check"

[checks.test]
command = "uv run pytest"

[checks.review]
persona = "personas/reviewer.md"
prompt = "prompts/REVIEW.md"
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
        "against the codebase, detect duplicates, and verify that the scope fits a single "
        "reviewable pull request.\n\n"
        "Guardrails:\n"
        "- Your output is published directly into a public GitHub Issue Body. Output ONLY "
        "the structured Markdown report with no conversational preamble or postscript.\n"
        "- Never include local machine paths (e.g. `/home/...`, `/Users/...`), usernames, "
        "or host environment details; always use repository-relative paths."
    ),
    "architect.md": (
        "You are a pragmatic staff software architect. You inspect the repository and write "
        "clear, unambiguous technical and product specifications with explicit acceptance criteria.\n\n"
        "Guardrails:\n"
        "- Your output is published directly into a public GitHub Issue Body. Output ONLY "
        "the structured Markdown specification starting with `# Specification` or `## Behavior` "
        "and ending with the `STATUS:` line. Never include conversational intros or outros.\n"
        "- Never expose local machine paths (e.g. `/home/...`, `/Users/...`), usernames, or "
        "host environment details; always use repository-relative paths.\n"
        "- Never silently assume changes to project dependencies (`pyproject.toml`, `package.json`, "
        "`Cargo.toml`, lockfiles) or missing project-wide tooling (e.g. missing test runners or "
        "async plugins). Always surface those in `## Open Questions` and end with `STATUS: NEEDS_HUMAN`."
    ),
    "implementer.md": (
        "You are a disciplined TDD software engineer. You implement the approved specification "
        "with minimal, focused code changes and thorough tests.\n\n"
        "Guardrails:\n"
        "- Follow the approved specification strictly; do not modify unrelated files or add "
        "unapproved dependencies.\n"
        "- Commit your changes locally on the issue branch and never push or merge directly.\n"
        "- Never expose local machine paths or host details in commit messages or summaries."
    ),
    "reviewer.md": (
        "You are a skeptical code and security reviewer. You verify the actual git diff and test "
        "coverage against the specification, never trusting summaries blindly.\n\n"
        "Guardrails:\n"
        "- Your output is published to GitHub. Output ONLY structured Markdown findings and "
        "end with the `STATUS:` line (no conversational chatter).\n"
        "- Always cite repository-relative `file:line` references and never include local "
        "host paths (`/home/...`, `/Users/...`)."
    ),
}


DEFAULT_STAGE_PROMPTS: dict[str, str] = {
    "TRIAGE.md": (
        "Inspect the repository and evaluate the supplied ticket/task. Check for duplicates "
        "and confirm whether the ticket is small enough for a single pull request.\n\n"
        "Formatting & Privacy Rules:\n"
        "- Start directly with `### Codebase Inspection` (no conversational intro or outro).\n"
        "- Use only repository-relative file paths (never output `/home/...` or host paths).\n"
        "- Note any missing project infrastructure (e.g. missing `tests/` directory or test runner) "
        "so the `spec` stage can address it.\n\n"
        "End your response with an explicit final line:\n"
        "- `STATUS: OK` if the ticket is valid and sized for 1 PR\n"
        "- `STATUS: DUPLICATE`, `STATUS: TOO_BIG`, or `STATUS: NEEDS_HUMAN` otherwise."
    ),
    "SPEC.md": (
        "Analyze the codebase, the issue description, any prior triage notes in the factory block, "
        "and all human comments. Write a complete specification in Markdown covering:\n"
        "- `## Behavior`\n"
        "- `## Files to Touch` (repository-relative paths only)\n"
        "- `## Assumptions` (ONLY for minor, reversible, code-local choices within the requested files)\n"
        "- `## Decisions` (ONLY for recording answers provided by humans in Issue Comments; write `- None yet.` if none)\n"
        "- `## Out of Scope`\n"
        "- `## Acceptance Criteria`\n\n"
        "Strict Rules:\n"
        "1. **No Conversational Wrapper**: Start directly with `# Specification` or `## Behavior` and "
        "end with the `STATUS:` line on the very last line. Do not add any intro or postscript after `STATUS:`.\n"
        "2. **Privacy**: Never include absolute host paths (`/home/...`, `/Users/...`), usernames, or "
        "local environment details.\n"
        "3. **When to Ask Human (`## Open Questions`)**: If fulfilling the issue requires adding/modifying "
        "project dependencies (`pyproject.toml`, `uv.lock`, `package.json`, etc.) not explicitly requested "
        "in the issue, choosing missing project-wide test/build tooling (e.g. `pytest` / `pytest-asyncio`), "
        "or resolving ambiguous product/architectural choices, you MUST add a `## Open Questions` section "
        "with numbered options and end with `STATUS: NEEDS_HUMAN`.\n"
        "4. Otherwise, if everything is unambiguous and requires no unapproved dependency/tooling changes, "
        "end with `STATUS: SPEC_READY`."
    ),
    "BUILD.md": (
        "Read relevant project instructions (AGENTS.md) and the approved specification. Implement "
        "the task strictly according to the specification and acceptance criteria with appropriate tests. "
        "Do not modify or reformat unrelated files outside `## Files to Touch`. "
        "Commit your changes locally on the current branch and do not push or merge."
    ),
    "REVIEW.md": (
        "Inspect the git diff and commits in the workspace against the specification and task context. "
        "Do not modify any files.\n\n"
        "Formatting & Privacy Rules:\n"
        "- Output ONLY the structured Markdown review (no conversational intro or outro).\n"
        "- Use only repository-relative `file:line` references (never output `/home/...` or host paths).\n\n"
        "End your response with an explicit final line:\n"
        "- `STATUS: ACCEPTED` (or `VERDICT: PASS`) if all criteria and quality standards are met\n"
        "- `STATUS: REVISION_NEEDED` (or `VERDICT: FAIL`) with specific `file:line` findings if fixes are needed\n"
        "- `STATUS: NEEDS_HUMAN` if a human decision is required."
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
        tasks = await list_actionable_issues(resolved_workspace)
        if not tasks:
            console.print(
                "[dim]No open GitHub issues with actionable `factory:*` labels "
                "(triage, spec, build, review, done) found.[/dim]"
            )
            return 0

        targets = tasks if all_issues else tasks[:1]
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
                if not all_issues:
                    break
        return exit_code

    try:
        rc = asyncio.run(_run_next())
    except Exception as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    raise typer.Exit(code=rc)


@app.command("status")
def status_cmd(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Target workspace directory."),
    ] = Path("."),
) -> None:
    """Display open `factory:*` GitHub Issues, their current stage/human gate, and local `.worktrees/`."""
    resolved_workspace = workspace.resolve()

    async def _show_status() -> None:
        tasks = await list_all_factory_issues(resolved_workspace)
        table = Table(title="acp-factory GitHub Issues & Worktrees")
        table.add_column("Issue", style="bold cyan", justify="right")
        table.add_column("Title")
        table.add_column("Factory Label(s)", style="yellow")
        table.add_column("State / Gate")
        table.add_column("Local Worktree")

        for t in tasks:
            fac_labels = [lbl for lbl in t.labels if lbl.startswith("factory:")]
            if t.current_stage:
                state_desc = f"[green]Actionable ({t.current_stage})[/green]"
            elif "factory:spec-ready" in fac_labels:
                state_desc = (
                    "[bold magenta]🛑 Human Gate 1 (Spec Approval)[/bold magenta]"
                )
            elif "factory:pr-ready" in fac_labels:
                state_desc = (
                    "[bold magenta]🛑 Human Gate 2 (PR Review & Merge)[/bold magenta]"
                )
            elif "factory:needs-human" in fac_labels:
                state_desc = "[bold red]⚠️ Needs Human Input[/bold red]"
            elif "factory:completed" in fac_labels:
                state_desc = "[dim]Completed[/dim]"
            else:
                state_desc = "[dim]Idle[/dim]"

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
