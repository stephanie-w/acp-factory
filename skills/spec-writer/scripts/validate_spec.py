#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic output validator for the `spec-writer` skill."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

VALID_STATUSES = {"SPEC_READY", "NEEDS_HUMAN"}
REQUIRED_READY_SECTIONS = (
    "Problem",
    "Behavior",
    "Files to Touch",
    "Decisions",
    "Assumptions",
    "Acceptance Criteria",
    "Out of Scope",
)

_CODE_BLOCK_RE = re.compile(r"(?P<fence>`{3,}|~{3,})[\s\S]*?(?P=fence)")
_STATUS_LINE_RE = re.compile(
    r"^\s*(?:\*\*?)?STATUS(?:\*\*?)?\s*:\s*([A-Z_]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SPEC_START_RE = re.compile(
    r"^(?:#\s+Specification|##\s+(?:Problem|Behavior|Open\s+Questions))\b",
    re.IGNORECASE | re.MULTILINE,
)
_OPEN_QUESTIONS_HEADING_RE = re.compile(
    r"^##+\s+Open\s+Questions\b",
    re.IGNORECASE | re.MULTILINE,
)
_UNFORMATTED_QUESTIONS_RE = re.compile(
    r"(?i)(?:\b\(Answer:\s*\d+=[a-z]|\*\*\[\d+\]\*\*|^\s*\[\d+\]\s+(?:Where|What|How|Which))",
    re.MULTILINE,
)
_AC_ROW_RE = re.compile(
    r"^\s*\|\s*`?(AC-\d+)`?\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|",
    re.MULTILINE,
)
_HOST_PATH_RE = re.compile(
    r"(?:/home/[a-zA-Z0-9][a-zA-Z0-9._-]*|/Users/[a-zA-Z0-9][a-zA-Z0-9._-]*)"
)


def _mask_code_blocks(text: str) -> str:
    return _CODE_BLOCK_RE.sub(lambda m: " " * len(m.group(0)), text)


def validate_spec_text(raw_text: str) -> tuple[list[str], str]:
    """Validate specification Markdown text and return (errors, normalized_markdown)."""
    errors: list[str] = []
    if not raw_text or not raw_text.strip():
        return (["Agent produced empty specification output."], "")

    masked = _mask_code_blocks(raw_text)
    status_matches = list(_STATUS_LINE_RE.finditer(masked))
    if not status_matches:
        errors.append(
            "Missing terminal 'STATUS: <SPEC_READY | NEEDS_HUMAN>' line at the end of the specification."
        )
        terminal_match = None
        status_token = ""
    else:
        terminal_match = status_matches[-1]
        status_token = terminal_match.group(1).upper()
        if status_token not in VALID_STATUSES:
            errors.append(
                f"Invalid spec STATUS '{status_token}'. Expected one of: {', '.join(sorted(VALID_STATUSES))}."
            )

    start_match = _SPEC_START_RE.search(masked)
    if not start_match:
        if _UNFORMATTED_QUESTIONS_RE.search(masked):
            errors.append(
                "Found unformatted questions without a '# Specification' / '## Open Questions' heading and 'STATUS: NEEDS_HUMAN'."
            )
        else:
            errors.append(
                "Missing required specification heading ('# Specification' or '## Problem'). Follow skills/spec-writer/templates/spec.md."
            )
        body = (
            raw_text[: terminal_match.start()].strip()
            if terminal_match
            else raw_text.strip()
        )
    else:
        end_idx = terminal_match.start() if terminal_match else len(raw_text)
        body = raw_text[start_match.start() : end_idx].strip()

    body = re.sub(r"(?:\n\s*---+\s*)+\Z", "", body).strip()
    masked_body = _mask_code_blocks(body)

    has_open_q_heading = bool(_OPEN_QUESTIONS_HEADING_RE.search(masked_body))
    has_unformatted_q = bool(_UNFORMATTED_QUESTIONS_RE.search(masked_body))

    if status_token == "NEEDS_HUMAN":
        if not has_open_q_heading:
            errors.append(
                "When STATUS is NEEDS_HUMAN, you MUST include a '## Open Questions' section listing numbered questions with 2-3 options and your recommendation."
            )
    elif status_token == "SPEC_READY":
        if has_open_q_heading or has_unformatted_q:
            errors.append(
                "STATUS is SPEC_READY, but '## Open Questions' (or unresolved questions) are present. Either resolve them under '## Assumptions' or set 'STATUS: NEEDS_HUMAN'."
            )
        for sec in REQUIRED_READY_SECTIONS:
            sec_re = re.compile(
                rf"^##+\s+{re.escape(sec)}\b", re.IGNORECASE | re.MULTILINE
            )
            if not sec_re.search(masked_body):
                errors.append(f"Missing required section '## {sec}'.")

        ac_rows = list(_AC_ROW_RE.finditer(body))
        if not ac_rows:
            errors.append(
                "Missing or empty 'AC-n' rows in '## Acceptance Criteria' table (expected '| AC-1 | Done when | How to check |')."
            )

    host_match = _HOST_PATH_RE.search(body)
    if host_match:
        errors.append(
            f"Privacy violation: found local host path '{host_match.group(0)}'. Use repository-relative paths only."
        )

    normalized = f"{body}\n\nSTATUS: {status_token}\n" if status_token else body
    return errors, normalized


def main() -> int:
    target = (
        Path(sys.argv[1])
        if len(sys.argv) > 1 and sys.argv[1]
        else Path(os.environ.get("FACTORY_RESPONSE_FILE", ""))
    )
    if not target or not target.is_file():
        sys.stderr.write(
            "Usage: validate_spec.py <response_file> (or set FACTORY_RESPONSE_FILE)\n"
        )
        return 2

    raw_text = target.read_text(encoding="utf-8")
    errors, normalized = validate_spec_text(raw_text)
    if errors:
        sys.stderr.write("Specification validation failed:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        sys.stderr.write(
            "\nRe-emit the complete specification strictly following skills/spec-writer/templates/spec.md.\n"
        )
        return 1

    target.write_text(normalized, encoding="utf-8")
    print("Specification validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
