"""Stream listener and turn event tracking built on acpterm's output and transcript modules."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from acpterm.output import _state as acpterm_output_state
from acpterm.transcript import TranscriptRecorder


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
