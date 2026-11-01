"""Rich panels, spinners, and log formatting for acp-factory."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from ..config import FactoryConfig
    from ..github import DryRunPlan
    from ..runner.checks import CheckResult


console = Console(highlight=False)


class FactoryDisplay:
    """Renders structured Rich terminal UI for the acp-factory orchestration loop."""

    def __init__(self, custom_console: Console | None = None) -> None:
        self.console = custom_console or console

    def show_run_header(
        self,
        config: FactoryConfig,
        workspace: Path,
        worktree_branch: str | None = None,
    ) -> None:
        """Display the initial run configuration summary panel."""
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold cyan", justify="right")
        grid.add_column()

        agent_desc = (
            " ".join(config.agent.command)
            if config.agent.command
            else f"{config.agent.name} (via acpterm)"
        )
        if config.agent.model:
            agent_desc += f" [dim](model: {config.agent.model})[/dim]"
        if config.agent.mode:
            agent_desc += f" [dim](mode: {config.agent.mode})[/dim]"
        if config.agent.read_only:
            agent_desc += " [yellow][read-only][/yellow]"

        grid.add_row("Workspace:", str(workspace))
        if config.active_stage:
            grid.add_row("Stage:", f"[bold green]{config.active_stage}[/bold green]")
        if config.dry_run:
            grid.add_row(
                "Dry Run:",
                "[bold yellow]YES (no automatic GitHub writes; commands printed for human)[/bold yellow]",
            )
        if worktree_branch:
            grid.add_row("Worktree Branch:", f"[magenta]{worktree_branch}[/magenta]")
        grid.add_row("Agent:", agent_desc)
        if config.agent.persona:
            grid.add_row("Persona:", config.agent.persona)
        if config.agent.skills:
            grid.add_row("Skills:", ", ".join(config.agent.skills))
        grid.add_row("Max Attempts:", str(config.retry.max_attempts))
        grid.add_row("Setup Steps:", str(len(config.setup)))
        grid.add_row("Checks:", str(len(config.checks)))
        grid.add_row("Task:", config.task.strip() or "[dim](none)[/dim]")

        self.console.print(
            Panel(
                grid,
                title="[bold white]acp-factory[/bold white]",
                border_style="cyan",
                expand=False,
            )
        )

    def show_phase_banner(self, title: str, style: str = "cyan") -> None:
        """Print a horizontal rule announcing a workflow phase."""
        self.console.print()
        self.console.print(Rule(f"[bold {style}]{title}[/bold {style}]", style=style))

    def show_attempt_banner(self, attempt: int, max_attempts: int) -> None:
        """Print a banner for the current agent attempt."""
        self.console.print()
        self.console.print(
            Rule(
                f"[bold yellow]Worker Attempt {attempt} / {max_attempts}[/bold yellow]",
                style="yellow",
            )
        )

    def show_setup_step(
        self, cmd: str, passed: bool, duration: float, output: str = ""
    ) -> None:
        """Display the result of a pre-setup shell command."""
        status_icon = (
            "[bold green]✓[/bold green]" if passed else "[bold red]✗[/bold red]"
        )
        self.console.print(
            f"  {status_icon} [bold]{cmd}[/bold] [dim]({duration:.2f}s)[/dim]"
        )
        if not passed and output.strip():
            self.console.print(
                Panel(
                    output.strip(),
                    title="[bold red]Setup Command Failed[/bold red]",
                    border_style="red",
                )
            )

    def show_check_result(self, result: CheckResult) -> None:
        """Render a pass/fail panel or status line for a single validation check."""
        kind_badge = (
            "[dim cyan][review][/dim cyan]"
            if result.check_type == "prompt"
            else "[dim blue][cmd][/dim blue]"
        )
        if result.passed:
            self.console.print(
                f"  [bold green]✓ PASS[/bold green] {kind_badge} [bold]{result.name}[/bold] "
                f"[dim]({result.duration_seconds:.2f}s)[/dim]"
            )
            return

        self.console.print(
            f"  [bold red]✗ FAIL[/bold red] {kind_badge} [bold]{result.name}[/bold] "
            f"[dim](exit={result.exit_code}, {result.duration_seconds:.2f}s)[/dim]"
        )
        body = result.truncated_output.strip() or "(no output captured)"
        self.console.print(
            Panel(
                Text(body),
                title=f"[bold red]Check Failed: {result.name}[/bold red]",
                subtitle=f"[dim]{result.command_or_prompt}[/dim]",
                border_style="red",
            )
        )

    def show_final_summary(
        self,
        *,
        succeeded: bool,
        attempts_used: int,
        max_attempts: int,
        total_duration: float,
        kept_worktree_path: Path | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Display the final outcome panel at the end of the factory run."""
        self.console.print()
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold", justify="right")
        grid.add_column()

        status_text = (
            "[bold green]SUCCESS (All Checks Passed)[/bold green]"
            if succeeded
            else "[bold red]FAILED[/bold red]"
        )
        grid.add_row("Status:", status_text)
        grid.add_row("Attempts:", f"{attempts_used} / {max_attempts}")
        grid.add_row("Total Time:", f"{total_duration:.2f}s")
        if failure_reason:
            grid.add_row("Reason:", f"[red]{failure_reason}[/red]")
        if kept_worktree_path:
            grid.add_row("Worktree Saved:", f"[yellow]{kept_worktree_path}[/yellow]")

        border = "green" if succeeded else "red"
        title = (
            "[bold green]Factory Run Complete[/bold green]"
            if succeeded
            else "[bold red]Factory Run Failed[/bold red]"
        )
        self.console.print(Panel(grid, title=title, border_style=border, expand=False))

    def show_dry_run_plan(self, plan: DryRunPlan) -> None:
        """Render the generated Issue/PR Body preview and manual `gh` commands in --dry-run mode."""
        self.console.print()
        if plan.updated_factory_block:
            panel_title = (
                f"[bold cyan]Dry-Run Preview: Issue #{plan.issue_number} Factory Block[/bold cyan]"
                if plan.issue_number > 0
                else "[bold cyan]Dry-Run Preview: Standalone Branch Review[/bold cyan]"
            )
            saved_file = plan.issue_body_file or plan.pr_body_file
            self.console.print(
                Panel(
                    Text(plan.updated_factory_block),
                    title=panel_title,
                    subtitle=(
                        f"[dim]Saved to {saved_file}[/dim]" if saved_file else None
                    ),
                    border_style="cyan",
                )
            )

        # A review that did not accept the change has nothing to publish. Show a
        # clear verdict panel instead of a misleading "run these commands" header.
        if plan.stage == "review" and not plan.publish_pr:
            status = plan.verdict_status or "unknown"
            self.console.print(
                Panel(
                    Text(
                        f"Review verdict: {status}\n\n"
                        "No publish commands were generated. Fix the findings above "
                        "and re-run the review."
                    ),
                    title="[bold red]Dry-Run: Review Not Accepted[/bold red]",
                    border_style="red",
                )
            )
            return

        cmd_lines = [
            "# Run these commands manually when you are satisfied with the output:",
            *plan.commands,
        ]
        self.console.print(
            Panel(
                "\n".join(cmd_lines),
                title="[bold yellow]Dry-Run: Manual GitHub Commands (Human Action)[/bold yellow]",
                border_style="yellow",
            )
        )
