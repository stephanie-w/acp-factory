"""Stream listener and turn event tracking built on acpterm's output and transcript modules."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from acpterm.output import _state as acpterm_output_state
from acpterm.transcript import TranscriptRecorder


@dataclass
class UsageTotals:
    """Cumulative token, cost, and turn totals accumulated across turns and stages."""

    tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0


def _non_negative_int(value: Any) -> int:
    """Coerce a telemetry value to a non-negative int, defaulting to 0 for bad input."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float):
        if not math.isfinite(value):
            return 0
        return int(value) if value > 0 else 0
    return 0


def _non_negative_float(value: Any) -> float:
    """Coerce a telemetry value to a non-negative float, defaulting to 0.0 for bad input."""
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        num = float(value)
        if not math.isfinite(num):
            return 0.0
        return num if num > 0.0 else 0.0
    return 0.0


def extract_usage_metrics(usage: dict[str, Any] | None) -> tuple[int, float]:
    """Extract `(tokens_used, cost_usd)` from an ACP `usage_update` or usage payload.

    Missing, malformed, or non-finite values are treated as zero so telemetry never
    crashes budget enforcement or falsely trips a cap.
    """
    if not usage:
        return 0, 0.0

    raw_tokens = (
        usage.get("used")
        or usage.get("total_tokens")
        or usage.get("totalTokens")
        or usage.get("tokens")
    )
    if raw_tokens is None and ("input_tokens" in usage or "output_tokens" in usage):
        raw_tokens = _non_negative_int(usage.get("input_tokens")) + _non_negative_int(
            usage.get("output_tokens")
        )

    tokens = _non_negative_int(raw_tokens)
    cost = usage.get("cost")
    if isinstance(cost, dict):
        amount: Any = cost.get("amount")
    else:
        amount = getattr(cost, "amount", None)
    return tokens, _non_negative_float(amount)


def _safe_json_value(val: Any) -> Any:
    """Recursively convert arbitrary objects into JSON-serializable primitives."""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, (list, tuple, set)):
        return [_safe_json_value(x) for x in val]
    if isinstance(val, dict):
        return {str(k): _safe_json_value(v) for k, v in val.items()}
    if hasattr(val, "__fspath__"):
        return str(val)
    if hasattr(val, "model_dump") and callable(val.model_dump):
        return _safe_json_value(val.model_dump())
    if hasattr(val, "__dict__"):
        return _safe_json_value(val.__dict__)
    return str(val)


def _truncate_text(text: str, max_chars: int) -> str:
    """Truncate text exceeding `max_chars`, appending metadata about omitted length."""
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n... [truncated {omitted} chars]"


def _condense_value(val: Any, max_chars: int) -> Any:
    """Recursively condense structured data by truncating long strings or deep arrays."""
    safe_val = _safe_json_value(val)
    if isinstance(safe_val, str):
        return _truncate_text(safe_val, max_chars)
    if isinstance(safe_val, dict):
        return {k: _condense_value(v, max_chars) for k, v in safe_val.items()}
    if isinstance(safe_val, list):
        if len(safe_val) > 25:
            condensed = [_condense_value(x, max_chars) for x in safe_val[:25]]
            condensed.append(f"... [{len(safe_val) - 25} items omitted]")
            return condensed
        return [_condense_value(x, max_chars) for x in safe_val]
    return safe_val


@dataclass
class TurnSummary:
    """Summary of a completed ACP agent turn."""

    prompt: str
    response_text: str
    thought_text: str
    stop_reason: str
    duration_seconds: float
    tool_call_count: int
    file_operations: list[dict[str, str | None]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    transcript_markdown: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    permissions: list[dict[str, str]] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        """Return the token count reported for this turn, or 0 when absent/invalid."""
        return extract_usage_metrics(self.usage)[0]

    @property
    def cost_usd(self) -> float:
        """Return the USD cost reported for this turn, or 0.0 when absent/invalid."""
        return extract_usage_metrics(self.usage)[1]

    def to_dict(self, *, max_output_chars: int | None = None) -> dict[str, Any]:
        """Convert the turn into a complete JSON-serializable dictionary.

        Args:
            max_output_chars: Optional safety ceiling on individual tool outputs
                (defaults to None, preserving full output).
        """
        tokens, cost_usd = extract_usage_metrics(self.usage)
        formatted_tools: list[dict[str, Any]] = []
        for tc in self.tool_calls:
            content = tc.get("content", "")
            if max_output_chars and isinstance(content, str):
                content = _truncate_text(content, max_output_chars)
            raw_out = tc.get("raw_output")
            if max_output_chars and raw_out is not None:
                raw_out = _condense_value(raw_out, max_output_chars)
            formatted_tools.append(
                {
                    "id": tc.get("id"),
                    "title": tc.get("title", ""),
                    "kind": tc.get("kind", ""),
                    "status": tc.get("status", "completed"),
                    "raw_input": _safe_json_value(tc.get("raw_input")),
                    "content": content,
                    "raw_output": _safe_json_value(raw_out),
                }
            )

        return {
            "summary": {
                "duration_seconds": round(self.duration_seconds, 3),
                "stop_reason": self.stop_reason,
                "tool_call_count": self.tool_call_count,
                "file_operations_count": len(self.file_operations),
                "tokens": tokens,
                "cost_usd": round(cost_usd, 6),
            },
            "prompt": self.prompt,
            "response_text": self.response_text,
            "thought_text": self.thought_text,
            "stop_reason": self.stop_reason,
            "duration_seconds": round(self.duration_seconds, 3),
            "tool_call_count": self.tool_call_count,
            "tool_calls": formatted_tools,
            "file_operations": _safe_json_value(self.file_operations),
            "permissions": _safe_json_value(self.permissions),
            "usage": _safe_json_value(self.usage),
        }

    def to_condensed_dict(self, *, max_output_len: int = 500) -> dict[str, Any]:
        """Generate a concise JSON representation optimized for retro agents and token efficiency.

        Large tool contents, scratchpad thoughts, and verbose outputs are aggressively truncated.
        """
        tokens, cost_usd = extract_usage_metrics(self.usage)
        condensed_tools: list[dict[str, Any]] = []
        for tc in self.tool_calls:
            entry: dict[str, Any] = {
                "id": tc.get("id"),
                "title": tc.get("title", ""),
                "kind": tc.get("kind", ""),
                "status": tc.get("status", "completed"),
            }
            if tc.get("raw_input") is not None:
                entry["raw_input"] = _condense_value(tc["raw_input"], max_output_len)
            content = tc.get("content", "")
            if content:
                entry["output_preview"] = _truncate_text(str(content), max_output_len)
            if tc.get("raw_output") is not None:
                entry["raw_output"] = _condense_value(tc["raw_output"], max_output_len)
            condensed_tools.append(entry)

        return {
            "summary": {
                "duration_seconds": round(self.duration_seconds, 3),
                "stop_reason": self.stop_reason,
                "tool_call_count": self.tool_call_count,
                "file_operations_count": len(self.file_operations),
                "tokens": tokens,
                "cost_usd": round(cost_usd, 6),
            },
            "prompt_preview": _truncate_text(self.prompt, max_output_len),
            "response_preview": _truncate_text(self.response_text, max_output_len)
            if self.response_text
            else "",
            "thought_preview": _truncate_text(self.thought_text, max_output_len)
            if self.thought_text
            else "",
            "tool_calls": condensed_tools,
            "file_operations": _safe_json_value(self.file_operations),
            "permissions": _safe_json_value(self.permissions),
        }


class TurnEventTracker:
    """Wraps acpterm's TranscriptRecorder to track per-turn metrics and reset state between retries."""

    def __init__(self, initial_prompt: str = "") -> None:
        self.recorder = TranscriptRecorder(prompt=initial_prompt)
        self._turn_start: float = 0.0
        self.history: list[TurnSummary] = []

    def begin_turn(self, prompt: str) -> None:
        """Reset per-turn buffers on the shared TranscriptRecorder before sending a prompt."""
        self._turn_start = time.monotonic()
        self.recorder.prompt = prompt
        self.recorder.thoughts.clear()
        self.recorder.messages.clear()
        self.recorder.tool_calls.clear()
        self.recorder.file_operations.clear()
        self.recorder.permissions.clear()
        self.recorder.stop_reason = None
        self.recorder.duration_seconds = None
        # Ensure acpterm's Rich markdown stream state is clean
        acpterm_output_state.end_stream()
        acpterm_output_state.flush_md()

    def end_turn(self, stop_reason: str) -> TurnSummary:
        """Finalize the turn, record duration, and return a TurnSummary snapshot."""
        duration = max(0.0, time.monotonic() - self._turn_start)
        self.recorder.set_stop_reason(stop_reason)
        self.recorder.set_duration(duration)

        recorded_tools: list[dict[str, Any]] = []
        for tc_id, tc in self.recorder.tool_calls.items():
            content_list = tc.get("content", [])
            content_str = (
                "\n".join(content_list).strip()
                if isinstance(content_list, list)
                else str(content_list or "")
            )
            recorded_tools.append(
                {
                    "id": tc_id,
                    "title": tc.get("title", ""),
                    "kind": tc.get("kind", ""),
                    "status": tc.get("status", "completed"),
                    "raw_input": tc.get("raw_input"),
                    "content": content_str,
                    "raw_output": tc.get("raw_output"),
                }
            )

        summary = TurnSummary(
            prompt=self.recorder.prompt,
            response_text="".join(self.recorder.messages).strip(),
            thought_text="".join(self.recorder.thoughts).strip(),
            stop_reason=stop_reason,
            duration_seconds=duration,
            tool_call_count=len(self.recorder.tool_calls),
            file_operations=list(self.recorder.file_operations),
            permissions=list(self.recorder.permissions),
            tool_calls=recorded_tools,
            usage=dict(self.recorder.usage) if self.recorder.usage else None,
            transcript_markdown=self.recorder.to_markdown(),
        )
        self.history.append(summary)
        return summary

    def cumulative_tokens(self) -> int:
        """Return the sum of `used` tokens across all completed turns."""
        return sum(turn.tokens for turn in self.history)

    def cumulative_cost_usd(self) -> float:
        """Return the sum of `cost.amount` across all completed turns."""
        return sum(turn.cost_usd for turn in self.history)
