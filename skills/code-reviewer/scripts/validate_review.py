#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic output validator for the `code-reviewer` skill."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

VALID_STATUSES = {"ACCEPTED", "REVISION_NEEDED", "NEEDS_HUMAN", "PASS", "FAIL"}

_CODE_BLOCK_RE = re.compile(r"(?P<fence>`{3,}|~{3,})[\s\S]*?(?P=fence)")
_STATUS_LINE_RE = re.compile(
    r"^\s*(?:\*\*?)?(?:STATUS|VERDICT)(?:\*\*?)?\s*:\s*([A-Z_]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_RISK_HEADING_RE = re.compile(
    r"^###+\s+Risk\s+Classification\s*:\s*(LOW|MEDIUM|HIGH)\b",
    re.IGNORECASE | re.MULTILINE,
)
_FINDINGS_HEADING_RE = re.compile(
    r"^###+\s+Findings\b",
    re.IGNORECASE | re.MULTILINE,
)
_BLOCKING_OR_MAJOR_RE = re.compile(r"`?\[(?:blocking|major)\]`?", re.IGNORECASE)
_HOST_PATH_RE = re.compile(
    r"(?:/home/[a-zA-Z0-9][a-zA-Z0-9._-]*|/Users/[a-zA-Z0-9][a-zA-Z0-9._-]*)"
)


def _mask_code_blocks(text: str) -> str:
    return _CODE_BLOCK_RE.sub(lambda m: " " * len(m.group(0)), text)


def validate_review_text(raw_text: str) -> tuple[list[str], str]:
    """Validate review report text and return (errors, normalized_markdown)."""
    errors: list[str] = []
    if not raw_text or not raw_text.strip():
        return (["Agent produced empty review output."], "")

    masked = _mask_code_blocks(raw_text)
    status_matches = list(_STATUS_LINE_RE.finditer(masked))
    if not status_matches:
        errors.append(
            "Missing terminal 'STATUS: <ACCEPTED | REVISION_NEEDED | NEEDS_HUMAN>' line."
        )
        terminal_match = None
        status_token = ""
    else:
        terminal_match = status_matches[-1]
        status_token = terminal_match.group(1).upper()
        if status_token not in VALID_STATUSES:
            errors.append(
                f"Invalid review STATUS '{status_token}'. Expected ACCEPTED, REVISION_NEEDED, or NEEDS_HUMAN."
            )

    risk_match = _RISK_HEADING_RE.search(masked)
    if not risk_match:
        errors.append(
            "Missing required heading '### Risk Classification: <LOW | MEDIUM | HIGH>' (see templates/review-report.md)."
        )
        body = (
            raw_text[: terminal_match.start()].strip()
            if terminal_match
            else raw_text.strip()
        )
    else:
        end_idx = terminal_match.start() if terminal_match else len(raw_text)
        body = raw_text[risk_match.start() : end_idx].strip()

    body = re.sub(r"(?:\n\s*---+\s*)+\Z", "", body).strip()
    masked_body = _mask_code_blocks(body)

    if not _FINDINGS_HEADING_RE.search(masked_body):
        errors.append("Missing required section '### Findings'.")

    has_blocking_or_major = bool(_BLOCKING_OR_MAJOR_RE.search(masked_body))
    if status_token in ("ACCEPTED", "PASS") and has_blocking_or_major:
        errors.append(
            "STATUS is ACCEPTED/PASS, but '[blocking]' or '[major]' findings are listed under '### Findings'. Use 'STATUS: REVISION_NEEDED'."
        )

    host_match = _HOST_PATH_RE.search(body)
    if host_match:
        errors.append(
            f"Privacy violation: found local host path '{host_match.group(0)}'. Use repository-relative paths only."
        )

    canonical_status = (
        "ACCEPTED"
        if status_token == "PASS"
        else ("REVISION_NEEDED" if status_token == "FAIL" else status_token)
    )
    normalized = f"{body}\n\nSTATUS: {canonical_status}\n" if canonical_status else body
    return errors, normalized


def main() -> int:
    target = (
        Path(sys.argv[1])
        if len(sys.argv) > 1 and sys.argv[1]
        else Path(os.environ.get("FACTORY_RESPONSE_FILE", ""))
    )
    if not target or not target.is_file():
        sys.stderr.write(
            "Usage: validate_review.py <response_file> (or set FACTORY_RESPONSE_FILE)\n"
        )
        return 2

    raw_text = target.read_text(encoding="utf-8")
    errors, normalized = validate_review_text(raw_text)
    if errors:
        sys.stderr.write("Review report validation failed:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        sys.stderr.write(
            "\nRe-emit the complete review report strictly following skills/code-reviewer/templates/review-report.md.\n"
        )
        return 1

    target.write_text(normalized, encoding="utf-8")
    print("Review report validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
