# `acp-factory` Workflow & Core Concepts

`acp-factory` is an automated software engineering orchestrator built on the **Agent Client Protocol (ACP)** via [`acpterm`](/home/stephanie/DEV/acpterm) and **APM (Agent Package Manager)**.

Inspired by `owainlewis/factory` and `owainlewis/software-factory`, it turns a GitHub Issue into a verified specification, builds and tests it in an isolated git worktree, reviews the branch with an independent reviewer persona, and delivers a reviewed Pull Request for a human to merge.

---

## 1. Core Concepts

### A. What is a Task?
In `acp-factory`, a **Task** (`TaskContext`) is a stateful work item backed by a **GitHub Issue** (or an ad-hoc CLI prompt for local one-off runs):
- **Identity**: GitHub Issue number (`#42`), title, and original description.
- **Living Specification**: Stored in-place inside the **GitHub Issue Body** between `<!-- factory:start -->` and `<!-- factory:end -->` markers.
- **Human Feedback**: Stored in **GitHub Issue Comments** (written strictly by humans).
- **Stage State**: Controlled by `factory:*` **GitHub Labels** on the issue.
- **Workspace**: An issue-dedicated git worktree (`.worktrees/issue-42` on branch `factory/issue-42`) that persists across `build` and `review` rounds until the `done` stage.

---

### B. Who Writes Where on GitHub (Zero Bot Comment Spam)

To keep issues clean and readable, `acp-factory` enforces a strict separation between the Issue Body and Issue Comments:

| GitHub Surface | Written By | Purpose |
| :--- | :--- | :--- |
| **Issue Body (Top)** | **Human** (or issue creator) | Original problem statement. Never overwritten. |
| **Issue Body (`<!-- factory:start -->` block)** | **Orchestrator** (and **Human** if editing directly) | Single canonical source of truth for Triage status, the Specification, `## Assumptions`, `## Decisions`, and any `## Open Questions`. Updated in-place via `gh issue edit`. |
| **Issue Comments** | **Human Only** | Human answers to open questions, review feedback, or revision requests. The orchestrator **never** posts bot comments on the issue thread. |
| **Issue Labels (`factory:*`)** | **Orchestrator** (on stage pass/halt) & **Human** (at Gates 1 & 2) | Drives the 5-stage workflow state machine. Agents cannot mutate labels directly. |
| **Pull Request** | **Orchestrator** (opens/updates PR after `review` passes) | Delivers the reviewed branch and PR description for **Human Gate 2**. Never auto-merged. The factory-managed description (`<!-- factory:pr-description:start/end -->`) and review report (`<!-- factory:review:start/end -->`) sections are refreshed in place on every bounce-back, preserving human-written text outside the markers. |

### C. Bot Identity & Credential Isolation

Automated runs must never masquerade as the human developer or borrow their interactive credentials:

- **Git attribution**: Issue worktrees (`.worktrees/issue-<id>`) enable `extensions.worktreeConfig` and set worktree-local `user.name = "acp-factory[bot]"` and `user.email = "acp-factory[bot]@users.noreply.github.com"`. The parent repository's interactive `user.name`/`user.email` are left untouched. Note: enabling `extensions.worktreeConfig` writes an idempotent repository-level flag into the shared `.git/config` so that per-worktree config files are honored; this flag is safe and applies to all worktrees, but it is the one repository-scoped state change the factory makes. Every subprocess, agent, and check also receives `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, and `GIT_COMMITTER_EMAIL` set to the same bot identity as an environment fallback (fully covering legacy Git builds without worktree config support).
- **GitHub CLI token isolation**: Set `FACTORY_GH_TOKEN` to a dedicated bot token. Internal `gh` calls (`run_gh`), workspace subprocesses, and the read-only `gh` shim export it as `GH_TOKEN`, overriding any inherited `GH_TOKEN` for that subprocess only. Interactive `gh auth` state in `~/.config/gh/` is never read or modified. When `FACTORY_GH_TOKEN` is unset, `gh` inherits the environment unchanged (personal login fallback for interactive development).
- **Issue body attribution banner**: The managed `<!-- factory:start --> ... <!-- factory:end -->` block displays the bot attribution immediately after the `<!-- factory:meta ... -->` line (or at the top of the block when no metadata is present):

  ```markdown
  <!-- factory:start -->
  <!-- factory:meta stage=spec tokens=1200 cost=0.02 turns=2 -->
  > 🤖 **acp-factory** | Stage: spec | Agent: opencode

  ## Specification
  ...
  <!-- factory:end -->
  ```

  `clean_spec_block()` strips the banner from the clean specification markdown used in PR bodies and downstream task prompts.

---

## 2. The 5-Stage GitHub Label State Machine

> 💡 See **[docs/github-project-setup.md](github-project-setup.md)** for tracking options, including direct **[GitHub Global Search Filters](github-project-setup.md#1-zero-setup-github-global-search-filters-recommended)** for your browser bookmarks and optional GitHub Projects (v2) boards.

```mermaid
stateDiagram-v2
    [*] --> Triage : Issue labeled `factory:triage`

    state "1. Triage (Agent)" as Triage
    Triage --> Spec : Valid & sized for 1 PR -> Orchestrator sets `factory:spec`
    Triage --> NeedsHuman : Duplicate / Too big / Unclear -> Orchestrator sets `factory:needs-human`

    state "2. Spec (Agent)" as Spec
    Spec --> SpecReady : Writes spec in Issue Body -> Orchestrator sets `factory:spec-ready`
    Spec --> NeedsHuman : Blocking open questions -> Writes `## Open Questions` in Issue Body & sets `factory:needs-human`
    NeedsHuman --> Spec : Human answers in comment (or edits body) & sets `factory:spec`

    state "🛑 HUMAN GATE 1 (Spec Approval)" as SpecReady
    SpecReady --> Build : Human approves spec & sets `factory:build`
    SpecReady --> Spec : Human comments with changes & sets `factory:spec`

    state "3. Build (Agent + Checks in Worktree)" as Build
    Build --> Review : Lint & tests pass, committed locally -> Orchestrator sets `factory:review`
    Build --> NeedsHuman : Max retry attempts exceeded -> Orchestrator sets `factory:needs-human`

    state "4. Review (Agent)" as Review
    Review --> Build : Changes needed -> Updates findings & sets `factory:build`
    Review --> PrReady : Accepted -> Pushes branch, opens/updates PR & sets `factory:pr-ready`

    state "🛑 HUMAN GATE 2 (PR Review & Manual Merge)" as PrReady
    PrReady --> Done : Human merges PR (`Closes #<id>` closes issue) or sets `factory:done`
    PrReady --> Build : Human requests changes & sets `factory:build`

    state "5. Done (Orchestrator Cleanup)" as Done
    Done --> [*] : Cleans up local `.worktrees/issue-<id>` & sets `factory:completed`
```

> **Fail-fast guard:** If a stage run's agent produces no output (the session aborts or fails before emitting any messages), the run fails immediately and the Issue transitions to `factory:needs-human` instead of advancing.

> **Human Gate 2 feedback ingestion:** When a human requests changes on the Pull Request and sends the issue back to `factory:build`, the orchestrator checks for an open PR on `factory/issue-<id>`. If found, it ingests the PR conversation comments (`gh pr view --json comments`), review submissions such as `CHANGES_REQUESTED` (`gh pr view --json reviews`), and the managed review section from the PR body (`gh pr view --json body`) into the task context. Build and review agents therefore see the reviewer's PR feedback, rendered chronologically alongside Issue comments and under `## Latest PR Review Findings`, without relying on a separate Issue comment.

### Stage Reference

1. **`triage` (`factory:triage`)**:
   - **Mode**: Read-only (`read_only = true`, `mode = "plan"`).
   - **Action**: Inspects the codebase and existing issues to check for duplicates and verify the ticket fits a single reviewable PR.
   - **Transition**: Updates the `<!-- factory:start -->` block in the Issue Body and transitions to `factory:spec` (or `factory:needs-human` if `duplicate` / `too_big`).

2. **`spec` (`factory:spec`)**:
   - **Mode**: Read-only (`read_only = true`, `mode = "plan"`).
   - **Action**: Reads the Issue Body + all human comments, analyzes the codebase, and writes the specification into the Issue Body (`## Behavior`, `## Files to Touch`, `## Assumptions`, `## Decisions`, `## Out of Scope`, `## Acceptance Criteria`).
   - **Handling Ambiguities (Two-Tier Rule)**:
     - **Reversible choices**: Recorded under `## Assumptions`; transitions to `factory:spec-ready` (**Human Gate 1**).
     - **Blocking questions**: Recorded under `## Open Questions` with recommended options in the Issue Body; transitions to `factory:needs-human`. Once the human answers in a comment and flips the label back to `factory:spec`, the next run folds those answers into `## Decisions` and clears `## Open Questions`.

3. **`build` (`factory:build`)**:
   - **Mode**: Read/Write inside isolated git worktree `.worktrees/issue-<id>` on branch `factory/issue-<id>`.
   - **Pre-Setup**: Runs configured `pre` commands (`apm install --frozen`, `uv sync`) once in the worktree.
   - **Action**: Implements the spec with unit/integration tests and runs sequential verification checks (`lint`, `test`) in a self-healing retry loop (up to `max_attempts`). Commits locally without pushing.
   - **Transition**: Sets `factory:review` on pass, or `factory:needs-human` if retries are exhausted.

4. **`review` (`factory:review`)**:
   - **Mode**: Read-only (`read_only = true`), using a skeptical Reviewer persona and `llm_judge` skill (optionally with a distinct model or agent for cross-model verification).
   - **Action**: Reviews the local branch diff, commit messages, and test coverage against the spec in the Issue Body.
    - **Transition**:
      - If **accepted**: Orchestrator pushes `factory/issue-<id>`, opens/updates the GitHub Pull Request (`Closes #<id>`), writes the review report into the PR body's managed `<!-- factory:review:start --> ... <!-- factory:review:end -->` section, and sets `factory:pr-ready` (**Human Gate 2**).
      - If **revision needed**: Writes the review findings into the managed `<!-- factory:review:start/end -->` section of the **PR body** (preserving human-written text outside the markers) and sets `factory:build` for another build round (up to 3 review rounds). If no PR exists yet, findings fall back to the Issue Body's `<!-- factory:findings:start/end -->` block so the builder still receives feedback; once a PR exists, any stale Issue findings block is stripped.
      - When an existing PR is updated (accepted re-runs or dry-run plans), the factory-managed description section (summary, specification, `Reviewed-SHA`) is also refreshed so it never goes stale; human-written text outside the managed markers is preserved.
      - The Issue Body's `<!-- factory:meta -->` comment continues to track `stage`, `review_round`, `last_reviewed_sha`, `tokens`, `cost`, and `turns` for every review outcome.

5. **`done` (`factory:done` or Issue `CLOSED`)**:
   - **Mode**: Orchestrator cleanup (no automatic PR merging).
   - **Action**: Triggered automatically when the GitHub Issue is `CLOSED` (e.g. when the human merges the PR containing `Closes #<id>` at **Human Gate 2**) or when `factory:done` is set. The orchestrator removes the local `.worktrees/issue-<id>` worktree and local branch, removes prior stage labels, and sets `factory:completed`.

---

## 3. Per-Stage Agent Customization, Domain-Agnostic Core & `apm` Skills

> 📖 **See [docs/model-selection.md](model-selection.md) for stage-by-stage model recommendations, reasoning strategies, and cross-model verification setups.**

### A. Domain-Agnostic Factory Core vs. Pluggable Domain Skills
`acp-factory` separates **universal factory governance** from **domain/tech-stack specifics**:

1. **Universal Factory Core (`.factory/personas/`, `.factory/prompts/`, and bundled `skills/`)**:
   - **Triage (`issue-triage`)**: Fact-checks claims against the repository, distinguishes `DUPLICATE` vs. `ALSO_FIXES`, and enforces single-PR sizing (**< ~400 changed lines** in one cohesive area) or outputs a build-order `### Suggested Split` (`STATUS: TOO_BIG`).
   - **Specification (`spec-writer`)**: Produces a falsifiable `## Acceptance Criteria` table (`| ID | Done when | How to check |` using `AC-1`, `AC-2`, ...) and applies the **cost-to-undo + dependency/compute/IAM filter** to decide between `## Assumptions` (`STATUS: SPEC_READY`) and `## Open Questions` (`STATUS: NEEDS_HUMAN` with 2-3 options + recommendation).
   - **Builder (`tdd-builder`)**: Turns each `AC-n` row into an executable local check/test first, implements minimal root-cause changes within `## Files to Touch`, probes bad/borderline inputs (including `NaN`/`Inf`/`null`/batch boundaries), and follows the 7-point quality standard.
   - **Reviewer (`code-reviewer` & `security-auditor`)**: Skeptically verifies `git diff` against every `AC-n` row, checks test effectiveness (revert-test heuristic: would existing tests fail if the fix were broken?), traces untrusted input from source to sink with concrete exploit/trigger proof, classifies risk (`LOW` / `MEDIUM` / `HIGH` with path elevation rules), and grades findings (`blocking`, `major`, `minor`).

2. **Adapting `AC-n` ("How to check") Across Software, MLOps & Cloud IaC**:
   - Because agents run inside isolated local git worktrees without live production cloud credentials or GPU clusters, `How to check` in `AC-n` is generalized by domain:
     - **Python / Application / ML Serving Endpoints**: `pytest` unit/contract tests using lightweight model stubs/fixtures and borderline payload checks (`NaN`, `Inf`, empty/oversized batches).
     - **ML & Data Pipelines (Kubeflow, Vertex, Airflow, Argo, CI)**: Offline DAG/workflow compilation and artifact schema dry-run checks.
     - **Cloud IaC & Containers (Terraform/OpenTofu, Helm, K8s, Docker)**: Static and plan-time local validation (`terraform fmt -check && terraform validate`, `tflint`, `helm template`, `kubeconform`) — **never** live cloud mutations.

3. **Bundled Core, Layered `resource_dirs`, & `apm` Auto-Discovery**:
   - Core orchestration personas, stage prompts, and skills/validators (`issue-triage`, `spec-writer`, `tdd-builder`, `code-reviewer`, `security-auditor`, `retro`) are bundled inside `acp_factory/resources/` as the lowest-precedence fallback.
   - Additional catalogs (e.g., org policy auditors or custom overrides) can be layered via `resource_dirs = [...]` (or legacy `resources_dir`), `FACTORY_RESOURCES_DIRS` / `FACTORY_RESOURCES_DIR`, or `.factory/`.
   - Target workspaces bring their own `apm.yml` + `apm.lock.yaml` and run `apm install --frozen` in `pre` / `setup`. When `discover_apm_resources = true` (the default), `acp-factory` auto-discovers deployed domain skills and `*.instructions.md` files from `apm.lock.yaml` (`deployed_files`) and standard directories (`.agents/skills/`, `.apm/skills/`, `.github/instructions/`, `apm_modules/`)—injecting them lazily under `# Available Workspace Skills` and `# Domain & Policy Instructions` with YAML frontmatter summaries.

### B. Stage Configuration Reference

Each `[stages.<name>]` section accepts the following stage-specific overrides:

| Key | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `agent` | string | inherited | acpterm agent alias for this stage. |
| `command` | string or list | inherited | Explicit agent command. |
| `model` | string | inherited | LLM model override. |
| `mode` | string | inherited | Agent mode (e.g. `plan`, `code`). |
| `read_only` | bool | inherited | Enforce read-only mode. |
| `persona` | string | inherited | Persona Markdown path or inline text. |
| `skills` | list | inherited | Skill paths or APM skill names. |
| `prompt` | string | inherited | Stage prompt Markdown path or inline text. |
| `pre` | list | inherited | Pre-stage setup commands. |
| `checks` | list | inherited | Check names or inline check definitions. |
| `timeout` | int | inherited | Per-turn timeout in seconds. |
| `env` | table | inherited | Stage-specific environment variables. |
| `strip_env` | list | inherited | Additional environment variable names to strip. |
| `keep_env` | list | inherited | Environment variable names to preserve even if they match sensitive credential patterns; `strip_env` wins on conflict. |
| `thought_max_chars` | int or null | `null` | Agent thought visibility/length in transcripts. `0` omits thoughts from `transcript.md` and `transcript.summary.json`; `N > 0` truncates to `N` characters; unset keeps full thoughts in `transcript.md`/`transcript.json` and the default 500-character `thought_preview`. Full reasoning is always preserved in `transcript.json` for `retro`. |

A top-level `thought_max_chars` acts as the global default and is overridden by any stage-level value:

```toml
thought_max_chars = 500

[stages.build]
thought_max_chars = 0

[stages.review]
thought_max_chars = 1000
```

---

## 4. Standalone Branch Review & Pull Request Creation (`acp-factory review`)

When you have an existing working branch in a repository without a GitHub Issue, you can run the standalone `review` command:

```bash
# Review current branch in read-only mode and preview PR creation:
acp-factory review "Short claim/description of what the branch does" \
  --workspace /path/to/repo \
  --create-pr \
  --dry-run

# If REVISION_NEEDED, run a fresh builder revision round on the branch:
acp-factory run \
  "Revision round: Read .factory/runs/<run-id>/pr-<branch>-body.md, fix [blocking]/[major] findings, and commit." \
  --stage build \
  --skill tdd-builder \
  --workspace /path/to/repo
```

---

## 5. Safe Iteration with `--dry-run` & Read-Only Agent `gh` Guardrail

While tuning personas, stage prompts, and `apm` skills, you can run in **Dry-Run Mode** (`--dry-run` on the CLI or `dry_run = true` in `.factory/config.toml`):

1. **Read-Only Agent `gh` Shim (Always Active)**:
   - Worker and reviewer agents are spawned with a `gh` PATH shim that allows read subcommands (`gh issue view`, `gh issue list`, `gh pr view`, `gh search`) and **blocks all mutating `gh` subcommands** (`edit`, `create`, `comment`, `close`, `merge`).
2. **Human-Only GitHub Writes (`--dry-run`)**:
   - `acp-factory issue 42 --dry-run` fetches Issue `#42` and runs the full ACP stage live in your terminal.
   - Instead of mutating GitHub, it saves the proposed Issue Body to `.factory/runs/<run-id>/issue-42-body.md` (and PR body if applicable) and prints a **Manual GitHub Commands** panel with the exact `gh issue edit` / `gh pr create` commands for you to execute manually.

---

## 6. Engineering Retrospectives (`retro`)

Retrospectives close the continuous improvement loop by having an architect agent analyze completed runs, multi-attempt failures, and check logs to propose concrete improvements to the agent environment (`AGENTS.md`, `.factory/config.toml`, automated checks, and prompts).

### A. How to Run a Retrospective

You can trigger a retrospective directly via the CLI:

```bash
# Run retro against recent .factory/runs/ in the current workspace:
acp-factory run --stage retro

# Target a specific workspace:
acp-factory run --stage retro --workspace /path/to/repo

# Run an ad-hoc retrospective focusing on a specific run ID:
acp-factory run \
  "Execute retro against run .factory/runs/20261112-120000-abc123 and AGENTS.md" \
  --stage retro
```

### B. Structured Run Artifacts & Fast Querying with `jq`

Each execution stores structured artifacts under `.factory/runs/<run-id>/`:
- **`run.json`**: Top-level run metadata (attempts used, total duration, stage outcome, and failure reason). Its `usage` block records only the token, cost, and turn consumption of this single run; cumulative issue totals across stages are tracked separately in the issue's `<!-- factory:meta -->` block.
- **`attempt-<N>/transcript.summary.json`**: Condensed event snapshot with truncated tool output previews and omitted raw scratchpads. Tailored for token efficiency during `retro`.
- **`attempt-<N>/transcript.json`**: Complete structured JSON event tree containing all tool invocations, inputs, file operations, permissions, and duration.
- **`attempt-<N>/transcript.md`**: Full raw Markdown transcript for human deep dives.
- **`attempt-<N>/check-<N>-<slug>.log`**: Raw stdout/stderr execution output from verification checks.
- **`report.md`**: Standalone clean Markdown review report (verdict, acceptance-criteria verification, findings, and security audit) written when the `review` stage completes. Non-review stages do not produce it.
- **`implementation-notes.md`**: Standalone clean Markdown implementation summary (acceptance-criteria test mapping, Before/After evidence, Door & Blast Radius, and tested edge cases) extracted from the builder's response when the `build` stage completes. Non-build stages, or build responses without an `### Implementation Summary`, do not produce it.

Every completed non-dry-run execution with at least one attempt refreshes the run directory pointer, regardless of stage:
- **`.factory/runs/latest`**: Symlink to the newest run directory (all stages).

Stage-specific pointers surface the newest report or notes without scanning run IDs:
- **`.factory/latest-report.md`**: Symlink to `runs/<run-id>/report.md` (or a direct copy when the platform refuses symlinks), written for `review` runs.
- **`.factory/latest-build-notes.md`**: Symlink to `runs/<run-id>/implementation-notes.md` (or a direct copy when the platform refuses symlinks), written for `build` runs.

```bash
cat .factory/runs/latest/report.md
# or
cat .factory/latest-report.md

cat .factory/runs/latest/implementation-notes.md
# or
cat .factory/latest-build-notes.md
```

You can easily query and analyze transcripts using `jq`:

```bash
# Quick attempt summary (duration, stop reason, tool count, tokens):
jq '.summary' .factory/runs/<run-id>/attempt-1/transcript.summary.json

# List all tool calls with status and title:
jq '.tool_calls[] | {title: .title, status: .status}' .factory/runs/<run-id>/attempt-1/transcript.json

# Find failed tool calls:
jq '.tool_calls[] | select(.status != "completed")' .factory/runs/<run-id>/attempt-1/transcript.json

# Calculate tool frequencies (tool economy):
jq '.tool_calls | group_by(.title) | map({tool: .[0].title, count: length})' .factory/runs/<run-id>/attempt-1/transcript.json

# List unique files touched during the turn:
jq '.file_operations | map(.path) | unique' .factory/runs/<run-id>/attempt-1/transcript.summary.json
```

### C. The 7 Retrospective Categories

The `retro` agent evaluates session evidence across 7 specific areas:
1. **Navigation**: Did the agent waste turns hunting for files or entrypoints? (*Fix*: Add a 1–2 line pointer in `AGENTS.md`).
2. **Automated Checks (Highest Leverage)**: Can a recurring error be caught by a deterministic check (`just`, linter rule, or validator) rather than a prose rule?
3. **Coding Standards & Reviewer Rubric**: Did the reviewer catch genuine bugs, or did it flag noisy subjective patterns?
4. **`AGENTS.md` Diet**: Are there bloated task-specific instructions in steering files that should be trimmed or moved to checks?
5. **Tool Economy**: Were expensive tools or un-truncated commands run repeatedly?
6. **No-ops**: Are there instructions that agents consistently ignore?
7. **Information Access**: Was the agent blocked by missing local environment context or documentation?

Output is formatted as a ranked Markdown report (`HIGH`, `MEDIUM`, `LOW`) with concrete unified diff proposals for human review.
