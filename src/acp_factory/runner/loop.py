"""Core [Setup -> Worker Agent via ACP -> Sequential Checks -> Feedback Loop] orchestrator."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..acp.client import FactoryACPClient
from ..config import CheckConfig, FactoryConfig
from ..github import (
    DryRunPlan,
    build_dry_run_gh_plan,
    fetch_issue,
    push_and_create_or_update_pr,
    transition_issue_label,
    update_issue_factory_block,
)
from ..task import (
    LABEL_COMPLETED,
    LABEL_NEEDS_HUMAN,
    StageVerdict,
    TaskContext,
    format_task_prompt_context,
    parse_stage_verdict,
)
from ..ui.display import FactoryDisplay
from .checks import CheckResult, run_checks_sequential, run_shell_command
from .worktree import WorktreeManager


@dataclass
class FactoryRunOutcome:
    """Final result of an acp-factory orchestration run."""

    succeeded: bool
    exit_code: int
    attempts_used: int
    total_duration_seconds: float
    last_response_text: str = ""
    run_dir: Path | None = None
    check_history: list[list[CheckResult]] = field(default_factory=list)
    preserved_worktree: Path | None = None
    failure_reason: str | None = None
    stage_verdict: StageVerdict | None = None
    dry_run_plan: DryRunPlan | None = None


def build_initial_prompt(task: str, checks: list[CheckConfig]) -> str:
    """Construct the initial task prompt sent to the ACP worker agent."""
    lines = [
        "# Coding Task",
        task.strip(),
        "",
    ]
    if checks:
        lines.append("## Verification Checks")
        lines.append(
            "After you complete your changes, the following verification checks will be "
            "executed sequentially in this workspace:"
        )
        for idx, chk in enumerate(checks, start=1):
            if chk.cmd:
                lines.append(f"{idx}. **{chk.name}** (`{chk.cmd}`)")
            elif chk.prompt:
                lines.append(f"{idx}. **{chk.name}** (Review: {chk.prompt})")
        lines.append("")
        lines.append(
            "Ensure your implementation is complete, clean, and satisfies all checks."
        )
    return "\n".join(lines)


def build_retry_prompt(
    task: str,
    attempt: int,
    max_attempts: int,
    check_results: list[CheckResult],
) -> str:
    """Construct a structured feedback prompt when a check fails."""
    failed_check = check_results[-1]
    passed_checks = [r for r in check_results[:-1] if r.passed]

    lines = [
        f"# Verification Failed — Retry Attempt {attempt} of {max_attempts}",
        "",
        "## Original Task",
        task.strip(),
        "",
    ]

    if passed_checks:
        lines.append("## Previously Passing Checks")
        for r in passed_checks:
            lines.append(f"- ✓ **{r.name}**")
        lines.append("")

    lines.append("## Failing Check Details")
    lines.append(f"- **Check Name:** {failed_check.name}")
    if failed_check.check_type == "cmd":
        lines.append(f"- **Command:** `{failed_check.command_or_prompt}`")
        lines.append(f"- **Exit Code:** `{failed_check.exit_code}`")
    else:
        lines.append(f"- **Review Criteria:** {failed_check.command_or_prompt}")
    lines.append("")
    lines.append("### Error Output / Trace")
    lines.append("```text")
    lines.append(failed_check.truncated_output.strip() or "(no output)")
    lines.append("```")
    lines.append("")
    lines.append("## Instructions")
    lines.append(
        "Analyze the failure output above, fix the root cause in the workspace without "
        "breaking prior work or previously passing checks, and verify your changes."
    )
    return "\n".join(lines)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-") or "check"


def _persist_run_artifacts(
    workspace: Path,
    run_id: str,
    config: FactoryConfig,
    *,
    succeeded: bool,
    attempts_used: int,
    total_duration: float,
    setup_logs: list[str],
    turn_transcripts: list[str],
    check_history: list[list[CheckResult]],
    failure_reason: str | None,
) -> Path | None:
    """Write structured run logs into `.factory/runs/<run-id>/` if `.factory/` is present."""
    factory_dir = workspace / ".factory"
    if not factory_dir.is_dir():
        return None

    run_dir = factory_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if setup_logs:
        (run_dir / "setup.log").write_text("\n\n".join(setup_logs), encoding="utf-8")

    for idx, transcript_md in enumerate(turn_transcripts, start=1):
        attempt_dir = run_dir / f"attempt-{idx}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "transcript.md").write_text(transcript_md, encoding="utf-8")

    for idx, attempt_checks in enumerate(check_history, start=1):
        attempt_dir = run_dir / f"attempt-{idx}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        for chk_idx, chk_res in enumerate(attempt_checks, start=1):
            slug = _slugify(chk_res.name)
            log_file = attempt_dir / f"check-{chk_idx}-{slug}.log"
            log_file.write_text(chk_res.raw_output, encoding="utf-8")

    run_meta = {
        "run_id": run_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "stage": config.active_stage,
        "agent": config.agent.name,
        "model": config.agent.model,
        "mode": config.agent.mode,
        "read_only": config.agent.read_only,
        "persona": config.agent.persona,
        "skills": config.agent.skills,
        "succeeded": succeeded,
        "attempts_used": attempts_used,
        "max_attempts": config.retry.max_attempts,
        "duration_seconds": round(total_duration, 3),
        "failure_reason": failure_reason,
    }
    (run_dir / "run.json").write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
    return run_dir


async def run_factory(
    config: FactoryConfig,
    workspace: Path,
    *,
    issue_number: int | None = None,
    display: FactoryDisplay | None = None,
    verbose: bool = False,
    export_transcript: Path | None = None,
) -> FactoryRunOutcome:
    """Execute the full acp-factory workflow asynchronously."""
    ui = display or FactoryDisplay()
    start_time = time.monotonic()
    run_id = f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    if not config.task.strip():
        raise ValueError(
            "No task specified. Provide a task via configuration or the CLI (--task / argument)."
        )

    wt_manager = WorktreeManager(
        workspace,
        enabled=config.worktree,
        issue_number=issue_number if config.worktree else None,
        keep_on_failure=config.retry.keep_worktree_on_failure,
        keep_on_success=config.retry.keep_worktree_on_success,
    )

    succeeded = False
    attempts_used = 0
    last_response_text = ""
    failure_reason: str | None = None
    check_history: list[list[CheckResult]] = []
    setup_logs: list[str] = []
    turn_transcripts: list[str] = []
    preserved_wt: Path | None = None

    active_cwd = await wt_manager.create()
    ui.show_run_header(config, active_cwd, worktree_branch=wt_manager.branch_name)

    try:
        # 1. Setup Phase
        if config.setup:
            ui.show_phase_banner("Setup Phase", style="blue")
            for setup_cmd in config.setup:
                rc, output, duration = await run_shell_command(
                    setup_cmd, active_cwd, timeout=config.agent.timeout
                )
                setup_logs.append(f"$ {setup_cmd} (exit {rc})\n{output}")
                passed = rc == 0
                ui.show_setup_step(
                    setup_cmd, passed=passed, duration=duration, output=output
                )
                if not passed:
                    failure_reason = f"Setup command failed (exit {rc}): {setup_cmd}"
                    total_duration = max(0.0, time.monotonic() - start_time)
                    preserved_wt = await wt_manager.cleanup(succeeded=False)
                    run_dir = _persist_run_artifacts(
                        workspace,
                        run_id,
                        config,
                        succeeded=False,
                        attempts_used=0,
                        total_duration=total_duration,
                        setup_logs=setup_logs,
                        turn_transcripts=[],
                        check_history=[],
                        failure_reason=failure_reason,
                    )
                    ui.show_final_summary(
                        succeeded=False,
                        attempts_used=0,
                        max_attempts=config.retry.max_attempts,
                        total_duration=total_duration,
                        kept_worktree_path=preserved_wt,
                        failure_reason=failure_reason,
                    )
                    return FactoryRunOutcome(
                        succeeded=False,
                        exit_code=1,
                        attempts_used=0,
                        total_duration_seconds=total_duration,
                        run_dir=run_dir,
                        preserved_worktree=preserved_wt,
                        failure_reason=failure_reason,
                    )

        # 2. Agent Worker & Check Feedback Loop
        async with FactoryACPClient(
            workspace=active_cwd,
            agent_config=config.agent,
            config_dir=config.config_dir,
            verbose=verbose,
        ) as acp_client:
            prompt = build_initial_prompt(config.task, config.checks)

            for attempt in range(1, config.retry.max_attempts + 1):
                attempts_used = attempt
                ui.show_attempt_banner(attempt, config.retry.max_attempts)

                turn_res = await acp_client.send_prompt(
                    prompt, timeout=config.agent.timeout
                )
                last_response_text = turn_res.summary.response_text

                if not config.checks:
                    succeeded = True
                    break

                ui.show_phase_banner(
                    f"Validation Checks (Attempt {attempt}/{config.retry.max_attempts})",
                    style="cyan",
                )
                all_passed, results = await run_checks_sequential(
                    config.checks,
                    active_cwd,
                    acp_client=acp_client,
                    config_dir=config.config_dir,
                    max_output_lines=config.retry.max_output_lines,
                    on_check_complete=ui.show_check_result,
                )
                check_history.append(results)

                if all_passed:
                    succeeded = True
                    break

                failed = results[-1]
                if failed.status == "stop":
                    failure_reason = (
                        f"Check '{failed.name}' returned unrecoverable STOP status "
                        f"(exit {failed.exit_code}) on attempt {attempt}"
                    )
                    break

                failure_reason = f"Check '{failed.name}' failed on attempt {attempt}"
                if attempt < config.retry.max_attempts:
                    prompt = build_retry_prompt(
                        config.task,
                        attempt=attempt + 1,
                        max_attempts=config.retry.max_attempts,
                        check_results=results,
                    )

            turn_transcripts = [
                turn.transcript_markdown for turn in acp_client.tracker.history
            ]
            if export_transcript and turn_transcripts:
                export_path = export_transcript.resolve()
                export_path.parent.mkdir(parents=True, exist_ok=True)
                sections = [
                    f"# Factory Run Transcript (Attempt {i})\n\n{md}"
                    for i, md in enumerate(turn_transcripts, start=1)
                ]
                export_path.write_text("\n\n---\n\n".join(sections), encoding="utf-8")

    except TimeoutError as exc:
        failure_reason = (
            f"ACP agent turn timed out after {config.agent.timeout}s ({exc})"
        )
    except Exception as exc:
        failure_reason = f"Unhandled error: {exc}"
    finally:
        preserved_wt = await wt_manager.cleanup(succeeded=succeeded)

    total_duration = max(0.0, time.monotonic() - start_time)
    run_dir = _persist_run_artifacts(
        workspace,
        run_id,
        config,
        succeeded=succeeded,
        attempts_used=attempts_used,
        total_duration=total_duration,
        setup_logs=setup_logs,
        turn_transcripts=turn_transcripts,
        check_history=check_history,
        failure_reason=None if succeeded else failure_reason,
    )
    ui.show_final_summary(
        succeeded=succeeded,
        attempts_used=attempts_used,
        max_attempts=config.retry.max_attempts,
        total_duration=total_duration,
        kept_worktree_path=preserved_wt,
        failure_reason=None if succeeded else failure_reason,
    )
    return FactoryRunOutcome(
        succeeded=succeeded,
        exit_code=0 if succeeded else 1,
        attempts_used=attempts_used,
        total_duration_seconds=total_duration,
        last_response_text=last_response_text,
        run_dir=run_dir,
        check_history=check_history,
        preserved_worktree=preserved_wt,
        failure_reason=None if succeeded else failure_reason,
    )


async def run_issue_stage(
    workspace: Path,
    issue_number: int,
    *,
    stage_override: str | None = None,
    config_file: Path | None = None,
    dry_run: bool | None = None,
    display: FactoryDisplay | None = None,
    verbose: bool = False,
) -> FactoryRunOutcome:
    """Execute the active GitHub label stage for a specific issue and transition its label."""
    ui = display or FactoryDisplay()
    resolved_workspace = workspace.resolve()
    base_cfg = FactoryConfig.discover(resolved_workspace, config_file)
    effective_dry_run = base_cfg.dry_run if dry_run is None else dry_run

    task: TaskContext = await fetch_issue(issue_number, cwd=resolved_workspace)
    stage = stage_override or task.current_stage
    if not stage:
        raise ValueError(
            f"Issue #{issue_number} does not have an actionable factory stage label "
            f"(current labels: {task.labels}). Expected one of: "
            "factory:triage, factory:spec, factory:build, factory:review, factory:done."
        )

    # Stage 5: `done` is an orchestrator cleanup stage (no automatic PR merge)
    if stage == "done":
        start_time = time.monotonic()
        ui.show_phase_banner(
            f"Issue #{issue_number} — Done Stage (Cleanup)", style="green"
        )
        verdict = StageVerdict(
            stage="done",
            status="completed",
            target_label=LABEL_COMPLETED,
            updated_factory_block=task.factory_block,
            summary=f"Cleaned up worktree for Issue #{issue_number} and set {LABEL_COMPLETED}.",
        )
        if effective_dry_run:
            plan = build_dry_run_gh_plan(task, verdict, workspace=resolved_workspace)
            ui.show_dry_run_plan(plan)
            duration = max(0.0, time.monotonic() - start_time)
            return FactoryRunOutcome(
                succeeded=True,
                exit_code=0,
                attempts_used=1,
                total_duration_seconds=duration,
                stage_verdict=verdict,
                dry_run_plan=plan,
            )

        await WorktreeManager.remove_issue_worktree(resolved_workspace, issue_number)
        await transition_issue_label(task, LABEL_COMPLETED, cwd=resolved_workspace)
        duration = max(0.0, time.monotonic() - start_time)
        ui.show_final_summary(
            succeeded=True,
            attempts_used=1,
            max_attempts=1,
            total_duration=duration,
        )
        return FactoryRunOutcome(
            succeeded=True,
            exit_code=0,
            attempts_used=1,
            total_duration_seconds=duration,
            stage_verdict=verdict,
        )

    task_context_md = format_task_prompt_context(task)

    # Use persistent issue worktree for `build` and `review` stages
    use_issue_worktree = stage in ("build", "review")
    if stage in base_cfg.stages:
        cfg = base_cfg.with_overrides(
            stage=stage,
            workspace=resolved_workspace,
            task=task_context_md,
            worktree=use_issue_worktree,
            dry_run=effective_dry_run,
        )
    else:
        cfg = base_cfg.with_overrides(
            workspace=resolved_workspace,
            task=task_context_md,
            read_only=(stage in ("triage", "spec", "review")),
            worktree=use_issue_worktree,
            dry_run=effective_dry_run,
        )
        cfg.active_stage = stage

    outcome = await run_factory(
        cfg,
        resolved_workspace,
        issue_number=issue_number if use_issue_worktree else None,
        display=ui,
        verbose=verbose,
    )

    if not outcome.succeeded:
        if effective_dry_run:
            fail_verdict = StageVerdict(
                stage=stage,
                status="failed",
                target_label=LABEL_NEEDS_HUMAN,
                updated_factory_block=task.factory_block,
                summary=outcome.failure_reason or "Stage failed",
            )
            plan = build_dry_run_gh_plan(
                task,
                fail_verdict,
                workspace=resolved_workspace,
                run_dir=outcome.run_dir,
            )
            outcome.dry_run_plan = plan
            ui.show_dry_run_plan(plan)
        else:
            await transition_issue_label(
                task, LABEL_NEEDS_HUMAN, cwd=resolved_workspace
            )
        return outcome

    verdict = parse_stage_verdict(
        stage,
        outcome.last_response_text,
        current_factory_block=task.factory_block,
    )
    outcome.stage_verdict = verdict

    if effective_dry_run:
        plan = build_dry_run_gh_plan(
            task,
            verdict,
            workspace=resolved_workspace,
            run_dir=outcome.run_dir,
        )
        outcome.dry_run_plan = plan
        ui.show_dry_run_plan(plan)
        return outcome

    if verdict.updated_factory_block and (
        verdict.updated_factory_block != task.factory_block
    ):
        await update_issue_factory_block(
            task,
            verdict.updated_factory_block,
            cwd=resolved_workspace,
        )

    if verdict.publish_pr:
        wt_cwd = outcome.preserved_worktree or WorktreeManager.issue_worktree_path(
            resolved_workspace, issue_number
        )
        pr_cwd = wt_cwd if wt_cwd.exists() else resolved_workspace
        pr_body = (
            f"## Summary\n{verdict.summary}\n\n"
            f"## Specification\n{task.factory_block or task.original_body}"
        )
        await push_and_create_or_update_pr(task, pr_body=pr_body, cwd=pr_cwd)

    await transition_issue_label(task, verdict.target_label, cwd=resolved_workspace)
    return outcome
