"""Helpers for resolving and inspecting local acp-factory transcripts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .history import list_run_history


def _pick_transcript_in_attempt(attempt_dir: Path) -> Path | None:
    """Return `transcript.json` if present, else `transcript.summary.json`."""
    for name in ("transcript.json", "transcript.summary.json"):
        candidate = attempt_dir / name
        if candidate.is_file():
            return candidate
    return None


def resolve_transcript_path(
    target: str | None,
    *,
    workspace: Path,
    attempt: int = 1,
) -> Path:
    """Resolve a run ID, prefix, directory, or file path to a transcript JSON file."""
    ws = workspace.resolve()
    runs_dir = ws / ".factory" / "runs"

    if target is None:
        records = list_run_history(ws)
        if records:
            run_id = str(records[0]["run_id"])
            found = _pick_transcript_in_attempt(
                runs_dir / run_id / f"attempt-{attempt}"
            )
            if found is not None:
                return found
        latest_found = _pick_transcript_in_attempt(
            runs_dir / "latest" / f"attempt-{attempt}"
        )
        if latest_found is not None:
            return latest_found
        raise FileNotFoundError(
            f"No transcript found for attempt-{attempt} under {runs_dir}."
        )

    explicit = Path(target).expanduser()
    if explicit.is_file():
        return explicit
    if explicit.is_dir():
        direct = _pick_transcript_in_attempt(explicit)
        if direct is not None:
            return direct
        nested = _pick_transcript_in_attempt(explicit / f"attempt-{attempt}")
        if nested is not None:
            return nested
        raise FileNotFoundError(f"No transcript JSON found in {explicit}.")

    run_dir = runs_dir / target
    if not run_dir.is_dir() and runs_dir.is_dir():
        matches = sorted(
            p
            for p in runs_dir.iterdir()
            if p.is_dir()
            and p.name not in ("latest", "dry-run")
            and p.name.startswith(target)
        )
        if len(matches) == 1:
            run_dir = matches[0]
        elif len(matches) > 1:
            names = ", ".join(p.name for p in matches[:5])
            raise FileNotFoundError(
                f"Ambiguous run ID prefix {target!r}; matches: {names}"
            )

    found = _pick_transcript_in_attempt(run_dir / f"attempt-{attempt}")
    if found is not None:
        return found

    raise FileNotFoundError(
        f"No transcript found for {target!r} (attempt-{attempt}) under {runs_dir}."
    )


def load_transcript(path: Path) -> dict[str, Any]:
    """Load a `transcript.json` or `transcript.summary.json` file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}, got {type(data).__name__}.")
    return data


def extract_tool_exit_code(tc: dict[str, Any]) -> int | None:
    """Return the integer exit code from `raw_output.metadata.exit`, if present."""
    raw_output = tc.get("raw_output")
    if isinstance(raw_output, dict):
        meta = raw_output.get("metadata")
        if isinstance(meta, dict):
            exit_val = meta.get("exit")
            if isinstance(exit_val, int) and not isinstance(exit_val, bool):
                return exit_val
    return None


def extract_tool_output(tc: dict[str, Any]) -> str:
    """Return the best available output text for a tool call entry."""
    for key in ("content", "output_preview"):
        val = tc.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, list) and val:
            return "\n".join(str(item) for item in val)
    raw_output = tc.get("raw_output")
    if isinstance(raw_output, dict):
        output = raw_output.get("output")
        if isinstance(output, str) and output:
            return output
        return json.dumps(raw_output, indent=2)
    if raw_output is not None:
        return str(raw_output)
    return ""


def filter_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    kind: str | None = None,
    failed: bool = False,
    grep: str | None = None,
) -> list[tuple[int, dict[str, Any]]]:
    """Filter tool calls by kind, failure/non-zero exit, and optional regex."""
    regex = re.compile(grep, re.IGNORECASE) if grep else None
    results: list[tuple[int, dict[str, Any]]] = []
    for idx, tc in enumerate(tool_calls):
        if kind and str(tc.get("kind", "")).lower() != kind.lower():
            continue
        exit_code = extract_tool_exit_code(tc)
        status = str(tc.get("status", ""))
        if failed and status == "completed" and (exit_code is None or exit_code == 0):
            continue
        if regex is not None:
            haystack = "\n".join(
                [
                    str(tc.get("title", "")),
                    str(tc.get("kind", "")),
                    json.dumps(tc.get("raw_input")) if tc.get("raw_input") else "",
                    extract_tool_output(tc),
                ]
            )
            if not regex.search(haystack):
                continue
        results.append((idx, tc))
    return results


def search_text_lines(text: str, pattern: str) -> list[tuple[int, str]]:
    """Return 1-based `(line_number, line)` pairs matching `pattern` (case-insensitive)."""
    regex = re.compile(pattern, re.IGNORECASE)
    return [
        (lineno, line)
        for lineno, line in enumerate(text.splitlines(), start=1)
        if regex.search(line)
    ]
