#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic build scope and privacy check for the `tdd-builder` skill."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_HOST_PATH_RE = re.compile(
    r"(?:/home/[a-zA-Z0-9][a-zA-Z0-9._-]*|/Users/[a-zA-Z0-9][a-zA-Z0-9._-]*)"
)


def _is_exempt_file(path_str: str) -> bool:
    """Return True if the file is a test or validator where path regexes/fixtures are expected."""
    p = Path(path_str)
    name = p.name
    parts = p.parts
    if "tests" in parts or "test" in parts:
        return True
    if name.startswith("test_") or name.endswith(("_test.py", "_test.ts", "_test.js")):
        return True
    if name in ("conftest.py", "check_build_scope.py"):
        return True
    return False


def check_diff_for_host_paths(
    cwd: Path, base_ref: str | None = None
) -> tuple[list[str], str | None]:
    """Scan added lines in git diff against base_ref (or auto-detected base) for leaked local host paths."""
    errors: list[str] = []

    # If base_ref is given, run against it directly.
    if base_ref:
        proc = subprocess.run(
            ["git", "diff", "--unified=0", base_ref],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            err_msg = (
                proc.stderr.strip() or f"git diff failed with code {proc.returncode}"
            )
            return errors, f"Failed to diff against '{base_ref}': {err_msg}"
    else:
        # Auto-detect diff base: try origin/main -> main -> HEAD -> unstaged
        proc = None
        for candidate in ("origin/main", "main", "HEAD", ""):
            cmd = ["git", "diff", "--unified=0"]
            if candidate:
                cmd.append(candidate)
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                break
        if proc is None or proc.returncode != 0:
            err_msg = proc.stderr.strip() if proc else "git diff failed"
            return errors, f"Failed to run git diff in '{cwd}': {err_msg}"

    current_file = "<unknown>"
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            continue
        if line.startswith("+++ /dev/null"):
            current_file = "<deleted>"
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if _is_exempt_file(current_file):
                continue
            match = _HOST_PATH_RE.search(line)
            if match:
                errors.append(
                    f"Privacy violation in {current_file}: added line contains host path '{match.group(0)}'."
                )
    return errors, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_build_scope.py",
        description="Deterministic build scope and privacy check: scans git diff for leaked local host paths.",
    )
    parser.add_argument(
        "ref",
        nargs="?",
        default=None,
        help="Git revision, commit range (e.g. '<sha>^..HEAD', 'HEAD~1'), or branch to diff against (default: auto-detect).",
    )
    parser.add_argument(
        "-C",
        "--cwd",
        type=Path,
        default=None,
        help="Repository working directory (default: current directory).",
    )

    try:
        args = parser.parse_args(argv)

        # Handle backward compatibility: if a single positional argument is an existing directory,
        # treat it as cwd rather than a git ref.
        cwd = args.cwd
        ref = args.ref
        if ref and cwd is None:
            candidate_path = Path(ref)
            if candidate_path.is_dir():
                cwd = candidate_path.resolve()
                ref = None

        if cwd is None:
            cwd = Path.cwd()
        else:
            cwd = cwd.resolve()

        if not cwd.is_dir():
            sys.stderr.write(f"Error: Working directory does not exist: {cwd}\n")
            return 2

        errors, fatal_err = check_diff_for_host_paths(cwd, base_ref=ref)
        if fatal_err:
            sys.stderr.write(f"Build scope & privacy check error: {fatal_err}\n")
            return 1

        if errors:
            sys.stderr.write("Build scope & privacy check failed:\n")
            for err in errors:
                sys.stderr.write(f"  - {err}\n")
            return 1

        print("Build scope & privacy check passed.")
        return 0

    except Exception as exc:
        sys.stderr.write(f"check_build_scope.py error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
