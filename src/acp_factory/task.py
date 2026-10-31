"""TaskContext model and in-place Issue Body parser/renderer for GitHub label workflows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

FACTORY_BLOCK_START = "<!-- factory:start -->"
FACTORY_BLOCK_END = "<!-- factory:end -->"

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
        if raw_token in ("ACCEPTED", "PASS"):
            return StageVerdict(
                stage="review",
                status="accepted",
                target_label=LABEL_PR_READY,
                updated_factory_block=current_factory_block.strip(),
                summary=cleaned_body or "Review accepted -> PR ready for Human Gate 2",
                publish_pr=True,
            )
        if raw_token in ("HUMAN_DECISION_NEEDED", "NEEDS_HUMAN", "STOP"):
            merged_block = f"{current_factory_block.strip()}\n\n## Review Escalation\n{cleaned_body}".strip()
            return StageVerdict(
                stage="review",
                status="needs_human",
                target_label=LABEL_NEEDS_HUMAN,
                updated_factory_block=merged_block,
                summary="Review requires human decision",
            )
        # Default for FAIL / REVISION_NEEDED: send back to build with findings
        merged_block = f"{current_factory_block.strip()}\n\n## Latest Review Findings\n{cleaned_body}".strip()
        return StageVerdict(
            stage="review",
            status="revision_needed",
            target_label=LABEL_BUILD,
            updated_factory_block=merged_block,
            summary="Review requested revisions -> returning to factory:build",
        )

    return StageVerdict(
        stage=stage,
        status="completed",
        target_label=LABEL_COMPLETED,
        updated_factory_block=current_factory_block,
        summary=cleaned_body,
    )
