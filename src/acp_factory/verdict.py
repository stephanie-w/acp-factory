"""Stage-verdict state machine: parse an agent response into the next label transition."""

from __future__ import annotations

import re

from pydantic import BaseModel

from .task import (
    LABEL_BUILD,
    LABEL_COMPLETED,
    LABEL_NEEDS_HUMAN,
    LABEL_PR_READY,
    LABEL_REVIEW,
    LABEL_SPEC,
    LABEL_SPEC_READY,
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


def parse_stage_verdict(
    stage: str,
    response_text: str,
    current_factory_block: str = "",
    *,
    reviewed_sha: str | None = None,
    max_review_rounds: int = MAX_REVIEW_ROUNDS,
) -> StageVerdict:
    """Determine the next GitHub label and updated Issue Body factory block for a completed stage."""
    cleaned_body = _clean_markdown_stage_body(response_text)
    _, raw_token = _find_terminal_status_or_verdict(response_text)

    if stage == "triage":
        if raw_token in ("DUPLICATE", "TOO_BIG", "NEEDS_HUMAN", "NEEDS_INPUT", "STOP"):
            return StageVerdict(
                stage="triage",
                status=raw_token.lower(),
                target_label=LABEL_NEEDS_HUMAN,
                updated_factory_block=f"## Triage Report (`{raw_token}`)\n\n{cleaned_body}",
                summary=f"Triage requires human attention ({raw_token})",
            )
        if raw_token == "TRIVIAL":  # noqa: S105
            return StageVerdict(
                stage="triage",
                status="trivial",
                target_label=LABEL_SPEC_READY,
                updated_factory_block=f"## Triage Report (`TRIVIAL`)\n\n{cleaned_body}",
                summary=(
                    "Triage fast-tracked (TRIVIAL) -> waiting for Human Gate 1 "
                    "approval (factory:spec-ready)"
                ),
            )
        return StageVerdict(
            stage="triage",
            status="ok",
            target_label=LABEL_SPEC,
            updated_factory_block=f"## Triage Report (`OK`)\n\n{cleaned_body}",
            summary="Triage passed -> ready for spec",
        )

    if stage == "spec":
        has_open_questions = bool(
            _OPEN_QUESTIONS_RE.search(_mask_code_blocks(cleaned_body))
        )
        if raw_token in ("NEEDS_HUMAN", "NEEDS_INPUT", "OPEN_QUESTIONS") or (
            has_open_questions and raw_token != "SPEC_READY"  # noqa: S105
        ):
            return StageVerdict(
                stage="spec",
                status="needs_human",
                target_label=LABEL_NEEDS_HUMAN,
                updated_factory_block=cleaned_body or current_factory_block,
                summary="Spec has open questions -> waiting for human answers in comments",
            )
        return StageVerdict(
            stage="spec",
            status="spec_ready",
            target_label=LABEL_SPEC_READY,
            updated_factory_block=cleaned_body or current_factory_block,
            summary="Spec complete -> waiting for Human Gate 1 approval (factory:spec-ready)",
        )

    if stage == "build":
        # Preserve the approved spec in the Issue Body and record build completion status
        spec_base = current_factory_block.strip()
        return StageVerdict(
            stage="build",
            status="built",
            target_label=LABEL_REVIEW,
            updated_factory_block=spec_base,
            summary=cleaned_body
            or "Build & verification checks passed -> ready for review",
        )

    if stage == "review":
        meta = parse_factory_meta(current_factory_block)
        review_round = parse_review_round(meta)

        if raw_token in ("ACCEPTED", "PASS"):
            clean_block = strip_factory_findings(current_factory_block)
            if reviewed_sha:
                clean_block = set_factory_meta(
                    clean_block,
                    stage="pr-ready",
                    review_round=str(review_round),
                    last_reviewed_sha=reviewed_sha,
                )
            return StageVerdict(
                stage="review",
                status="accepted",
                target_label=LABEL_PR_READY,
                updated_factory_block=clean_block,
                summary="Review accepted -> PR ready for Human Gate 2",
                publish_pr=True,
                review_report=cleaned_body,
            )

        if raw_token in ("HUMAN_DECISION_NEEDED", "NEEDS_HUMAN", "STOP"):
            merged_block = f"{strip_factory_findings(current_factory_block)}\n\n## Review Escalation\n{cleaned_body}".strip()
            merged_block = set_factory_meta(
                merged_block,
                stage="needs-human",
                review_round=str(review_round),
                last_reviewed_sha=reviewed_sha or meta.get("last_reviewed_sha", ""),
            )
            return StageVerdict(
                stage="review",
                status="needs_human",
                target_label=LABEL_NEEDS_HUMAN,
                updated_factory_block=merged_block,
                summary="Review requires human decision",
                review_report=cleaned_body,
            )

        # Default for FAIL / REVISION_NEEDED: replace findings in-place, track round,
        # and trip the circuit breaker once the round cap is reached.
        new_round = review_round + 1
        findings_block = replace_factory_findings(
            current_factory_block,
            cleaned_body,
            round_num=new_round,
            max_rounds=max_review_rounds,
        )
        if new_round >= max_review_rounds:
            escalation = (
                "\n\n## Review Loop Escalation\n"
                f"The {max_review_rounds}-round review limit was reached with unresolved findings. "
                "A human must decide whether to accept, fix manually, or close."
            )
            block = set_factory_meta(
                findings_block + escalation,
                stage="needs-human",
                review_round=str(new_round),
                last_reviewed_sha=reviewed_sha or "",
            )
            return StageVerdict(
                stage="review",
                status="needs_human",
                target_label=LABEL_NEEDS_HUMAN,
                updated_factory_block=block,
                summary=(
                    f"Review loop hit the {max_review_rounds}-round circuit breaker -> "
                    "waiting for human decision"
                ),
                review_report=cleaned_body,
            )

        block = set_factory_meta(
            findings_block,
            stage="build",
            review_round=str(new_round),
            last_reviewed_sha=reviewed_sha or "",
        )
        return StageVerdict(
            stage="review",
            status="revision_needed",
            target_label=LABEL_BUILD,
            updated_factory_block=block,
            summary="Review requested revisions -> returning to factory:build",
            review_report=cleaned_body,
        )

    return StageVerdict(
        stage=stage,
        status="completed",
        target_label=LABEL_COMPLETED,
        updated_factory_block=current_factory_block,
        summary=cleaned_body,
    )
