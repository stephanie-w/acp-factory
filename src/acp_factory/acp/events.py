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
    """Extract `(tokens_used, cost_usd)` from an ACP `usage_update` payload.

    Missing, malformed, or non-finite values are treated as zero so telemetry never
    crashes budget enforcement or falsely trips a cap.
    """
    if not usage:
        return 0, 0.0

    tokens = _non_negative_int(usage.get("used"))
    cost = usage.get("cost")
    if isinstance(cost, dict):
        amount: Any = cost.get("amount")
    else:
        amount = getattr(cost, "amount", None)
    return tokens, _non_negative_float(amount)


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

    @property
    def tokens(self) -> int:
        """Return the token count reported for this turn, or 0 when absent/invalid."""
        return extract_usage_metrics(self.usage)[0]

    @property
    def cost_usd(self) -> float:
        """Return the USD cost reported for this turn, or 0.0 when absent/invalid."""
        return extract_usage_metrics(self.usage)[1]


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

        summary = TurnSummary(
            prompt=self.recorder.prompt,
            response_text="".join(self.recorder.messages).strip(),
            thought_text="".join(self.recorder.thoughts).strip(),
            stop_reason=stop_reason,
            duration_seconds=duration,
            tool_call_count=len(self.recorder.tool_calls),
            file_operations=list(self.recorder.file_operations),
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
