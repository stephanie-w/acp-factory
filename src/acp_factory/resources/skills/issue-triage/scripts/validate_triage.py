#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic output validator for the `issue-triage` skill."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

VALID_STATUSES = {"OK", "TRIVIAL", "DUPLICATE", "TOO_BIG", "NEEDS_HUMAN"}
REQUIRED_FIELDS = (
    "Touched Area",
    "Current Behavior & Root Cause",
    "Duplicates / Related Issues",
    "Estimated Scope",
    "Verification Tooling Available",
)

_CODE_BLOCK_RE = re.compile(r"(?P<fence>`{3,}|~{3,})[\s\S]*?(?P=fence)")
_STATUS_LINE_RE = re.compile(
    r"^\s*(?:\*\*?)?STATUS(?:\*\*?)?\s*:\s*([A-Z_]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_HEADING_START_RE = re.compile(
    r"^###+\s+Codebase\s+Inspection\b", re.IGNORECASE | re.MULTILINE
)
_HOST_PATH_RE = re.compile(
    r"(?:/home/[a-zA-Z0-9][a-zA-Z0-9._-]*|/Users/[a-zA-Z0-9][a-zA-Z0-9._-]*)"
)


def _mask_code_blocks(text: str) -> str:
    return _CODE_BLOCK_RE.sub(lambda m: " " * len(m.group(0)), text)


def validate_triage_text(raw_text: str) -> tuple[list[str], str]:
    """Validate triage report text and return (errors, normalized_markdown)."""
    errors: list[str] = []
    if not raw_text or not raw_text.strip():
        return (["Agent produced empty triage output."], "")

    masked = _mask_code_blocks(raw_text)
    status_matches = list(_STATUS_LINE_RE.finditer(masked))
    if not status_matches:
        errors.append(
            "Missing terminal 'STATUS: <OK | TRIVIAL | DUPLICATE | TOO_BIG | NEEDS_HUMAN>' line."
        )
        terminal_match = None
        status_token = ""
    else:
        terminal_match = status_matches[-1]
        status_token = terminal_match.group(1).upper()
        if status_token not in VALID_STATUSES:
            errors.append(
                f"Invalid triage STATUS '{status_token}'. Expected one of: {', '.join(sorted(VALID_STATUSES))}."
            )

    heading_match = _HEADING_START_RE.search(raw_text)
    if not heading_match:
        errors.append(
            "Missing required starting section '### Codebase Inspection' (see templates/triage-report.md)."
        )
        body = (
            raw_text[: terminal_match.start()].strip()
            if terminal_match
            else raw_text.strip()
        )
    else:
        end_idx = terminal_match.start() if terminal_match else len(raw_text)
        body = raw_text[heading_match.start() : end_idx].strip()

    body = re.sub(r"(?:\n\s*---+\s*)+\Z", "", body).strip()

    for field_name in REQUIRED_FIELDS:
        pattern = re.compile(rf"\*\*{re.escape(field_name)}\*\*\s*:", re.IGNORECASE)
        if not pattern.search(body):
            errors.append(
                f"Missing required bullet '- **{field_name}**:' under '### Codebase Inspection'."
            )

    if status_token == "TOO_BIG" and not re.search(
        r"^###+\s+Suggested\s+Split\b", body, re.IGNORECASE | re.MULTILINE
    ):
        errors.append(
            "When STATUS is TOO_BIG, a '### Suggested Split' section with numbered build-order tickets is required."
        )

    if status_token == "TRIVIAL" and not re.search(
        r"^###+\s+Acceptance\s+Criteria\b", body, re.IGNORECASE | re.MULTILINE
    ):
        errors.append(
            "When STATUS is TRIVIAL, a '### Acceptance Criteria' section with AC-1 items is required."
        )

    host_match = _HOST_PATH_RE.search(body)
    if host_match:
        errors.append(
            f"Privacy violation: found local host path '{host_match.group(0)}'. Use repository-relative paths only."
        )

    normalized = f"{body}\n\nSTATUS: {status_token}\n" if status_token else body
    return errors, normalized


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_triage.py",
        description="Deterministic output validator for the issue-triage skill.",
    )
    parser.add_argument(
        "response_file",
        nargs="?",
        default=None,
        help="Path to triage response markdown file or '-' for stdin (default: $FACTORY_RESPONSE_FILE).",
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
            errors, normalized = validate_triage_text(raw_text)
        except Exception as exc:
            sys.stderr.write(f"Error validating triage report: {exc}\n")
            return 1

        if errors:
            sys.stderr.write("Triage report validation failed:\n")
            for err in errors:
                sys.stderr.write(f"  - {err}\n")
            sys.stderr.write(
                "\nRe-emit the complete triage report strictly following skills/issue-triage/templates/triage-report.md.\n"
            )
            return 1

        if target is not None:
            try:
                target.write_text(normalized, encoding="utf-8")
            except OSError as exc:
                sys.stderr.write(
                    f"Error writing normalized report to '{target}': {exc}\n"
                )
                return 2

        print("Triage report validation passed.")
        return 0

    except Exception as exc:
        sys.stderr.write(f"validate_triage.py error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
