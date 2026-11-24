"""Stage-verdict state machine: parse an agent response into the next label transition."""

from __future__ import annotations

import re

from pydantic import BaseModel

from .config import DEFAULT_PR_FLOW, FlowConfig, TransitionAction
from .task import (
    LABEL_COMPLETED,
    MAX_REVIEW_ROUNDS,
    _mask_code_blocks,
    parse_factory_meta,
    parse_review_round,
    replace_factory_findings,
    set_factory_meta,
    strip_factory_findings,
)


class StageVerdict(BaseModel):
    """Structured outcome parsed from a stage agent's final response."""

    stage: str
    status: str
    target_label: str
    updated_factory_block: str
    summary: str = ""
    action: TransitionAction = "none"
    publish_pr: bool = False
    review_report: str = ""


_STATUS_LINE_RE = re.compile(
    r"^\s*(?:\*\*?)?STATUS(?:\*\*?)?\s*:\s*([A-Z_]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_VERDICT_LINE_RE = re.compile(
    r"^\s*(?:\*\*?)?VERDICT(?:\*\*?)?\s*:\s*([A-Z_]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_OPEN_QUESTIONS_RE = re.compile(
    r"^##+\s+Open\s+Questions\b",
    re.IGNORECASE | re.MULTILINE,
)

_HEADING_START_RE = re.compile(r"^#{1,3}\s+\S", re.MULTILINE)


def _find_terminal_status_or_verdict(text: str) -> tuple[re.Match[str] | None, str]:
    """Find the trailing non-fenced STATUS: or VERDICT: match.

    Returns (match, raw_token_upper). Matches inside markdown code blocks are ignored.
    If multiple matches exist outside code fences, the last one wins.
    """
    masked = _mask_code_blocks(text)
    all_matches: list[tuple[re.Match[str], str]] = []
    for m in _STATUS_LINE_RE.finditer(masked):
        all_matches.append((m, m.group(1).upper()))
    for m in _VERDICT_LINE_RE.finditer(masked):
        all_matches.append((m, m.group(1).upper()))

    if not all_matches:
        return None, ""

    all_matches.sort(key=lambda item: item[0].start())
    return all_matches[-1]


def _clean_markdown_stage_body(text: str) -> str:
    """Strip conversational preamble before the first heading and any postscript after STATUS/VERDICT."""
    trimmed = text
    terminal_match, _ = _find_terminal_status_or_verdict(trimmed)
    if terminal_match:
        trimmed = trimmed[: terminal_match.start()]

    heading_match = _HEADING_START_RE.search(trimmed)
    if heading_match:
        trimmed = trimmed[heading_match.start() :]

    # Remove trailing horizontal rules left behind before STATUS
    trimmed = re.sub(r"(?:\n\s*---+\s*)+\Z", "", trimmed.strip())
    return trimmed.strip()


def _stage_slug_from_label(label: str) -> str:
    """Derive the metadata stage name from a `factory:<stage>` label."""
    return label.removeprefix("factory:")


def parse_stage_verdict(
    stage: str,
    response_text: str,
    current_factory_block: str = "",
    *,
    reviewed_sha: str | None = None,
    max_review_rounds: int | None = None,
    flow: FlowConfig | None = None,
) -> StageVerdict:
    """Determine the next GitHub label, action, and updated Issue Body block for a completed stage."""
    cleaned_body = _clean_markdown_stage_body(response_text)
    _, raw_token = _find_terminal_status_or_verdict(response_text)

    active_flow = flow or DEFAULT_PR_FLOW
    stage_transitions = active_flow.transitions.get(stage)
    if not stage_transitions:
        return StageVerdict(
            stage=stage,
            status="completed",
            target_label=LABEL_COMPLETED,
            updated_factory_block=current_factory_block,
            summary=cleaned_body,
        )

    status_key = raw_token
    if stage in active_flow.open_questions_stages:
        has_open_questions = bool(
            _OPEN_QUESTIONS_RE.search(_mask_code_blocks(cleaned_body))
        )
        if (
            has_open_questions
            and status_key not in ("SPEC_READY", "READY")
            and "NEEDS_HUMAN" in stage_transitions
        ):
            status_key = "NEEDS_HUMAN"

    transition = stage_transitions.get(status_key) or stage_transitions.get("DEFAULT")
    if transition is None:
        return StageVerdict(
            stage=stage,
            status=status_key.lower() if status_key else "completed",
            target_label=LABEL_COMPLETED,
            updated_factory_block=current_factory_block,
            summary=cleaned_body,
        )

    display_token = status_key or "OK"
    review_report = cleaned_body if transition.record_review_report else ""
    summary = (
        transition.summary.format(token=display_token)
        if transition.summary
        else (
            cleaned_body
            or (
                "Build & verification checks passed -> ready for review"
                if stage == "build"
                else ""
            )
        )
    )
    status = transition.status or (status_key.lower() if status_key else "completed")

    if transition.block_mode == "preserve":
        updated_block = (
            current_factory_block.strip() if stage != "done" else current_factory_block
        )
        return StageVerdict(
            stage=stage,
            status=status,
            target_label=transition.label,
            updated_factory_block=updated_block,
            summary=summary,
            action=transition.action,
            publish_pr=transition.action == "publish_pr",
            review_report=review_report,
        )

    if transition.block_mode == "replace":
        return StageVerdict(
            stage=stage,
            status=status,
            target_label=transition.label,
            updated_factory_block=cleaned_body or current_factory_block,
            summary=summary,
            action=transition.action,
            publish_pr=transition.action == "publish_pr",
            review_report=review_report,
        )

    if transition.block_mode == "append_section":
        heading = (
            transition.section_title.format(token=display_token)
            if transition.section_title
            else f"## {stage.title()} Update"
        )
        if transition.replace_existing:
            updated_block = f"{heading}\n\n{cleaned_body}"
        else:
            base = strip_factory_findings(current_factory_block)
            updated_block = f"{base}\n\n{heading}\n{cleaned_body}".strip()
            if transition.record_review_report:
                meta = parse_factory_meta(current_factory_block)
                review_round = parse_review_round(meta)
                updated_block = set_factory_meta(
                    updated_block,
                    stage=_stage_slug_from_label(transition.label),
                    review_round=str(review_round),
                    last_reviewed_sha=reviewed_sha or meta.get("last_reviewed_sha", ""),
                )
        return StageVerdict(
            stage=stage,
            status=status,
            target_label=transition.label,
            updated_factory_block=updated_block,
            summary=summary,
            action=transition.action,
            publish_pr=transition.action == "publish_pr",
            review_report=review_report,
        )

    if transition.block_mode == "clear_findings":
        meta = parse_factory_meta(current_factory_block)
        review_round = parse_review_round(meta)
        clean_block = strip_factory_findings(current_factory_block)
        if reviewed_sha:
            clean_block = set_factory_meta(
                clean_block,
                stage=_stage_slug_from_label(transition.label),
                review_round=str(review_round),
                last_reviewed_sha=reviewed_sha,
            )
        return StageVerdict(
            stage=stage,
            status=status,
            target_label=transition.label,
            updated_factory_block=clean_block,
            summary=summary,
            action=transition.action,
            publish_pr=transition.action == "publish_pr",
            review_report=review_report,
        )

    # block_mode == "findings": replace findings in-place, increment round, and trip circuit breaker
    meta = parse_factory_meta(current_factory_block)
    review_round = parse_review_round(meta)
    effective_cap = (
        max_review_rounds
        if max_review_rounds is not None
        else (transition.max_rounds or MAX_REVIEW_ROUNDS)
    )
    new_round = review_round + 1
    findings_block = replace_factory_findings(
        current_factory_block,
        cleaned_body,
        round_num=new_round,
        max_rounds=effective_cap,
    )
    if new_round >= effective_cap:
        escalation = (
            "\n\n## Review Loop Escalation\n"
            f"The {effective_cap}-round review limit was reached with unresolved findings. "
            "A human must decide whether to accept, fix manually, or close."
        )
        block = set_factory_meta(
            findings_block + escalation,
            stage=_stage_slug_from_label(transition.on_max_rounds_label),
            review_round=str(new_round),
            last_reviewed_sha=reviewed_sha or "",
        )
        return StageVerdict(
            stage=stage,
            status="needs_human",
            target_label=transition.on_max_rounds_label,
            updated_factory_block=block,
            summary=(
                f"Review loop hit the {effective_cap}-round circuit breaker -> "
                "waiting for human decision"
            ),
            action=transition.action,
            publish_pr=False,
            review_report=review_report,
        )

    block = set_factory_meta(
        findings_block,
        stage=_stage_slug_from_label(transition.label),
        review_round=str(new_round),
        last_reviewed_sha=reviewed_sha or "",
    )
    return StageVerdict(
        stage=stage,
        status=status,
        target_label=transition.label,
        updated_factory_block=block,
        summary=summary,
        action=transition.action,
        publish_pr=transition.action == "publish_pr",
        review_report=review_report,
    )
