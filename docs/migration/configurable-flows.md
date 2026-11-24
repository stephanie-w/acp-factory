# Evolution Plan: Configurable Flows & Refactoring Flow

> **Status:** Implemented (Phases 0–4 Complete)  
> **Scope:** Evolving `acp-factory` from a single hardcoded PR workflow (`triage → spec → build → review → done`) into a declarative, multi-flow orchestrator—starting with a dedicated **Refactoring Flow** as the second concrete flow.

---

## 1. Motivation: Why One Flow Isn't Enough

The current 5-stage PR flow (`triage → spec → build → review → done`) is optimized for **behavioral changes** (features and bug fixes): it verifies single-PR sizing (`< ~400` changed lines), writes falsifiable `AC-1..N` acceptance criteria, builds via TDD, and reviews behavioral diffs before opening a PR.

Running structural refactors (and other non-feature workflows) through that flow exposes structural friction:

| Dimension | PR Flow (Feature / Fix) | Refactoring Flow |
| :--- | :--- | :--- |
| **Core invariant** | New behavior matches `## Acceptance Criteria` | **Zero unintended behavior change** (observable behavior preserved) |
| **Sizing heuristic** | `< ~400` changed lines in one PR | **Seam / blast radius & import-cycle risk** (pure code moves easily exceed 400 diff lines with zero behavioral risk) |
| **Pre-condition** | Spec approved at Human Gate 1 | **Baseline / characterization tests green** before moving code |
| **Planning stage** | `triage` + `spec` (what to build) | **Refactor Plan** (target seam, dependency direction, re-export/compatibility policy, split steps) |
| **Execution** | TDD red → green loop | **Behavior-preserving structural move** + seam/contract tests, verified after each step |
| **Review rubric** | "Does diff satisfy `AC-n`?" | "Did public APIs/semantics stay intact? Any hidden behavior change, dead code, or scope creep?" |
| **Delivery** | GitHub Issue → PR | GitHub Issue → PR, **or** branch-only local refactor |

Other natural future flows share the same need for different stage graphs and rubrics: incident/bug RCA, dependency upgrades, security audits, or docs sweeps.

---

## 2. Architectural Seam Analysis

The inner execution engine (`run_factory`) and stage configuration (`StageConfig`, `CheckConfig`, layered `resources/`) are **already flow-agnostic**—they execute `pre → agent → checks → retry`.

Coupling to the single PR flow is concentrated in four seams:

1. **Verdict & transition tree (`src/acp_factory/verdict.py`, `src/acp_factory/task.py`)**:
   - `ACTIONABLE_LABEL_TO_STAGE`, `ALL_FACTORY_STAGE_LABELS`, and `parse_stage_verdict()` hardcode the 5 PR stages and their verdict-to-label transitions.
2. **Stage-name branching (`src/acp_factory/runner/loop.py`)**:
   - `run_issue_stage()` branches directly on stage names (`if stage == "done"`, `if stage == "spec"`, `if stage == "review"`, `use_issue_worktree = stage in ("build", "review")`).
3. **GitHub admission & PR assumptions (`src/acp_factory/github.py`)**:
   - Issue listing, branch-contention checks (`_is_branch_contending`), and PR publishing assume `factory:pr-ready` is the terminal agent gate.
4. **Bundled templates (`src/acp_factory/resources/`, `src/acp_factory/cli.py`)**:
   - Scaffolding currently assumes a single default multi-stage config template.

---

## 3. Target Design: Declarative Flows Over Reusable Stages

Rather than building a general-purpose DAG engine, model a **Flow** as a small, declarative **state machine over reusable `[stages.<name>]` definitions**.

### A. Configuration Shape (`.factory/config.toml`)

Stages remain reusable building blocks; flows wire stages, transitions, worktree policy, and orchestrator side-effects:

```toml
# Reusable stages (existing mechanism, extended with opt-in capabilities)
[stages.refactor-plan]
read_only = true
persona = "agents/architect.agent.md"
skills = ["refactor-planner"]
prompt = "prompts/refactor-plan.md"
checks = ["validate-refactor-plan"]

[stages.refactor-apply]
read_only = false
worktree = true
persona = "agents/implementer.agent.md"
skills = ["refactor-builder"]
prompt = "prompts/refactor-apply.md"
checks = ["lint", "test"]

[stages.refactor-review]
read_only = true
worktree = true
require_clean_worktree = true
delta_review = true
inject_prior_notes_from = "refactor-apply"
persona = "agents/reviewer.agent.md"
skills = ["refactor-reviewer"]
prompt = "prompts/refactor-review.md"
checks = ["validate-review"]

# Built-in default flow (implicit when [flows] is omitted)
[flows.pr]
entry = "triage"
# ... equivalent to today's 5-stage PR state machine

# Second concrete flow: Refactoring
[flows.refactor]
entry = "refactor-plan"

[flows.refactor.transitions.refactor-plan]
READY = { label = "factory:plan-ready", action = "update_issue_block", block_mode = "replace" }
SPLIT_NEEDED = { label = "factory:needs-human", action = "update_issue_block", block_mode = "replace" }
NEEDS_HUMAN = { label = "factory:needs-human", action = "update_issue_block", block_mode = "replace" }

[flows.refactor.transitions.refactor-apply]
PASS = { label = "factory:refactor-review", block_mode = "preserve" }
FAIL = { label = "factory:needs-human", block_mode = "preserve" }

[flows.refactor.transitions.refactor-review]
ACCEPTED = { label = "factory:pr-ready", action = "publish_pr", block_mode = "clear_findings" }
REVISION_NEEDED = { label = "factory:refactor-apply", block_mode = "findings", max_rounds = 3, on_max_rounds_label = "factory:needs-human" }
NEEDS_HUMAN = { label = "factory:needs-human", block_mode = "append_section" }

[flows.refactor.transitions.done]
COMPLETED = { label = "factory:completed", action = "cleanup_worktree" }
```

### B. Fixed Set of Orchestrator Side-Effect Actions & Block Modes

Keep side-effects constrained to a small, auditable set of built-in primitives that any transition can reference:

- **Actions (`action`):**
  - `update_issue_block` — write updated stage output into `<!-- factory:start/end -->`
  - `publish_pr` — push branch, open/update PR, refresh managed PR description & review sections
  - `cleanup_worktree` — remove `.worktrees/issue-<id>` and local branch
  - `none` — local/artifact-only transition (no GitHub mutation)
- **Block update modes (`block_mode`, driving `verdict.py`):**
  - `preserve` — keep the existing `<!-- factory:start/end -->` block untouched (e.g., `build`, `refactor-apply`)
  - `replace` — replace the block with cleaned stage output (e.g., `spec`, `refactor-plan`)
  - `append_section` — wrap/append a headed report or escalation section (e.g., `triage`, review escalation)
  - `findings` — replace `<!-- factory:findings -->` in-place, increment `review_round`, and trip the `max_rounds` circuit breaker to `on_max_rounds_label` (default `factory:needs-human`)
  - `clear_findings` — strip `<!-- factory:findings -->` and record `last_reviewed_sha` on acceptance

### C. Opt-In Stage Capabilities (Replacing `if stage == ...`)

Replace hardcoded stage-name hooks in `run_issue_stage()` with declarative flags on `StageConfig`:

| Capability | Current Hardcoded Stage | Behavior |
| :--- | :--- | :--- |
| `worktree: bool` | `build`, `review` | Run inside persistent `.worktrees/issue-<id>` |
| `require_clean_worktree: bool` | `review` | Abort & bounce back if worktree has uncommitted changes |
| `delta_review: bool` | `review` | Snapshot `HEAD` and inject Round 2+ delta review brief |
| `inject_prior_notes_from: str \| None` | `review` (from `build`) | Attach latest implementation notes from the named stage |
| `revision_brief: bool` | `spec` | Inject incremental revision brief when updating an existing block |
| `orchestrator_only: bool` | `done` | Execute action directly without spawning an ACP agent |

### D. Flow Selection & Backward Compatibility

- **GitHub Issues (1:1 stage labels + `flow:<name>` selector):** Select a flow via `flow:<name>` (e.g., `flow:refactor`) paired with 1:1 `factory:<stage_key>` labels (`factory:refactor-plan`, `factory:refactor-apply`, `factory:refactor-review`) and shared gate/terminal labels (`factory:pr-ready`, `factory:done`, `factory:needs-human`, `factory:completed`). When `flow:<name>` is absent, infer the flow if the stage label uniquely belongs to a single configured flow; otherwise default to `pr` and fail fast on mismatch. Persist `flow=<name>` in `<!-- factory:meta -->` for observability and `history`/`stats`.
- **Local / CLI runs:** Multi-stage flow orchestration stays GitHub-Issue-driven first; local branch-only refactors use the existing standalone `acp-factory run --stage <stage>` and `acp-factory review --stage <stage>` commands.
- **Zero breaking changes:** Repositories with no `[flows.*]` section and issues with no `flow:*` label continue running the built-in `pr` flow identically to today.

---

## 4. Phased Migration Plan

### Phase 0: Structural Prerequisites (De-coupling the Seams)

Before introducing `FlowConfig`, finish isolating the modules that multi-flow orchestration depends on:

- [x] **#46** — Seal `acpterm` process-spawn seam behind `acp/spawn.py` *(merged in PR #83)*
- [x] **#47** — Extract stage-verdict state machine into `src/acp_factory/verdict.py` *(merged in PR #84)*
- [x] **#45** — Move embedded default personas & stage prompts from `cli.py` into `resources/` *(merged in PR #88)*
- [x] **#44** — Extract `RunArtifactStore` into `runner/artifacts.py` *(Part 1 of `loop.py` split)*
- [x] **#48** — Dedupe `list_actionable_issues` vs `list_all_factory_issues` in `github.py` *(single admission seam for flow/stage labels)*
- [x] **#85** — Move starter config & APM templates from `cli.py` into `resources/templates/` *(Part 2 of #45)*
- [x] **#86** — Extract prompt builders & budget caps into `runner/prompts.py` *(Part 2 of #44)*
- [x] **#87** — Extract `run_factory` into `runner/orchestrator.py` *(Part 3 of #44)*

### Phase 1: Prototype Refactoring Stages (Zero Orchestrator Changes)

Validate the refactoring prompts, skills, and review rubric *before* writing flow-engine code:

1. Author `refactor-plan`, `refactor-apply`, and `refactor-review` prompts, skills, and validators directly in `src/acp_factory/resources/` (keeping external generic skills such as `coding-ai-resources/skills/improve-codebase-architecture` strictly factory-agnostic).
2. Run them on real refactoring branches via `acp-factory run --stage refactor-apply` and `acp-factory review --stage refactor-review --create-pr`.
3. Capture lessons via `acp-factory run --stage retro` and refine the behavior-preservation rubric and validators.

### Phase 2: `FlowConfig` & Data-Driven Verdict Engine

1. Bundle `RunArtifactStore.persist()`'s 17 keyword-only run-metadata parameters (`issue_number`, `repo_slug`, `branch`, `head_sha`, etc.) into a `RunMetadata` dataclass before adding `flow` to `run.json` metadata (follow-up from `#44` review).
2. Add `FlowConfig` and `TransitionConfig` models to `src/acp_factory/config.py`, plus a built-in `DEFAULT_PR_FLOW` matching current behavior 1:1.
3. Refactor `src/acp_factory/verdict.py` so `parse_stage_verdict()` resolves target labels, actions, and round caps from the active `FlowConfig` rather than hardcoded `if stage == ...` branches.
4. Verify full regression parity across `tests/test_github_task.py` and `tests/test_fleet.py`.

### Phase 3: Stage Capabilities & Flow-Aware Dispatcher

1. Add opt-in stage capability fields (`worktree`, `require_clean_worktree`, `delta_review`, `inject_prior_notes_from`, `revision_brief`, `orchestrator_only`) to `StageConfig`.
2. Replace stage-name conditionals in `run_issue_stage()` with capability checks and transition action dispatch (`update_issue_block`, `publish_pr`, `cleanup_worktree`).
3. Update `github.py` issue admission (built on `#48`) to resolve an issue's active flow from `flow:<name>` labels (defaulting to `pr`) and derive actionable stage labels dynamically from the configured flows.

### Phase 4: Ship Bundled `refactor` Flow & CLI Ergonomics

1. Bundle `[flows.refactor]` and its stages/prompts/skills in `src/acp_factory/resources/`.
2. Add `--flow` CLI support and `acp-factory status` / `history` flow reporting.
3. Update `docs/workflow.md`, `docs/fleet-orchestration.md`, and `acp-factory init` templates.

---

## 5. Settled Design Decisions

1. **Custom & preset flows:** Support arbitrary `[flows.<name>]` definitions in `.factory/config.toml` from day one, with `pr` and `refactor` shipped as built-in presets that repositories can inherit or override. Overriding a built-in preset (e.g., `[flows.pr.transitions.spec]`) deep-merges per-stage transitions on top of the preset so single-transition tweaks do not require repeating the entire flow, while custom flows require an explicit `entry`.
2. **Label namespace & stage mapping:** Use `flow:<name>` as the flow selector + 1:1 `factory:<stage_key>` labels (`factory:refactor-plan`, `factory:refactor-apply`, `factory:refactor-review`) alongside shared gate/terminal labels (`factory:pr-ready`, `factory:done`, `factory:needs-human`, `factory:completed`). Keeps `factory:<stage>` 1:1 with `[stages.<stage>]` and `--stage` CLI flags with minimal code impact.
3. **Missing `flow:<name>` fallback:** When `flow:<name>` is absent, infer the flow if the stage label uniquely belongs to a single configured flow (e.g., `factory:refactor-plan` → `refactor`); otherwise default to `pr` and fail fast on mismatch.
4. **Human Gate 1 (`refactor` preset):** Pause at `factory:plan-ready` by default after `refactor-plan`, while allowing humans to skip planning by labeling `factory:refactor-apply` directly when a Refactor Contract is already written.
5. **Delivery modes:** Keep multi-stage flow orchestration GitHub-Issue-driven first; rely on existing standalone `acp-factory run --stage` and `acp-factory review --stage` for local branch-only refactors.
6. **Fleet admission & branch contention:** Derive `_is_branch_contending` from stage capabilities (`worktree = true`) and `publish_pr` gate labels (`factory:pr-ready`), so read-only stages and flows (e.g., `refactor-plan`, audits, RCA) never consume a branch slot.
7. **Verdict & issue-block updates:** Use a declarative `block_mode` enum on `TransitionConfig` (`preserve`, `replace`, `append_section`, `findings`, `clear_findings`) plus `max_rounds` and `on_max_rounds_label` (default `factory:needs-human`) so `verdict.py` stays 100% data-driven.
8. **Phase 1 prototyping location:** Author `refactor-plan`, `refactor-apply`, and `refactor-review` prompts, skills, and validators directly in `src/acp_factory/resources/` in Phase 1 (keeping `improve-codebase-architecture` strictly generic and factory-agnostic).
