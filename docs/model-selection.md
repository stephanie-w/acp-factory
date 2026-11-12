# Model Selection & Reasoning Strategy Guide

This guide defines the **model selection strategy** across `acp-factory` stages, balancing reasoning depth, tool-call velocity, token budgets, and verification integrity.

---

## 1. Cognitive Postures & Context Pressure

Different stages in the 5-stage software factory lifecycle have vastly different cognitive requirements and context pressures:

```
[ triage ]          High-volume, fast classification & fact-checking
    │
[ spec ]            Architectural reasoning & falsifiable acceptance criteria (AC-n)
    │
[ build ]           High-speed, iterative coding & check self-healing loop
    │
[ review ]          Skeptical critique & security auditing (Cross-Model!)
    │
[ retro ]           Deep system analysis over massive run transcripts (400KB+)
```

* **Implementation (`build`)** has the **highest context pressure and tool frequency**. The agent explores the codebase, edits multiple files, runs shell checks, and digests stack traces. Running a model with heavy, unbounded reasoning here introduces 2–3 minute delays on *every tool call*, frequently causing ACP turn timeouts (600s).
* **Verification (`review`)** has the **lowest context pressure**. The reviewer only inspects a bounded diff (`git diff`) against the acceptance criteria table. Deep reasoning and critique models excel here because they don't perform iterative file edits.
* **Planning (`spec`)** requires **rigorous structured thinking**. Hallucinated assumptions or poorly defined criteria compound into failed builds.
* **Triage (`triage`)** is **high-volume and cost-sensitive**. It needs quick claim verification against code without burning frontier reasoning budgets.
* **Retrospective (`retro`)** demands **massive context windows** to ingest multi-turn run transcripts, check logs, and configuration files.

---

## 2. Model Selection Matrix

| Stage | Cognitive Posture | Recommended Model | Fast / Budget Alternative | Primary Rationale |
| :--- | :--- | :--- | :--- | :--- |
| **`triage`** | High-volume classifier | **Claude 3.5 Haiku** or **Gemini 2.0 Flash** | **DeepSeek V3 / Flash** (no reasoning) | High speed, low cost; fast claim fact-checking and single-PR sizing (<400 lines) without reasoning overhead. |
| **`spec`** | Architectural planner | **Claude 3.7 Sonnet** or **Claude 3.5 Sonnet** | **DeepSeek V4 Pro** / **GPT-4o** | Excellent structured formatting, strict falsifiable `AC-n` tables, and reliable cost-to-undo question filtering. |
| **`build`** | TDD builder & fixer | **Claude 3.7 Sonnet** or **DeepSeek Flash / V3** | **GPT-4o** | Fast file-editing loop, rapid self-healing recovery, zero reasoning delay. **Avoid heavy reasoning models here.** |
| **`review`** | Skeptical judge | **o3-mini** (high reasoning) or **DeepSeek Reasoner / V4 Pro** | **Claude 3.5 Sonnet** (if not built by Claude) | Dedicated reasoning models excel at finding subtle logic flaws, security sinks, and missing edge cases in a bounded diff. |
| **`retro`** | Log analyzer | **Gemini 2.0 Flash / Pro** | **Claude 3.5 Sonnet** (200k) | Ingests 400KB+ transcripts and logs with high needle-in-a-haystack retrieval and low token cost. |

---

## 3. The Golden Rule: Cross-Model Review

> [!IMPORTANT]
> **Never let an agent review its own work with the same model family.**
> Models from the same family share the same cognitive biases, blind spots, and training data assumptions. If Claude builds the feature, have DeepSeek or OpenAI review it. If DeepSeek builds the feature, have Claude or OpenAI review it.

In `.factory/config.toml`:
```toml
# Fast, surgical implementation
[stages.build]
agent = "opencode"
model = "anthropic/claude-3-7-sonnet"

# Independent, skeptical review
[stages.review]
agent = "opencode"
model = "openai/o3-mini"
```

---

## 4. Stage-by-Stage Guidelines

### 📥 1. Triage (`factory:triage`)
- **Goal**: Fact-check claims against the codebase, classify issues (`DUPLICATE`, `ALSO_FIXES`), and enforce single-PR sizing (<400 lines).
- **Ideal Model**: Fast, affordable flash models.
- **Why**: Triage runs frequently across many incoming issues. Using heavy reasoning models here adds latency and cost with zero quality benefit.

### 📝 2. Specification (`factory:spec`)
- **Goal**: Read the issue and human comments, produce the living spec in the issue body, and draft a falsifiable `## Acceptance Criteria` table (`AC-1`, `AC-2`, ...).
- **Ideal Model**: Frontier architectural models with strong instruction-following.
- **Why**: The spec defines the contract for the builder and reviewer. A disciplined model avoids hallucinations, accurately separates reversible assumptions from blocking open questions, and writes clean acceptance tables.

### 🔨 3. Builder (`factory:build`)
- **Goal**: Turn `AC-n` rows into executable tests, implement the minimal changes, and run the self-healing retry check loop (`lint`, `test`).
- **Ideal Model**: Fast coding models with direct, low-latency tool execution.
- **⚠️ Critical Warning on Reasoning Models**:
  Avoid full reasoning models (such as DeepSeek R1 with max thinking effort) in the `build` stage. Because the agent executes multiple tool calls (reading files, editing blocks, running `uv run pytest`), reasoning for 2–3 minutes between each action quickly breaches the 600-second turn timeout (`timeout = 600`). Use non-reasoning or low-thinking variants (`deepseek-chat`, `deepseek-flash`, `claude-3-7-sonnet`).

### 🔍 4. Reviewer (`factory:review`)
- **Goal**: Skeptically critique the `git diff` against every `AC-n` criterion, trace untrusted inputs from source to sink, and check the revert-test heuristic.
- **Ideal Model**: Deep reasoning models (`deepseek-reasoner`, `deepseek-v4-pro`, `o3-mini`).
- **Why**: Review is read-only and examines a static diff. Latency is acceptable, and deep reasoning models are exceptional at spotting subtle edge cases, off-by-one errors, and architectural boundary violations.

### 🔄 5. Retrospective (`factory:retro`)
- **Goal**: Analyze multi-attempt run transcripts, check execution logs, and propose minimal environment or check improvements.
- **Ideal Model**: High-context models with strong synthesis capabilities (e.g. Gemini 2.0 Flash / Pro).
- **Why**: Session transcripts can exceed 400KB. Models with 1M+ context windows process the entire session history without truncation.

---

## 5. Configuration Recipes

### Recipe A: Unified DeepSeek Stack (Fast Build + Deep Review)
Ideal when utilizing DeepSeek API or local models:

```toml
[stages.triage]
agent = "opencode"
model = "deepseek/deepseek-flash"
read_only = true

[stages.spec]
agent = "opencode"
model = "deepseek/deepseek-v4-pro"
read_only = true

[stages.build]
agent = "opencode"
model = "deepseek/deepseek-flash"     # Fast tool loop; prevents 600s timeouts
timeout = 600
read_only = false

[stages.review]
agent = "opencode"
model = "deepseek/deepseek-v4-pro"   # Deep reasoning verification
timeout = 900
read_only = true

[stages.retro]
agent = "opencode"
model = "deepseek/deepseek-flash"
read_only = true
```

---

### Recipe B: Multi-Provider Cross-Model Stack (Maximum Rigor)
Ideal for high-stakes production features:

```toml
[stages.triage]
agent = "opencode"
model = "anthropic/claude-3-5-haiku"
read_only = true

[stages.spec]
agent = "opencode"
model = "anthropic/claude-3-7-sonnet"
read_only = true

[stages.build]
agent = "opencode"
model = "anthropic/claude-3-7-sonnet"
timeout = 900
read_only = false

[stages.review]
agent = "opencode"
model = "openai/o3-mini"             # Cross-model skeptical review
timeout = 900
read_only = true

[stages.retro]
agent = "opencode"
model = "google/gemini-2.0-flash"     # Massive context ingestion
read_only = true
```

---

## 6. Managing Turn Timeouts & Reasoning Delays

If you choose to run models with extended reasoning in implementation stages:

1. **Bump the stage timeout**:
   Increase `timeout` from the default `600` (10 minutes) to `900` or `1200` (15–20 minutes) in `.factory/config.toml`:
   ```toml
   [stages.build]
   timeout = 1200
   ```
2. **Use effort suffixes where supported**:
   Gateways like OpenRouter or LiteLLM allow specifying reasoning intensity directly in the model ID:
   ```toml
   model = "openai/o3-mini:low"       # Reduces reasoning delay on build
   model = "openai/o3-mini:high"      # Maximizes critique depth on review
   ```
3. **Preserve worktrees on timeout**:
   Keep `keep_worktree_on_failure = true` in the top-level `[retry]` configuration. If an agent turn times out, any code and tests written prior to the timeout remain safely preserved in `.worktrees/issue-<id>`.
