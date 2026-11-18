# Exploring `transcript.summary.json` with `jq`

Each attempt in `.factory/runs/<run-id>/attempt-<N>/` writes three transcript files:

- `transcript.summary.json` — compact structured summary (previews, tool calls, file operations, permissions, usage/duration).
- `transcript.json` — full raw ACP event/tool-call payload.
- `transcript.md` — human-readable Markdown log.

`transcript.summary.json` is designed for fast inspection with `jq`.

## Pick a File

Set a shell variable to the summary you want to inspect (for example, the newest run's first attempt):

```bash
SUMMARY="$(ls -td .factory/runs/20* | head -n 1)/attempt-1/transcript.summary.json"
```

## Top-Level Structure

```bash
jq 'keys' "$SUMMARY"
```

Fields:

- `summary`: `{duration_seconds, stop_reason, tool_call_count, file_operations_count, tokens, cost_usd}`
- `prompt_preview`, `thought_preview`, `response_preview`: truncated text previews
- `tool_calls[]`: `{id, title, kind, status, raw_input, output_preview, raw_output}`
- `file_operations[]`: file edits/writes recorded by the tracker
- `permissions[]`: `{title, kind, outcome}`

### JSON Schema

Generated from `TurnSummary.to_condensed_dict()` in [`src/acp_factory/acp/events.py`](../src/acp_factory/acp/events.py):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "TranscriptSummary",
  "type": "object",
  "required": [
    "summary",
    "prompt_preview",
    "response_preview",
    "thought_preview",
    "tool_calls",
    "file_operations",
    "permissions"
  ],
  "additionalProperties": false,
  "properties": {
    "summary": {
      "type": "object",
      "required": [
        "duration_seconds",
        "stop_reason",
        "tool_call_count",
        "file_operations_count",
        "tokens",
        "cost_usd"
      ],
      "additionalProperties": false,
      "properties": {
        "duration_seconds": { "type": "number" },
        "stop_reason": { "type": "string" },
        "tool_call_count": { "type": "integer" },
        "file_operations_count": { "type": "integer" },
        "tokens": { "type": "integer" },
        "cost_usd": { "type": "number" }
      }
    },
    "prompt_preview": { "type": "string" },
    "response_preview": { "type": "string" },
    "thought_preview": { "type": "string" },
    "tool_calls": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "title", "kind", "status"],
        "additionalProperties": false,
        "properties": {
          "id": { "type": ["string", "null"] },
          "title": { "type": "string" },
          "kind": { "type": "string" },
          "status": { "type": "string" },
          "raw_input": {},
          "output_preview": { "type": "string" },
          "raw_output": {}
        }
      }
    },
    "file_operations": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": { "type": ["string", "null"] }
      }
    },
    "permissions": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": { "type": "string" },
        "properties": {
          "title": { "type": "string" },
          "kind": { "type": "string" },
          "outcome": { "type": "string" }
        }
      }
    }
  }
}
```

## Common `jq` Queries

### 1. Run Overview (Duration, Stop Reason, Counts, Usage)

```bash
jq '.summary' "$SUMMARY"
```

Include the truncated prompt and response previews:

```bash
jq '{summary, prompt_preview, response_preview}' "$SUMMARY"
```

### 2. List All Tool Calls in Order

```bash
jq -r '.tool_calls | to_entries[] | "\(.key)\t[\(.value.kind)/\(.value.status)]\t\(.value.title)"' "$SUMMARY"
```

### 3. Count Tool Calls by `kind` and `status`

```bash
jq '.tool_calls | group_by(.kind) | map({kind: .[0].kind, count: length})' "$SUMMARY"
```

```bash
jq '.tool_calls | group_by(.status) | map({status: .[0].status, count: length})' "$SUMMARY"
```

### 4. Find Failed or Non-Zero-Exit Tool Calls

```bash
jq '.tool_calls[] | select(.status != "completed" or (.raw_output.metadata.exit? // 0) != 0) | {id, title, kind, status, exit: .raw_output.metadata.exit?, output_preview}' "$SUMMARY"
```

### 5. Filter Tool Calls by Title or Kind

Only shell/command executions (`kind == "execute"`):

```bash
jq -r '.tool_calls[] | select(.kind == "execute") | "\(.status)\t\(.title)"' "$SUMMARY"
```

Search titles with a regex (case-insensitive):

```bash
jq '.tool_calls[] | select(.title | test("git|gh|pytest"; "i")) | {id, title, status, output_preview}' "$SUMMARY"
```

### 6. Inspect One Tool Call in Detail (by Index or `id`)

Using the index from query #2 (for example, index `0`):

```bash
jq '.tool_calls[0]' "$SUMMARY"
```

Print only its `output_preview` as raw text:

```bash
jq -r '.tool_calls[0].output_preview' "$SUMMARY"
```

### 7. Inspect File Operations & Permissions

```bash
jq '.file_operations' "$SUMMARY"
```

```bash
jq '.permissions | group_by(.outcome) | map({outcome: .[0].outcome, count: length, titles: (map(.title) | unique)})' "$SUMMARY"
```

List any denied permissions:

```bash
jq '.permissions[] | select(.outcome != "approved")' "$SUMMARY"
```

### 8. Compare Across All Attempts in a Run

```bash
RUN_DIR="$(ls -td .factory/runs/20* | head -n 1)"
jq -s 'to_entries[] | {attempt: (.key + 1), summary: .value.summary}' "$RUN_DIR"/attempt-*/transcript.summary.json
```

---

## Reproducible Before/After (A/B) Token & Performance Comparisons

When evaluating prompt changes, CLI environment flags, tool shims, or model swaps, use the built-in `history`, `stats`, and `inspect` commands alongside `jq` to compare runs on the same stage (or same issue via `--dry-run`).

### 1. Identify Candidate Run IDs

List recent runs in chronological order (`acp-factory history` sorts newest-first from `.factory/runs/20*/run.json`):

```bash
uv run acp-factory history --limit 15
```

Or filter runs for a specific stage (e.g., `triage`, `spec`, `build`, `review`) directly from `.factory/runs/`:

```bash
for d in $(ls -td .factory/runs/20* | head -n 15); do
  jq -c '{run_id, stage, issue_id, model, succeeded, attempts_used, duration_seconds, tokens: .usage.total_tokens}' "$d/run.json"
done
```

### 2. Controlled Same-Issue A/B Testing with `--dry-run`

To compare two configurations, prompts, or models on the **exact same GitHub Issue and stage** without mutating GitHub labels or posting comments:

1. **Run Baseline (A)** in `--dry-run` mode (or on a specific model via `--model`):
   ```bash
   uv run acp-factory issue <ISSUE_ID> --stage triage --dry-run
   ```
   *(Note: `--dry-run` writes its output artifacts to `.factory/runs/dry-run/`. If you want full persisted run directories under `.factory/runs/<timestamp>-<id>/`, run without `--dry-run` on a test issue or copy `.factory/runs/<run-id>` before re-running.)*
2. **Apply Optimization / Switch Model (B)** and run the same stage again.

### 3. Compare Token Breakdown & Tool Output Volume (`acp-factory stats`)

Run `acp-factory stats <run-id>` on the Before (`RUN_A`) and After (`RUN_B`) run IDs:

```bash
for r in <RUN_A> <RUN_B>; do
  echo "=== $r ==="
  uv run acp-factory stats "$r"
done
```

Even when an ACP agent does not emit provider-level `usage_update` token telemetry (`Tokens: 0`), `acp-factory stats` computes a deterministic character-based token estimate (`~4 chars/token`) split across four buckets:

| Bucket | What It Measures | Which Optimizations Affect It |
| :--- | :--- | :--- |
| **`prompt`** | Initial task + persona + stage instructions | Lazy skill/template attachment ([#50](https://github.com/stephanie-w/acp-factory/issues/50)), concise prompt headers |
| **`thought`** | Intermediate agent reasoning & tool-loop narration | Scoped caveman / quiet tool-loop directives ([#53](https://github.com/stephanie-w/acp-factory/issues/53)), `thought_max_chars` |
| **`response`** | Final assistant stage report / verdict text | Terse final response rules ([#53](https://github.com/stephanie-w/acp-factory/issues/53)) |
| **`tool output`** | Cumulative characters returned by `read`, `execute`, etc. | Low-noise CLI env & `RIPGREP_CONFIG_PATH` ([#49](https://github.com/stephanie-w/acp-factory/issues/49)), noisy-path guardrails ([#51](https://github.com/stephanie-w/acp-factory/issues/51)), `PATH` tool shims ([#52](https://github.com/stephanie-w/acp-factory/issues/52)) |

### 4. Side-by-Side JSON Comparison with `jq`

To compare exact character and estimated-token metrics between two or more runs in a single table using `acp-factory stats <run-id> --json`:

```bash
for r in <RUN_A> <RUN_B>; do
  uv run acp-factory stats "$r" --json | jq '{
    transcript: (.transcript | split("/")[-3]),
    duration_s: .duration_seconds,
    tool_calls: .tool_call_count,
    failed_calls: .failed_count,
    tool_output_chars: .total_output_chars,
    est_tokens_total: .estimated_tokens,
    breakdown: .estimated_tokens_breakdown
  }'
done
```

### 5. Drill Into the Heaviest Tool Calls (`acp-factory inspect`)

When a run has high `tool_output` or `thought` tokens, use `acp-factory inspect` to pinpoint the exact cause:

```bash
# List all tool calls with kind, status, exit code, and duration
uv run acp-factory inspect <RUN_ID>

# Filter only shell executions or failed calls
uv run acp-factory inspect <RUN_ID> --kind execute
uv run acp-factory inspect <RUN_ID> --failed

# Inspect the full input and output of a specific tool call (e.g. #7 from Top 10 Largest Outputs)
uv run acp-factory inspect <RUN_ID> --call 7

# Inspect intermediate thoughts/narration or final response
uv run acp-factory inspect <RUN_ID> --thoughts
uv run acp-factory inspect <RUN_ID> --response
```

