#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic output validator for the `refactor-planner` skill."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

VALID_STATUSES = {"READY", "SPLIT_NEEDED", "NEEDS_HUMAN"}
REQUIRED_READY_SECTIONS = (
    "Invariant",
    "Target Seam & Dependency Direction",
    "Compatibility & Re-export Policy",
    "Files to Touch",
    "Baseline & Seam Tests",
    "Out of Scope",
)

_CODE_BLOCK_RE = re.compile(r"(?P<fence>`{3,}|~{3,})[\s\S]*?(?P=fence)")
_STATUS_LINE_RE = re.compile(
    r"^\s*(?:\*\*?)?STATUS(?:\*\*?)?\s*:\s*([A-Z_]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_PLAN_START_RE = re.compile(
    r"^(?:#\s+Refactor\s+Contract|##\s+(?:Problem(?:\s*&\s*Motivation)?|Invariant|Open\s+Questions|Proposed\s+Split))\b",
    re.IGNORECASE | re.MULTILINE,
)
_OPEN_QUESTIONS_HEADING_RE = re.compile(
    r"^##+\s+Open\s+Questions\b",
    re.IGNORECASE | re.MULTILINE,
)
_PROPOSED_SPLIT_HEADING_RE = re.compile(
    r"^##+\s+Proposed\s+Split\b",
    re.IGNORECASE | re.MULTILINE,
)
_HOST_PATH_RE = re.compile(
    r"(?:/home/[a-zA-Z0-9][a-zA-Z0-9._-]*|/Users/[a-zA-Z0-9][a-zA-Z0-9._-]*)"
)


def _mask_code_blocks(text: str) -> str:
    return _CODE_BLOCK_RE.sub(lambda m: " " * len(m.group(0)), text)


def validate_refactor_plan_text(raw_text: str) -> tuple[list[str], str]:
    """Validate Refactor Contract Markdown text and return (errors, normalized_markdown)."""
    errors: list[str] = []
    if not raw_text or not raw_text.strip():
        return (["Agent produced empty refactor plan output."], "")

    masked = _mask_code_blocks(raw_text)
    status_matches = list(_STATUS_LINE_RE.finditer(masked))
    if not status_matches:
        errors.append(
            "Missing terminal 'STATUS: <READY | SPLIT_NEEDED | NEEDS_HUMAN>' line at the end of the Refactor Contract."
        )
        terminal_match = None
        status_token = ""
    else:
        terminal_match = status_matches[-1]
        status_token = terminal_match.group(1).upper()
        if status_token not in VALID_STATUSES:
            errors.append(
                f"Invalid refactor plan STATUS '{status_token}'. "
                f"Expected one of: {', '.join(sorted(VALID_STATUSES))}."
            )

    start_match = _PLAN_START_RE.search(masked)
    if not start_match:
        errors.append(
            "Missing required heading ('# Refactor Contract' or '## Invariant'). "
            "Follow skills/refactor-planner/templates/refactor-plan.md."
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

    has_open_q = bool(_OPEN_QUESTIONS_HEADING_RE.search(masked_body))
    has_split = bool(_PROPOSED_SPLIT_HEADING_RE.search(masked_body))

    if status_token == "NEEDS_HUMAN":
        if not has_open_q:
            errors.append(
                "When STATUS is NEEDS_HUMAN, you MUST include a '## Open Questions' section "
                "listing numbered questions with 2-3 options and your recommendation."
            )
    elif status_token == "SPLIT_NEEDED":
        if not has_split:
            errors.append(
                "When STATUS is SPLIT_NEEDED, you MUST include a '## Proposed Split' section "
                "listing ordered, independently mergeable sub-refactors."
            )
    elif status_token == "READY":
        if has_open_q:
            errors.append(
                "STATUS is READY, but '## Open Questions' is present. "
                "Either resolve them under '## Assumptions' or set 'STATUS: NEEDS_HUMAN'."
            )
        for sec in REQUIRED_READY_SECTIONS:
            sec_re = re.compile(
                rf"^##+\s+{re.escape(sec)}\b", re.IGNORECASE | re.MULTILINE
            )
            if not sec_re.search(masked_body):
                errors.append(f"Missing required section '## {sec}'.")

    host_match = _HOST_PATH_RE.search(body)
    if host_match:
        errors.append(
            f"Privacy violation: found local host path '{host_match.group(0)}'. "
            "Use repository-relative paths only."
        )

    normalized = f"{body}\n\nSTATUS: {status_token}\n" if status_token else body
    return errors, normalized


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_refactor_plan.py",
        description="Deterministic output validator for the refactor-planner skill.",
    )
    parser.add_argument(
        "response_file",
        nargs="?",
        default=None,
        help="Path to refactor plan response markdown file or '-' for stdin (default: $FACTORY_RESPONSE_FILE).",
    )

    try:
        args = parser.parse_args(argv)
        raw_path = args.response_file or os.environ.get("FACTORY_RESPONSE_FILE", "")
        if not raw_path:
            sys.stderr.write(
                "Error: No response file specified. Provide a file path or set FACTORY_RESPONSE_FILE.\n"
            )
            return 2

        target: Path | None = None
        if raw_path == "-":
            try:
                raw_text = sys.stdin.read()
            except OSError as exc:
                sys.stderr.write(f"Error reading from stdin: {exc}\n")
                return 2
        else:
            target = Path(raw_path).resolve()
            if not target.is_file():
                sys.stderr.write(f"Error: Response file not found: {target}\n")
                return 2

            try:
                raw_text = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                sys.stderr.write(f"Error reading response file '{target}': {exc}\n")
                return 2

        try:
            errors, normalized = validate_refactor_plan_text(raw_text)
        except Exception as exc:
            sys.stderr.write(f"Error validating refactor plan: {exc}\n")
            return 1

        if errors:
            sys.stderr.write("Refactor plan validation failed:\n")
            for err in errors:
                sys.stderr.write(f"  - {err}\n")
            sys.stderr.write(
                "\nRe-emit the complete Refactor Contract strictly following "
                "skills/refactor-planner/templates/refactor-plan.md.\n"
            )
            return 1

        if target is not None:
            try:
                target.write_text(normalized, encoding="utf-8")
            except OSError as exc:
                sys.stderr.write(
                    f"Error writing normalized refactor plan to '{target}': {exc}\n"
                )
                return 2

        print("Refactor plan validation passed.")
        return 0

    except Exception as exc:
        sys.stderr.write(f"validate_refactor_plan.py error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
