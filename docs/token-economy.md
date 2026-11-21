# Token Economy, Prompt Caching & Budget Caps

This document explains how `acp-factory` measures, reports, and caps token usage across agent runs.

## 1. Where Token Numbers Come From

ACP agents can report usage through two mechanisms:

1. **`usage_update` notifications** (during a turn): reports current context-window occupancy (`used / size`) and cumulative `cost`.
2. **`PromptResponse.usage`** (at turn end): reports provider-level token counts across all API calls in the turn (`total_tokens`, `input_tokens`, `output_tokens`, `thought_tokens`, `cached_read_tokens`, `cached_write_tokens`).

### What You See in Each Place

| Surface | What It Shows | Notes |
|---|---|---|
| **Terminal `[tokens]` line** (`acpterm`) | Raw `PromptResponse.usage.total_tokens` | Includes `cached_read_tokens`. |
| **Terminal `[context]` line** (`acpterm`) | `used / size` from `usage_update` | Context-window occupancy at turn end, not total billed tokens. |
| **`attempt-N/transcript.json`** | Full raw `usage` dictionary | Preserves `total_tokens`, `cached_read_tokens`, `input_tokens`, `output_tokens`, `thought_tokens`, `used`, `size`, and `cost`. |
| **`run.json` & `acp-factory history`** | Extracted uncached tokens (`extract_usage_metrics`) | Used for budget enforcement and per-run summaries. |
| **`acp-factory stats`** | Provider tokens + `~4 chars/token` estimate | Breaks down estimated tokens into `prompt`, `thought`, `response`, and `tool_output`. |

## 2. Server-Side Prompt Caching (`cached_read_tokens`)

In a multi-step tool loop, each tool call triggers another model request with the growing conversation prefix. Providers with server-side prompt caching (DeepSeek, Anthropic, Gemini, OpenAI) serve that prefix from cache and report it under `cached_read_tokens`.

- Because the prefix is re-read on every tool-call step, `total_tokens` can reach hundreds of thousands of tokens even when the context window (`used`) stays around ~40k and the cost is only `$0.01`.
- In `src/acp_factory/acp/events.py`, `extract_usage_metrics` computes effective uncached tokens in this priority order:
  1. `total_tokens` / `totalTokens` / `tokens`, **minus `cached_read_tokens`** (clamped at `>= 0`).
  2. `input_tokens + output_tokens` (when `total_tokens` is absent).
  3. `used` (fallback for agents that only emit `usage_update` context snapshots).

This prevents prompt-cached tool loops from falsely tripping `max_tokens_per_run`, while `max_cost_usd` continues to guard total dollar spend (including cached reads).

## 3. Per-Run Caps vs. Cross-Stage Auditing

- **Enforcement (`[budget]`)**: `max_tokens_per_run`, `max_cost_usd`, and `max_turns` are all evaluated **per run** (`run_usage`), so prior stages (`triage`, `spec`) on an issue do not eat into a `build` or `review` run's budget.
- **Auditing (`<!-- factory:meta -->`)**: Cumulative tokens, cost, and turns across all stages of an issue are still persisted in the issue's `<!-- factory:meta -->` block for visibility.

When a per-run cap is breached, execution halts deterministically and (in GitHub issue mode) transitions the issue to `factory:needs-human` with a `## Budget Escalation` summary.

## 4. Measuring & Optimizing Token Usage

- **Benchmarking**: See [docs/transcript-inspection.md](transcript-inspection.md) for A/B comparison recipes with `acp-factory history`, `acp-factory stats`, and `acp-factory inspect`.
- **Model routing**: See [docs/model-selection.md](model-selection.md) for choosing fast vs. high-reasoning models per stage (reasoning models emit `thought` tokens on every tool-call round-trip).
