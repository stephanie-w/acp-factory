"""Top-level factory run orchestration and its outcome type."""

from __future__ import annotations

import logging
import tempfile
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..acp.client import FactoryACPClient
from ..acp.events import UsageTotals
from ..config import FactoryConfig
from ..github import DryRunPlan
from ..ui.display import FactoryDisplay
from ..verdict import StageVerdict
from .artifacts import RunArtifactStore, RunMetadata
from .checks import CheckResult, run_checks_sequential, run_shell_command
from .prompts import (
    EMPTY_OUTPUT_FAILURE_REASON,
    build_initial_prompt,
    build_retry_prompt,
    check_turn_cap,
    check_usage_cap,
)
from .worktree import WorktreeError, WorktreeManager, get_head_sha

logger = logging.getLogger(__name__)


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
    usage: UsageTotals = field(default_factory=UsageTotals)
    run_usage: UsageTotals = field(default_factory=UsageTotals)
    budget_breached: bool = False
    report_path: Path | None = None
    build_notes_path: Path | None = None


async def _capture_head_sha(cwd: Path) -> str | None:
    """Return `HEAD` for `cwd`, or None when the directory is not a usable repo."""
    try:
        return await get_head_sha(cwd)
    except WorktreeError as exc:
        logger.debug("Could not capture HEAD SHA for run artifacts: %s", exc)
        return None


async def run_factory(
    config: FactoryConfig,
    workspace: Path,
    *,
    issue_number: int | None = None,
    issue_title: str | None = None,
    repo_slug: str | None = None,
    branch: str | None = None,
    display: FactoryDisplay | None = None,
    verbose: bool = False,
    export_transcript: Path | None = None,
    base_ref: str | None = None,
    initial_usage: UsageTotals | None = None,
) -> FactoryRunOutcome:
    """Execute the full acp-factory workflow asynchronously."""
    ui = display or FactoryDisplay()
    start_time = time.monotonic()
    run_id = f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    if not config.task.strip():
        raise ValueError(
            "No task specified. Provide a task via configuration or the CLI (--task / argument)."
        )

    artifacts = RunArtifactStore(workspace, config)

    wt_manager = WorktreeManager(
        workspace,
        enabled=config.worktree,
        issue_number=issue_number if config.worktree else None,
        keep_on_failure=config.retry.keep_worktree_on_failure,
        keep_on_success=config.retry.keep_worktree_on_success,
    )

    succeeded = False
    attempts_used = 0
    run_turns_used = 0
    last_response_text = ""
    failure_reason: str | None = None
    budget_breached = False
    run_usage = UsageTotals()
    cumulative_usage = (
        replace(initial_usage) if initial_usage is not None else UsageTotals()
    )
    check_history: list[list[CheckResult]] = []
    setup_logs: list[str] = []
    turn_transcripts: list[str] = []
    turn_summaries: list[dict[str, Any]] = []
    turn_condensed: list[dict[str, Any]] = []
    preserved_wt: Path | None = None
    session_id: str | None = None
    session_title: str | None = None
    head_sha: str | None = None

    active_cwd = await wt_manager.create(base_ref=base_ref)
    # Spill files for truncated check output outlive retry attempts but are removed
    # with the run so agents can inspect omitted output without re-running checks.
    spill_tmp_dir = tempfile.TemporaryDirectory(prefix=f"acp-factory-spill-{run_id}-")
    spill_dir = Path(spill_tmp_dir.name)
    ui.show_run_header(
        config,
        active_cwd,
        worktree_branch=wt_manager.branch_name,
        repo_slug=repo_slug,
        issue_number=issue_number,
        issue_title=issue_title,
    )

    try:
        # 1. Setup Phase
        if config.setup:
            ui.show_phase_banner("Setup Phase", style="blue")
            for setup_cmd in config.setup:
                rc, output, duration = await run_shell_command(
                    setup_cmd,
                    active_cwd,
                    timeout=config.agent.timeout,
                    extra_env=config.env or None,
                    strip_env=config.strip_env or None,
                    keep_env=config.keep_env or None,
                )
                setup_logs.append(f"$ {setup_cmd} (exit {rc})\n{output}")
                passed = rc == 0
                ui.show_setup_step(
                    setup_cmd, passed=passed, duration=duration, output=output
                )
                if not passed:
                    failure_reason = f"Setup command failed (exit {rc}): {setup_cmd}"
                    total_duration = max(0.0, time.monotonic() - start_time)
                    head_sha = await _capture_head_sha(active_cwd)
                    preserved_wt = await wt_manager.cleanup(succeeded=False)
                    run_dir = artifacts.persist(
                        run_id,
                        succeeded=False,
                        attempts_used=0,
                        total_duration=total_duration,
                        setup_logs=setup_logs,
                        turn_transcripts=[],
                        check_history=[],
                        failure_reason=failure_reason,
                        usage=run_usage,
                        metadata=RunMetadata(
                            issue_number=issue_number,
                            issue_title=issue_title,
                            repo_slug=repo_slug,
                            branch=branch or wt_manager.branch_name,
                            base_ref=base_ref,
                            session_id=session_id,
                            head_sha=head_sha,
                        ),
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
                        usage=cumulative_usage,
                        run_usage=run_usage,
                    )

        # 2. Agent Worker & Check Feedback Loop
        resolved_res_dirs = config.resolved_resource_dirs
        resolved_res_dir = resolved_res_dirs[0] if resolved_res_dirs else None
        effective_discover = (
            config.agent.discover_apm_resources
            if config.agent.discover_apm_resources is not None
            else config.discover_apm_resources
        )
        merged_instructions = list(
            dict.fromkeys([*config.instructions, *config.agent.instructions])
        )
        is_retro = config.active_stage == "retro"
        async with FactoryACPClient(
            workspace=active_cwd,
            agent_config=config.agent,
            config_dir=config.config_dir,
            resource_dirs=resolved_res_dirs,
            instructions=merged_instructions,
            discover_apm_resources=effective_discover,
            guardrails=config.guardrails,
            runs_dir=config.resolve_runs_dir(workspace),
            deny_runs=bool(config.active_stage and not is_retro),
            allow_runs=is_retro,
            verbose=verbose,
            silent=ui.quiet,
            extra_env=config.env or None,
            strip_env=config.strip_env or None,
            keep_env=config.keep_env or None,
            thought_max_chars=config.thought_max_chars,
        ) as acp_client:
            session_id = getattr(acp_client, "session_id", None)
            prompt = build_initial_prompt(
                config.task,
                config.checks,
                read_only=config.agent.read_only,
                workspace=active_cwd,
            )

            context_tag = ""
            if issue_number is not None and config.active_stage:
                context_tag = f"#{issue_number} · {config.active_stage}"
            elif config.active_stage:
                context_tag = config.active_stage
            elif issue_number is not None:
                context_tag = f"#{issue_number}"

            for attempt in range(1, config.retry.max_attempts + 1):
                # Pre-turn budget check: halt before dispatching another turn once the
                # run's agent turn cap has been reached (no further agent turns run).
                turn_breach = check_turn_cap(run_turns_used, config.budget)
                if turn_breach is not None:
                    failure_reason = turn_breach
                    budget_breached = True
                    break

                attempts_used = attempt
                ui.show_attempt_banner(
                    attempt, config.retry.max_attempts, context=context_tag
                )

                with ui.agent_turn_status(enabled=not verbose):
                    turn_res = await acp_client.send_prompt(
                        prompt, timeout=config.agent.timeout
                    )
                last_response_text = turn_res.summary.response_text
                if ui.quiet and last_response_text.strip():
                    ui.show_response_preview(last_response_text)

                # Post-turn accumulation: fold this turn's usage into both the
                # per-run totals (persisted in run.json) and the cumulative issue
                # totals (budget enforcement + factory meta persistence).
                run_turns_used += 1
                run_usage.turns += 1
                run_usage.tokens += turn_res.summary.tokens
                run_usage.cost_usd += turn_res.summary.cost_usd
                cumulative_usage.turns += 1
                cumulative_usage.tokens += turn_res.summary.tokens
                cumulative_usage.cost_usd += turn_res.summary.cost_usd

                usage_breach = check_usage_cap(
                    run_usage.tokens, run_usage.cost_usd, config.budget
                )
                if usage_breach is not None:
                    failure_reason = usage_breach
                    budget_breached = True
                    break

                if not config.checks:
                    if not last_response_text.strip():
                        failure_reason = EMPTY_OUTPUT_FAILURE_REASON
                        logger.error(failure_reason)
                        break
                    succeeded = True
                    break

                ui.show_phase_banner(
                    f"Validation Checks (Attempt {attempt}/{config.retry.max_attempts})",
                    style="cyan",
                )
                with tempfile.TemporaryDirectory(
                    prefix=f"acp-factory-{run_id}-attempt-{attempt}-"
                ) as resp_tmp_dir:
                    resp_file = Path(resp_tmp_dir) / "response.md"
                    resp_file.write_text(last_response_text, encoding="utf-8")
                    check_env = dict(config.env)
                    check_env.update(
                        {
                            "FACTORY_RUN_ID": run_id,
                            "FACTORY_RESPONSE_FILE": str(resp_file),
                        }
                    )
                    if config.active_stage:
                        check_env["FACTORY_STAGE"] = config.active_stage
                    if resolved_res_dir is not None:
                        skills_dir = (
                            resolved_res_dir / "skills"
                            if (resolved_res_dir / "skills").is_dir()
                            else resolved_res_dir
                        )
                        check_env["FACTORY_SKILLS_DIR"] = str(skills_dir.resolve())
                        check_env["FACTORY_BUNDLED_SKILLS_DIR"] = str(
                            skills_dir.resolve()
                        )

                    all_passed, results = await run_checks_sequential(
                        config.checks,
                        active_cwd,
                        acp_client=acp_client,
                        config_dir=config.config_dir,
                        resources_dir=resolved_res_dir,
                        resource_dirs=resolved_res_dirs,
                        discover_apm_resources=effective_discover,
                        max_output_lines=config.retry.max_output_lines,
                        spill_dir=spill_dir,
                        on_check_complete=ui.show_check_result,
                        extra_env=check_env,
                        strip_env=config.strip_env or None,
                        keep_env=config.keep_env or None,
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
                        captured_response=last_response_text,
                    )

            turn_transcripts = [
                turn.transcript_markdown for turn in acp_client.tracker.history
            ]
            turn_summaries = [turn.to_dict() for turn in acp_client.tracker.history]
            turn_condensed = [
                turn.to_condensed_dict() for turn in acp_client.tracker.history
            ]
            # Resolve the title before the client context closes and the agent
            # shuts down; agents may finalize session titles after the last turn.
            title_getter = getattr(acp_client, "get_session_title", None)
            if callable(title_getter):
                session_title = await title_getter()
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
        # The worktree (and its HEAD) disappears during cleanup, so snapshot the
        # commit under review before removing it.
        head_sha = await _capture_head_sha(active_cwd)
        spill_tmp_dir.cleanup()
        preserved_wt = await wt_manager.cleanup(succeeded=succeeded)

    total_duration = max(0.0, time.monotonic() - start_time)
    run_dir = artifacts.persist(
        run_id,
        succeeded=succeeded,
        attempts_used=attempts_used,
        total_duration=total_duration,
        setup_logs=setup_logs,
        turn_transcripts=turn_transcripts,
        check_history=check_history,
        failure_reason=None if succeeded else failure_reason,
        usage=run_usage,
        turn_summaries=turn_summaries,
        turn_condensed=turn_condensed,
        last_response_text=last_response_text,
        metadata=RunMetadata(
            issue_number=issue_number,
            issue_title=issue_title,
            repo_slug=repo_slug,
            branch=branch or wt_manager.branch_name,
            base_ref=base_ref,
            session_id=session_id,
            session_title=session_title,
            head_sha=head_sha,
        ),
    )
    report_path = (
        run_dir / "report.md"
        if run_dir is not None and (run_dir / "report.md").is_file()
        else None
    )
    build_notes_path = (
        run_dir / "implementation-notes.md"
        if run_dir is not None and (run_dir / "implementation-notes.md").is_file()
        else None
    )
    ui.show_final_summary(
        succeeded=succeeded,
        attempts_used=attempts_used,
        max_attempts=config.retry.max_attempts,
        total_duration=total_duration,
        kept_worktree_path=preserved_wt,
        failure_reason=None if succeeded else failure_reason,
        report_path=report_path,
        build_notes_path=build_notes_path,
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
        usage=cumulative_usage,
        run_usage=run_usage,
        budget_breached=budget_breached,
        report_path=report_path,
        build_notes_path=build_notes_path,
    )
