"""Helpers for resolving and inspecting local acp-factory transcripts."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .acp.events import extract_usage_metrics
from .config import resolve_runs_dir
from .history import candidate_runs_dirs, list_run_history

logger = logging.getLogger(__name__)


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
    runs_dir: Path | None = None,
) -> Path:
    """Resolve a run ID, prefix, directory, or file path to a transcript JSON file."""
    ws = workspace.resolve()
    primary_runs_dir = (runs_dir or resolve_runs_dir(ws)).resolve()
    candidates = candidate_runs_dirs(ws, primary_runs_dir)

    if target is None:
        records = list_run_history(ws, runs_dir=primary_runs_dir)
        if records:
            run_id = str(records[0]["run_id"])
            for candidate_dir in candidates:
                found = _pick_transcript_in_attempt(
                    candidate_dir / run_id / f"attempt-{attempt}"
                )
                if found is not None:
                    return found
        for candidate_dir in candidates:
            latest_found = _pick_transcript_in_attempt(
                candidate_dir / "latest" / f"attempt-{attempt}"
            )
            if latest_found is not None:
                return latest_found
        raise FileNotFoundError(
            f"No transcript found for attempt-{attempt} under {primary_runs_dir}."
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

    run_dir = next(
        (
            candidate_dir / target
            for candidate_dir in candidates
            if (candidate_dir / target).is_dir()
        ),
        None,
    )
    if run_dir is None:
        matches = sorted(
            {
                candidate_dir / p.name
                for candidate_dir in candidates
                for p in candidate_dir.iterdir()
                if p.is_dir()
                and p.name not in ("latest", "dry-run")
                and p.name.startswith(target)
            }
        )
        if len(matches) == 1:
            run_dir = matches[0]
        elif len(matches) > 1:
            names = ", ".join(p.name for p in matches[:5])
            raise FileNotFoundError(
                f"Ambiguous run ID prefix {target!r}; matches: {names}"
            )

    if run_dir is not None:
        found = _pick_transcript_in_attempt(run_dir / f"attempt-{attempt}")
        if found is not None:
            return found

    raise FileNotFoundError(
        f"No transcript found for {target!r} (attempt-{attempt}) under {primary_runs_dir}."
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
                    str(tc.get("name") or ""),
                    str(tc.get("kind", "")),
                    json.dumps(tc.get("raw_input")) if tc.get("raw_input") else "",
                    extract_tool_output(tc),
                ]
            )
            if not regex.search(haystack):
                continue
        results.append((idx, tc))
    return results


def extract_tool_duration(tc: dict[str, Any]) -> float | None:
    """Return `duration_seconds` for a tool call if recorded as a finite number."""
    val = tc.get("duration_seconds")
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    return None


def search_text_lines(text: str, pattern: str) -> list[tuple[int, str]]:
    """Return 1-based `(line_number, line)` pairs matching `pattern` (case-insensitive)."""
    regex = re.compile(pattern, re.IGNORECASE)
    return [
        (lineno, line)
        for lineno, line in enumerate(text.splitlines(), start=1)
        if regex.search(line)
    ]


def _is_tool_call_failed(tc: dict[str, Any]) -> bool:
    exit_code = extract_tool_exit_code(tc)
    status = str(tc.get("status", ""))
    return status != "completed" or (exit_code is not None and exit_code != 0)


def compute_transcript_stats(data: dict[str, Any], *, top: int = 10) -> dict[str, Any]:
    """Compute per-kind, slowest, and largest tool-call stats for a single transcript."""
    raw_calls = data.get("tool_calls")
    tool_calls: list[dict[str, Any]] = (
        [tc for tc in raw_calls if isinstance(tc, dict)]
        if isinstance(raw_calls, list)
        else []
    )

    by_kind_map: dict[str, dict[str, Any]] = {}
    call_rows: list[dict[str, Any]] = []
    total_output_chars = 0
    failed_count = 0

    for idx, tc in enumerate(tool_calls):
        kind = str(tc.get("kind") or "other")
        title = str(tc.get("title") or "-")
        dur = extract_tool_duration(tc)
        out_len = len(extract_tool_output(tc))
        failed = _is_tool_call_failed(tc)
        if failed:
            failed_count += 1
        total_output_chars += out_len

        bucket = by_kind_map.setdefault(
            kind,
            {
                "kind": kind,
                "count": 0,
                "failed": 0,
                "total_duration_seconds": 0.0,
                "timed_count": 0,
                "max_duration_seconds": None,
                "total_output_chars": 0,
            },
        )
        bucket["count"] += 1
        if failed:
            bucket["failed"] += 1
        bucket["total_output_chars"] += out_len
        if dur is not None:
            bucket["total_duration_seconds"] = round(
                bucket["total_duration_seconds"] + dur, 3
            )
            bucket["timed_count"] += 1
            prev_max = bucket["max_duration_seconds"]
            bucket["max_duration_seconds"] = (
                dur if prev_max is None else max(prev_max, dur)
            )

        call_rows.append(
            {
                "index": idx,
                "kind": kind,
                "name": tc.get("name"),
                "status": str(tc.get("status") or "-"),
                "exit": extract_tool_exit_code(tc),
                "duration_seconds": dur,
                "output_chars": out_len,
                "title": title,
            }
        )

    for bucket in by_kind_map.values():
        bucket["estimated_tokens"] = bucket["total_output_chars"] // 4

    by_kind = sorted(by_kind_map.values(), key=lambda x: x["count"], reverse=True)
    timed = [c for c in call_rows if c["duration_seconds"] is not None]
    slowest_calls = sorted(
        timed, key=lambda c: float(c["duration_seconds"]), reverse=True
    )[:top]
    largest_calls = sorted(
        call_rows, key=lambda c: int(c["output_chars"]), reverse=True
    )[:top]

    summary_block = data.get("summary") or {}
    tokens, cost_usd = extract_usage_metrics(data.get("usage"))
    if tokens == 0 and isinstance(summary_block.get("tokens"), int):
        tokens = summary_block["tokens"]
    if cost_usd == 0.0 and isinstance(summary_block.get("cost_usd"), (int, float)):
        cost_usd = float(summary_block["cost_usd"])

    prompt_chars = len(str(data.get("prompt") or data.get("prompt_preview") or ""))
    thought_chars = len(
        str(data.get("thought_text") or data.get("thought_preview") or "")
    )
    response_chars = len(
        str(data.get("response_text") or data.get("response_preview") or "")
    )
    estimated_tokens_breakdown = {
        "prompt": prompt_chars // 4,
        "thought": thought_chars // 4,
        "response": response_chars // 4,
        "tool_output": total_output_chars // 4,
    }
    estimated_tokens = (
        prompt_chars + thought_chars + response_chars + total_output_chars
    ) // 4

    return {
        "duration_seconds": data.get("duration_seconds")
        or summary_block.get("duration_seconds"),
        "tokens": tokens,
        "cost_usd": round(cost_usd, 6),
        "estimated_tokens": estimated_tokens,
        "estimated_tokens_breakdown": estimated_tokens_breakdown,
        "tool_call_count": len(tool_calls),
        "failed_count": failed_count,
        "total_output_chars": total_output_chars,
        "by_kind": by_kind,
        "slowest_calls": slowest_calls,
        "largest_calls": largest_calls,
    }


def compute_runs_stats(
    workspace: Path,
    *,
    limit: int = 20,
    top: int = 10,
    runs_dir: Path | None = None,
) -> dict[str, Any]:
    """Aggregate run-level and tool-call stats across recent resolved/legacy runs."""
    ws = workspace.resolve()
    primary_runs_dir = (runs_dir or resolve_runs_dir(ws)).resolve()
    candidates = candidate_runs_dirs(ws, primary_runs_dir)
    records = list_run_history(ws, runs_dir=primary_runs_dir)[:limit]

    total_duration = 0.0
    total_tokens = 0
    total_estimated_tokens = 0
    total_cost = 0.0
    telemetry_runs = 0
    succeeded_runs = 0
    failed_runs = 0
    stage_map: dict[str, dict[str, Any]] = {}
    run_rows: list[dict[str, Any]] = []
    all_calls: list[dict[str, Any]] = []

    for rec in records:
        run_id = str(rec.get("run_id") or "-")
        stage = str(rec.get("stage") or "-")
        dur = rec.get("duration_seconds")
        dur_val = (
            float(dur)
            if isinstance(dur, (int, float)) and not isinstance(dur, bool)
            else 0.0
        )
        attempts = rec.get("attempts_used")
        attempts_val = (
            int(attempts)
            if isinstance(attempts, int) and not isinstance(attempts, bool)
            else 0
        )
        succ = rec.get("succeeded")
        if succ is True:
            succeeded_runs += 1
        elif succ is False:
            failed_runs += 1

        usage = rec.get("usage") or {}
        telemetry = usage.get("telemetry_available") is True
        run_tokens = 0
        run_cost = 0.0
        if telemetry:
            telemetry_runs += 1
            tok = usage.get("total_tokens")
            cost = usage.get("total_cost_usd")
            if isinstance(tok, (int, float)) and not isinstance(tok, bool):
                run_tokens = int(tok)
                total_tokens += run_tokens
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                run_cost = float(cost)
                total_cost += run_cost

        run_estimated_tokens = 0
        run_dir = next(
            (
                candidate_dir / run_id
                for candidate_dir in candidates
                if (candidate_dir / run_id).is_dir()
            ),
            None,
        )
        if run_dir is not None:
            for attempt_dir in sorted(run_dir.glob("attempt-*")):
                t_path = _pick_transcript_in_attempt(attempt_dir)
                if t_path is None:
                    continue
                try:
                    t_data = load_transcript(t_path)
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    logger.debug("Skipping unreadable transcript %s: %s", t_path, exc)
                    continue
                t_stats = compute_transcript_stats(t_data, top=top)
                run_estimated_tokens += int(t_stats["estimated_tokens"])
                for row in t_stats["slowest_calls"]:
                    all_calls.append(
                        {"run_id": run_id, "attempt": attempt_dir.name, **row}
                    )
                if not t_stats["slowest_calls"]:
                    for row in t_stats["largest_calls"]:
                        all_calls.append(
                            {"run_id": run_id, "attempt": attempt_dir.name, **row}
                        )

        total_estimated_tokens += run_estimated_tokens
        total_duration += dur_val
        sb = stage_map.setdefault(
            stage,
            {
                "stage": stage,
                "runs": 0,
                "succeeded": 0,
                "total_duration_seconds": 0.0,
                "total_attempts": 0,
                "total_tokens": 0,
                "total_estimated_tokens": 0,
                "total_cost_usd": 0.0,
                "telemetry_runs": 0,
            },
        )
        sb["runs"] += 1
        if succ is True:
            sb["succeeded"] += 1
        sb["total_duration_seconds"] += dur_val
        sb["total_attempts"] += attempts_val
        sb["total_tokens"] += run_tokens
        sb["total_estimated_tokens"] += run_estimated_tokens
        sb["total_cost_usd"] += run_cost
        if telemetry:
            sb["telemetry_runs"] += 1

        run_rows.append(
            {
                "run_id": run_id,
                "stage": stage,
                "model": str(rec.get("model") or "-"),
                "issue_number": rec.get("issue_number"),
                "attempts_used": attempts_val,
                "duration_seconds": round(dur_val, 3),
                "tokens": run_tokens,
                "estimated_tokens": run_estimated_tokens,
                "cost_usd": round(run_cost, 6),
                "telemetry_available": telemetry,
                "succeeded": succ,
            }
        )

    by_stage = [
        {
            "stage": sb["stage"],
            "runs": sb["runs"],
            "succeeded": sb["succeeded"],
            "avg_duration_seconds": round(sb["total_duration_seconds"] / sb["runs"], 2),
            "avg_attempts": round(sb["total_attempts"] / sb["runs"], 2),
            "total_tokens": sb["total_tokens"],
            "avg_tokens": round(sb["total_tokens"] / sb["runs"]),
            "total_estimated_tokens": sb["total_estimated_tokens"],
            "avg_estimated_tokens": round(sb["total_estimated_tokens"] / sb["runs"]),
            "total_cost_usd": round(sb["total_cost_usd"], 6),
            "telemetry_runs": sb["telemetry_runs"],
        }
        for sb in sorted(stage_map.values(), key=lambda x: x["runs"], reverse=True)
    ]

    slowest_runs = sorted(
        run_rows, key=lambda r: float(r["duration_seconds"]), reverse=True
    )[:top]
    top_token_runs = sorted(
        run_rows,
        key=lambda r: (
            int(r["tokens"]) if int(r["tokens"]) > 0 else int(r["estimated_tokens"])
        ),
        reverse=True,
    )[:top]
    timed_calls = [c for c in all_calls if c.get("duration_seconds") is not None]
    slowest_calls = sorted(
        timed_calls, key=lambda c: float(c["duration_seconds"]), reverse=True
    )[:top]
    largest_calls = sorted(
        all_calls, key=lambda c: int(c["output_chars"]), reverse=True
    )[:top]

    return {
        "total_runs": len(records),
        "succeeded_runs": succeeded_runs,
        "failed_runs": failed_runs,
        "total_duration_seconds": round(total_duration, 2),
        "avg_duration_seconds": round(total_duration / len(records), 2)
        if records
        else 0.0,
        "total_tokens": total_tokens,
        "total_estimated_tokens": total_estimated_tokens,
        "avg_tokens_per_run": round(total_tokens / len(records)) if records else 0,
        "avg_estimated_tokens_per_run": round(total_estimated_tokens / len(records))
        if records
        else 0,
        "telemetry_runs": telemetry_runs,
        "total_cost_usd": round(total_cost, 6),
        "by_stage": by_stage,
        "slowest_runs": slowest_runs,
        "top_token_runs": top_token_runs,
        "slowest_calls": slowest_calls,
        "largest_calls": largest_calls,
    }
