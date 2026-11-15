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
