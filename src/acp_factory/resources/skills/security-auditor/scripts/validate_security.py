#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic output validator for the `security-auditor` skill."""

from __future__ import annotations

import argparse
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
_SUMMARY_HEADING_RE = re.compile(
    r"^###+\s+Security\s+Audit\s+Summary\b",
    re.IGNORECASE | re.MULTILINE,
)
_FINDINGS_HEADING_RE = re.compile(
    r"^###+\s+Security\s+Findings\b",
    re.IGNORECASE | re.MULTILINE,
)
_BLOCKING_OR_MAJOR_RE = re.compile(r"`?\[(?:blocking|major)\]`?", re.IGNORECASE)
_HOST_PATH_RE = re.compile(
    r"(?:/home/[a-zA-Z0-9][a-zA-Z0-9._-]*|/Users/[a-zA-Z0-9][a-zA-Z0-9._-]*)"
)


def _mask_code_blocks(text: str) -> str:
    return _CODE_BLOCK_RE.sub(lambda m: " " * len(m.group(0)), text)


def validate_security_text(raw_text: str) -> tuple[list[str], str]:
    """Validate security report text and return (errors, normalized_markdown)."""
    errors: list[str] = []
    if not raw_text or not raw_text.strip():
        return (["Agent produced empty security audit output."], "")

    masked = _mask_code_blocks(raw_text)
    status_matches = list(_STATUS_LINE_RE.finditer(masked))
    if not status_matches:
        errors.append("Missing terminal 'STATUS: <ACCEPTED | REVISION_NEEDED>' line.")
        terminal_match = None
        status_token = ""
    else:
        terminal_match = status_matches[-1]
        status_token = terminal_match.group(1).upper()
        if status_token not in VALID_STATUSES:
            errors.append(
                f"Invalid security STATUS '{status_token}'. Expected ACCEPTED or REVISION_NEEDED."
            )

    summary_match = _SUMMARY_HEADING_RE.search(masked)
    if not summary_match:
        errors.append(
            "Missing required heading '### Security Audit Summary' (see templates/security-report.md)."
        )
        body = (
            raw_text[: terminal_match.start()].strip()
            if terminal_match
            else raw_text.strip()
        )
    else:
        end_idx = terminal_match.start() if terminal_match else len(raw_text)
        body = raw_text[summary_match.start() : end_idx].strip()

    body = re.sub(r"(?:\n\s*---+\s*)+\Z", "", body).strip()
    masked_body = _mask_code_blocks(body)

    if not _FINDINGS_HEADING_RE.search(masked_body):
        errors.append("Missing required section '### Security Findings'.")

    has_blocking_or_major = bool(_BLOCKING_OR_MAJOR_RE.search(masked_body))
    if status_token in ("ACCEPTED", "PASS") and has_blocking_or_major:
        errors.append(
            "STATUS is ACCEPTED/PASS, but '[blocking]' or '[major]' findings are listed under '### Security Findings'. Use 'STATUS: REVISION_NEEDED'."
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_security.py",
        description="Deterministic output validator for the security-auditor skill.",
    )
    parser.add_argument(
        "response_file",
        nargs="?",
        default=None,
        help="Path to security report response markdown file or '-' for stdin (default: $FACTORY_RESPONSE_FILE).",
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
            errors, normalized = validate_security_text(raw_text)
        except Exception as exc:
            sys.stderr.write(f"Error validating security report: {exc}\n")
            return 1

        if errors:
            sys.stderr.write("Security report validation failed:\n")
            for err in errors:
                sys.stderr.write(f"  - {err}\n")
            sys.stderr.write(
                "\nRe-emit the complete security report strictly following skills/security-auditor/templates/security-report.md.\n"
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

        print("Security report validation passed.")
        return 0

    except Exception as exc:
        sys.stderr.write(f"validate_security.py error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
