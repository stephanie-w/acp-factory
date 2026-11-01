"""TaskContext model and in-place Issue Body parser/renderer for GitHub label workflows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

FACTORY_BLOCK_START = "<!-- factory:start -->"
FACTORY_BLOCK_END = "<!-- factory:end -->"

FACTORY_META_RE = re.compile(
    r"<!--\s*factory:meta\s+(.*?)-->",
    re.DOTALL,
)
FACTORY_FINDINGS_START = "<!-- factory:findings:start -->"
FACTORY_FINDINGS_END = "<!-- factory:findings:end -->"
_FACTORY_FINDINGS_RE = re.compile(
    rf"{re.escape(FACTORY_FINDINGS_START)}.*?{re.escape(FACTORY_FINDINGS_END)}",
    re.DOTALL,
)

MAX_REVIEW_ROUNDS = 3

LABEL_TRIAGE = "factory:triage"
LABEL_SPEC = "factory:spec"
LABEL_SPEC_READY = "factory:spec-ready"
LABEL_BUILD = "factory:build"
LABEL_REVIEW = "factory:review"
LABEL_PR_READY = "factory:pr-ready"
LABEL_DONE = "factory:done"
LABEL_COMPLETED = "factory:completed"
LABEL_NEEDS_HUMAN = "factory:needs-human"

ACTIONABLE_LABEL_TO_STAGE: dict[str, str] = {
    LABEL_TRIAGE: "triage",
    LABEL_SPEC: "spec",
    LABEL_BUILD: "build",
    LABEL_REVIEW: "review",
    LABEL_DONE: "done",
}

ALL_FACTORY_STAGE_LABELS: tuple[str, ...] = (
    LABEL_TRIAGE,
    LABEL_SPEC,
    LABEL_SPEC_READY,
    LABEL_BUILD,
    LABEL_REVIEW,
    LABEL_PR_READY,
    LABEL_DONE,
    LABEL_COMPLETED,
    LABEL_NEEDS_HUMAN,
)

_FACTORY_BLOCK_RE = re.compile(
    rf"{re.escape(FACTORY_BLOCK_START)}(.*?){re.escape(FACTORY_BLOCK_END)}",
    re.DOTALL,
)


def parse_factory_meta(factory_block: str) -> dict[str, str]:
    """Parse `<!-- factory:meta k=v ... -->` key/value fields from a factory block."""
    match = FACTORY_META_RE.search(factory_block)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for token in match.group(1).split():
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key] = value
    return fields


def render_factory_meta(**fields: str) -> str:
    """Render a `<!-- factory:meta ... -->` comment from key/value fields."""
    inner = " ".join(f"{key}={value}" for key, value in fields.items() if value)
    return f"<!-- factory:meta {inner} -->"


def set_factory_meta(factory_block: str, **updates: str) -> str:
    """Merge `updates` into the factory block's meta comment, replacing it in place."""
    existing = parse_factory_meta(factory_block)
    merged = {key: value for key, value in {**existing, **updates}.items() if value}
    body = FACTORY_META_RE.sub("", factory_block).strip()
    if not merged:
        return body
    meta = render_factory_meta(**merged)
    return f"{meta}\n{body}" if body else meta


def strip_factory_meta(factory_block: str) -> str:
    """Remove the `<!-- factory:meta ... -->` comment from a factory block."""
    return FACTORY_META_RE.sub("", factory_block).strip()


def strip_factory_findings(factory_block: str) -> str:
    """Remove the delimited findings section (including a dangling start marker) from a factory block."""
    cleaned = _FACTORY_FINDINGS_RE.sub("", factory_block)
    cleaned = cleaned.replace(FACTORY_FINDINGS_START, "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def replace_factory_findings(
    factory_block: str,
    findings_body: str,
    *,
    round_num: int,
    max_rounds: int,
) -> str:
    """Replace the delimited findings section in-place with `## Latest Review Findings (Round N/3)`."""
    base = strip_factory_findings(factory_block).strip()
    section = (
        f"{FACTORY_FINDINGS_START}\n"
        f"## Latest Review Findings (Round {round_num}/{max_rounds})\n"
        f"{findings_body.strip()}\n"
        f"{FACTORY_FINDINGS_END}"
    )
    return f"{base}\n\n{section}" if base else section


def extract_factory_findings(factory_block: str) -> str:
    """Return the content between the findings delimiters, without the markers."""
    match = _FACTORY_FINDINGS_RE.search(factory_block)
    if not match:
        return ""
    return (
        match.group(0)
        .replace(FACTORY_FINDINGS_START, "")
        .replace(FACTORY_FINDINGS_END, "")
        .strip()
    )


def clean_spec_block(factory_block: str) -> str:
    """Return the specification content with internal meta and findings comments removed."""
    return strip_factory_meta(strip_factory_findings(factory_block)).strip()


class IssueComment(BaseModel):
    """A human comment on a GitHub Issue."""

    author: str = Field(default="unknown")
    body: str = Field(default="")
    created_at: str = Field(default="")


class TaskContext(BaseModel):
    """Stateful work item backed by a GitHub Issue."""

    issue_number: int
    title: str
    state: str = "OPEN"
    url: str = ""
    original_body: str = ""
    factory_block: str = ""
    labels: list[str] = Field(default_factory=list)
    comments: list[IssueComment] = Field(default_factory=list)
    branch: str = ""
    worktree_path: Path | None = None
    pr_url: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if not self.branch:
            self.branch = f"factory/issue-{self.issue_number}"

    @property
    def current_stage(self) -> str | None:
        """Return the actionable stage determined by the issue's state and `factory:*` labels."""
        if self.state.upper() == "CLOSED":
            if LABEL_COMPLETED in self.labels:
                return None
            return "done"
        return resolve_stage_from_labels(self.labels)

    @classmethod
    def from_gh_json(cls, data: dict[str, Any]) -> TaskContext:
        """Construct a TaskContext from `gh issue view --json` output."""
        number = int(data["number"])
        state = str(data.get("state") or "OPEN").upper()
        raw_body = data.get("body") or ""
        orig_body, fac_block = split_issue_body(raw_body)

        raw_labels = data.get("labels") or []
        labels = [
            lbl["name"] if isinstance(lbl, dict) else str(lbl) for lbl in raw_labels
        ]

        raw_comments = data.get("comments") or []
        comments: list[IssueComment] = []
        for c in raw_comments:
            if not isinstance(c, dict):
                continue
            author_obj = c.get("author") or {}
            author_login = (
                author_obj.get("login", "unknown")
                if isinstance(author_obj, dict)
                else str(author_obj)
            )
            comments.append(
                IssueComment(
                    author=author_login,
                    body=str(c.get("body") or ""),
                    created_at=str(c.get("createdAt") or ""),
                )
            )

        return cls(
            issue_number=number,
            title=str(data.get("title") or ""),
            state=state,
            url=str(data.get("url") or ""),
            original_body=orig_body,
            factory_block=fac_block,
            labels=labels,
            comments=comments,
        )


def split_issue_body(full_body: str) -> tuple[str, str]:
    """Split a GitHub Issue body into `(original_body, factory_block)`.

    Everything outside `<!-- factory:start --> ... <!-- factory:end -->` is preserved
    as `original_body`.
    """
    match = _FACTORY_BLOCK_RE.search(full_body)
    if not match:
        return full_body.strip(), ""

    factory_content = match.group(1).strip()
    before = full_body[: match.start()].rstrip()
    after = full_body[match.end() :].lstrip()
    if before and after:
        original = f"{before}\n\n{after}".strip()
    else:
        original = (before or after).strip()
    return original, factory_content


def render_issue_body(original_body: str, factory_block: str) -> str:
    """Combine `original_body` and `factory_block` using `<!-- factory:start/end -->` markers."""
    clean_orig = original_body.strip()
    clean_block = factory_block.strip()
    if not clean_block:
        return clean_orig

    managed_section = f"{FACTORY_BLOCK_START}\n{clean_block}\n{FACTORY_BLOCK_END}"
    if not clean_orig:
        return managed_section
    return f"{clean_orig}\n\n{managed_section}\n"


def resolve_stage_from_labels(labels: list[str]) -> str | None:
    """Determine the active factory stage from a list of GitHub labels."""
    if LABEL_DONE in labels:
        return "done"
    for label, stage in ACTIONABLE_LABEL_TO_STAGE.items():
        if label in labels:
            return stage
    return None


def format_task_prompt_context(task: TaskContext) -> str:
    """Render the full GitHub Issue context (description, living spec, and human comments)."""
    lines: list[str] = [
        f"# GitHub Issue #{task.issue_number}: {task.title}",
    ]
    if task.url:
        lines.append(f"- **URL:** {task.url}")
    if task.labels:
        lines.append(f"- **Labels:** {', '.join(task.labels)}")
    lines.append(f"- **Target Branch:** `{task.branch}`")
    lines.append("")
    lines.append("## Original Issue Description")
    lines.append(task.original_body or "(empty)")
    lines.append("")

    if task.factory_block:
        lines.append("## Current Factory Specification / Status Block")
        lines.append(task.factory_block)
        lines.append("")

    if task.comments:
        lines.append("## Human Comments (Chronological)")
        for idx, comment in enumerate(task.comments, start=1):
            header = f"### Comment #{idx} by @{comment.author}"
            if comment.created_at:
                header += f" ({comment.created_at})"
            lines.append(header)
            lines.append(comment.body.strip())
            lines.append("")

    return "\n".join(lines).strip()


class StageVerdict(BaseModel):
    """Structured outcome parsed from a stage agent's final response."""

    stage: str
    status: str
    target_label: str
    updated_factory_block: str
    summary: str = ""
    publish_pr: bool = False


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


def _clean_markdown_stage_body(text: str) -> str:
    """Strip conversational preamble before the first heading and any postscript after STATUS/VERDICT."""
    trimmed = text
    status_match = _STATUS_LINE_RE.search(trimmed)
    verdict_match = _VERDICT_LINE_RE.search(trimmed)
    cut_pos: int | None = None
    if status_match:
        cut_pos = status_match.start()
    elif verdict_match:
        cut_pos = verdict_match.start()

    if cut_pos is not None:
        trimmed = trimmed[:cut_pos]

    trimmed = _STATUS_LINE_RE.sub("", trimmed)
    trimmed = _VERDICT_LINE_RE.sub("", trimmed)

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
    status_match = _STATUS_LINE_RE.search(response_text)
    verdict_match = _VERDICT_LINE_RE.search(response_text)

    raw_token = ""
    if status_match:
        raw_token = status_match.group(1).upper()
    elif verdict_match:
        raw_token = verdict_match.group(1).upper()

    if stage == "triage":
        if raw_token in ("DUPLICATE", "TOO_BIG", "NEEDS_HUMAN", "NEEDS_INPUT", "STOP"):
            return StageVerdict(
                stage="triage",
                status=raw_token.lower(),
                target_label=LABEL_NEEDS_HUMAN,
                updated_factory_block=f"## Triage Report (`{raw_token}`)\n\n{cleaned_body}",
                summary=f"Triage requires human attention ({raw_token})",
            )
        return StageVerdict(
            stage="triage",
            status="ok",
            target_label=LABEL_SPEC,
            updated_factory_block=f"## Triage Report (`OK`)\n\n{cleaned_body}",
            summary="Triage passed -> ready for spec",
        )

    if stage == "spec":
        has_open_questions = bool(_OPEN_QUESTIONS_RE.search(cleaned_body))
        if raw_token in ("NEEDS_HUMAN", "NEEDS_INPUT", "OPEN_QUESTIONS") or (
            has_open_questions and raw_token != "SPEC_READY"
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
        review_round = int(meta.get("review_round", "0") or "0")

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
                summary=cleaned_body or "Review accepted -> PR ready for Human Gate 2",
                publish_pr=True,
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
        )

    return StageVerdict(
        stage=stage,
        status="completed",
        target_label=LABEL_COMPLETED,
        updated_factory_block=current_factory_block,
        summary=cleaned_body,
    )
