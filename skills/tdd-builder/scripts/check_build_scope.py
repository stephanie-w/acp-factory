#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic build scope and privacy check for the `tdd-builder` skill."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_HOST_PATH_RE = re.compile(
    r"(?:/home/[a-zA-Z0-9][a-zA-Z0-9._-]*|/Users/[a-zA-Z0-9][a-zA-Z0-9._-]*)"
)


def check_diff_for_host_paths(cwd: Path, base_ref: str = "main") -> list[str]:
    """Scan added lines in git diff against base_ref (or HEAD) for leaked local host paths."""
    errors: list[str] = []
    proc = subprocess.run(
        ["git", "diff", "--unified=0", base_ref],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        proc = subprocess.run(
            ["git", "diff", "--unified=0", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )

    current_file = "<unknown>"
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            continue
        if line.startswith("+") and not line.startswith("+++"):
            # Allow test assertions that explicitly test host-path regex detection
            if (
                "test_" in current_file
                or "validate_" in current_file
                or "check_build_scope" in current_file
            ):
                continue
            match = _HOST_PATH_RE.search(line)
            if match:
                errors.append(
                    f"Privacy violation in {current_file}: added line contains host path '{match.group(0)}'."
                )
    return errors


def main() -> int:
    cwd = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    errors = check_diff_for_host_paths(cwd)
    if errors:
        sys.stderr.write("Build scope & privacy check failed:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        return 1
    print("Build scope & privacy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
