"""Prompt builders and budget caps shared by the runner orchestration modules."""

from __future__ import annotations

import re
from pathlib import Path

from ..config import BudgetConfig, CheckConfig
from ..task import TaskContext, extract_factory_findings
from .checks import CheckResult

EMPTY_OUTPUT_FAILURE_REASON = "Agent produced no output (session aborted or failed)"


def check_turn_cap(turns_used: int, budget: BudgetConfig) -> str | None:
    """Return a deterministic breach message when the cumulative turn cap is reached."""
    if budget.max_turns is not None and turns_used >= budget.max_turns:
        return (
            f"Budget cap breached: turn count ({turns_used}) reached "
            f"max_turns ({budget.max_turns})"
        )
    return None


def check_usage_cap(
    total_tokens: int, total_cost_usd: float, budget: BudgetConfig
) -> str | None:
    """Return a deterministic breach message when token or cost caps are exceeded."""
    if (
        budget.max_tokens_per_run is not None
        and total_tokens > budget.max_tokens_per_run
    ):
        return (
            f"Budget cap breached: run tokens {total_tokens:,} exceeded "
            f"max_tokens_per_run ({budget.max_tokens_per_run:,})"
        )
    if budget.max_cost_usd is not None and total_cost_usd > budget.max_cost_usd:
        return (
            f"Budget cap breached: run cost ${total_cost_usd:.2f} exceeded "
            f"max_cost_usd (${budget.max_cost_usd:.2f})"
        )
    return None


def build_initial_prompt(
    task: str,
    checks: list[CheckConfig],
    *,
    read_only: bool = False,
    workspace: Path | None = None,
) -> str:
    """Construct the initial task prompt sent to the ACP worker agent."""
    lines = [
        "# Coding Task",
        task.strip(),
        "",
        "## Execution Mode: Autonomous & Non-Interactive",
        "- You are executing autonomously in an automated orchestrator with NO human in the loop to answer questions, approve plans, or confirm decisions.",
        "- Do NOT pause to ask clarifying questions, request approval, or propose a strategy and wait for confirmation.",
        "- If repository instructions (such as `AGENTS.md` or `CLAUDE.md`) specify an interactive confirmation workflow (e.g. 'wait for user approval before proceeding'), that waiting requirement is strictly superseded: proceed directly to execute and verify the task to completion.",
        "- Security policy strictly blocks dangerous commands (e.g. sudo, rm -rf, git push) and paths outside this workspace. If an action is blocked by a guardrail (`[GUARDRAIL DENIED]`), do not loop trying alternative ways to bypass it; adapt your strategy using workspace-local tools or document the blocker in your final summary.",
    ]
    if workspace is not None:
        resolved_ws = workspace.resolve()
        lines.append(
            f"- Active workspace: `{resolved_ws}`. Use workspace-relative paths (e.g. `src/...`, `tests/...`) for all file reads, searches, and edits—do not reference paths in a parent repository checkout."
        )
    if read_only:
        lines.append(
            "- Complete your analysis and output your full structured report directly. If any ambiguity requires human decisions, document it within your report (e.g. under `## Open Questions` with `STATUS: NEEDS_HUMAN`), but never end your turn waiting for interactive chat confirmation."
        )
    else:
        lines.append(
            "- Complete all necessary research, file modifications, and verification autonomously within your turn."
        )
    final_clause = (
        "output strictly the required stage sections and terminal `STATUS:` / `VERDICT:` line."
        if read_only
        else "output strictly the required stage sections."
    )
    lines.extend(
        [
            "",
            "## Tool & Token Discipline (First Priority)",
            "- Token economy is your first priority: you are operating under a strict per-run token budget (exceeding it aborts the run). Before every tool call, ask whether current evidence already answers the question.",
            "- Locate first, read selectively: find targets with `rg -n`, `fd`, `jq`, or `git diff --stat`, then use targeted, windowed file reads (`<=150` lines). Never read full files over ~200 lines or re-read lines already read in this turn.",
            "- Quiet & query at source: use quiet/no-color flags (`-q`, `--no-color`) and project structured outputs (`jq`, `head`/`tail`) while retaining exact failure diagnostics.",
            "- Batch parallel tool calls: run independent file reads, searches, and git checks in a single turn instead of sequential 1-by-1 calls.",
            "- Do not narrate tool calls or announce next steps (e.g. 'Let me...', 'I will...', 'Now let's...') between tool invocations; invoke tools silently.",
            f"- Telegraphic output: omit pleasantries, preambles, filler adverbs, and prompt restatements; {final_clause}",
            "",
        ]
    )

    if checks:
        lines.append("## Verification Checks")
        if read_only:
            lines.append(
                "After your turn finishes, the orchestrator automatically writes your response text "
                "to `$FACTORY_RESPONSE_FILE` (do not write this file yourself) and runs these checks:"
            )
        else:
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
            "Ensure your output is complete, clean, and satisfies all checks."
            if read_only
            else "Ensure your implementation is complete, clean, and satisfies all checks."
        )
    return "\n".join(lines)


_RESPONSE_PREVIEW_CHARS = 4000


def _format_response_excerpt(
    response_text: str,
    max_chars: int = _RESPONSE_PREVIEW_CHARS,
) -> str:
    """Return `response_text` bounded to `max_chars` (preserving head and tail)."""
    stripped = response_text.strip()
    if len(stripped) <= max_chars:
        return stripped
    half = max_chars // 2
    omitted = len(stripped) - max_chars
    return (
        f"{stripped[:half]}\n\n... [{omitted} chars omitted] ...\n\n{stripped[-half:]}"
    )


def build_retry_prompt(
    task: str,
    attempt: int,
    max_attempts: int,
    check_results: list[CheckResult],
    *,
    captured_response: str | None = None,
) -> str:
    """Construct a structured feedback prompt when a check fails."""
    failed_check = check_results[-1]
    passed_checks = [r for r in check_results[:-1] if r.passed]
    is_response_check = (
        failed_check.check_type == "cmd"
        and "FACTORY_RESPONSE_FILE" in failed_check.command_or_prompt
    )

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

    if is_response_check and captured_response is not None:
        lines.append("### Captured Response (`$FACTORY_RESPONSE_FILE`)")
        lines.append(
            "This exact text was captured from your previous final response and passed to the validator:"
        )
        lines.append("````markdown")
        lines.append(_format_response_excerpt(captured_response) or "(empty)")
        lines.append("````")
        lines.append("")

    lines.append("## Instructions")
    if is_response_check:
        lines.append(
            "This check validated your previous final response text (`$FACTORY_RESPONSE_FILE`). "
            "Fix the formatting or content issue described in the error output above and output "
            "the complete, corrected markdown report directly as your final response."
        )
        lines.append(
            "Do NOT inspect orchestrator/runner code or try to locate `$FACTORY_RESPONSE_FILE` "
            "on disk; the file is temporary and populated verbatim from your final assistant message."
        )
        lines.append(
            "Remember: You are executing autonomously with no human operator in the loop. "
            "Do NOT end your turn asking questions or proposing a strategy for approval."
        )
    else:
        lines.append(
            "Analyze the failure output above, fix the root cause in the workspace without "
            "breaking prior work or previously passing checks, and verify your changes."
        )
        lines.append(
            "Remember: You are executing autonomously with no human operator in the loop. "
            "Do NOT end your turn asking questions or proposing a strategy for approval; "
            "apply all changes directly to the workspace."
        )
    retry_final_clause = (
        "and output strictly the required sections and terminal `STATUS:` / `VERDICT:` line."
        if is_response_check
        else "and output strictly the required stage sections."
    )
    lines.append(
        "Do not narrate tool calls or announce next steps; batch independent tool calls in parallel, "
        f"omit pleasantries and preambles, {retry_final_clause}"
    )
    return "\n".join(lines)


_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)


def build_delta_review_brief(
    task: TaskContext,
    *,
    last_reviewed_sha: str,
    review_round: int,
) -> str:
    """Construct a Round 2+ delta-focused review brief to prevent reviewer goalpost-moving."""
    prior_findings = extract_factory_findings(task.factory_block)
    safe_sha = (
        last_reviewed_sha if _SHA_RE.match(last_reviewed_sha) else "<invalid-sha>"
    )
    lines = [
        "# Delta Review Brief (Round 2+)",
        f"- **Prior Review Round:** {review_round}",
        f"- **Prior Reviewed SHA:** `{safe_sha}`",
        f"- **Delta Command:** audit `git diff {safe_sha}..HEAD` (plus the full diff against base for context).",
        "- **Rules:** verify every prior finding below was resolved and audit all code touched in "
        f"`{safe_sha}..HEAD` for regressions or security issues; do NOT invent new findings on untouched lines.",
    ]
    if prior_findings:
        lines.extend(["", "## Prior Review Findings", prior_findings])
    return "\n".join(lines)


_SPEC_MARKERS = (
    "## Problem",
    "# Specification",
    "## Acceptance Criteria",
    "## Behavior",
)


def has_existing_spec(task: TaskContext) -> bool:
    """Check if the task's factory block contains an existing specification."""
    block = task.factory_block
    if not block:
        return False
    return any(marker in block for marker in _SPEC_MARKERS)


def build_spec_revision_brief(task: TaskContext) -> str:
    """Construct a delta-focused spec revision brief when an existing specification is present."""
    has_comments = bool(task.comments)
    lines = [
        "# Spec Revision Brief (Incremental Update)",
        "- **Mode:** SPEC_REVISION (an existing specification is already drafted in the factory block above).",
        "- **Goal:** Surgically update the existing specification"
        + (" to incorporate the latest human comments." if has_comments else "."),
        "- **Rules:**",
        "  1. Do NOT re-explore the codebase from scratch or restart the design from zero.",
        "  2. Preserve existing problem statement, architecture, behaviors, and valid `AC-n` rows.",
        "  3. Fold human answers and decisions from comments into `## Decisions`.",
        "  4. Add any newly requested criteria as new `AC-n` rows using the next available numbers (never renumber existing `AC-n` IDs).",
        "  5. Update `## Scope`, `## Files to Touch`, or `## Out of Scope` only as requested.",
        "  6. Output the complete revised Markdown specification starting with `# Specification` and ending with the `STATUS:` line.",
    ]
    return "\n".join(lines)
