# acp-factory

An automated multi-stage software engineering runner built on the **Agent Client Protocol (ACP)** via [`acpterm`](/home/stephanie/DEV/acpterm) and **APM (Agent Package Manager)**.

Inspired by `owainlewis/factory` and `owainlewis/software-factory`, `acp-factory` runs ACP-compliant coding agents (`opencode`, `kiro`, `copilot`, `agy`, etc.) across a **5-stage GitHub label workflow** with per-stage personas, `apm` skills, deterministic verification checks, and two mandatory human gates:

```text
[triage] -> [spec] -> (🛑 Human Gate 1) -> [build + checks] -> [review] -> (🛑 Human Gate 2: Manual Merge) -> [done]
```

📖 **See [docs/workflow.md](docs/workflow.md) for the complete architecture, GitHub label state machine, and core concepts.**

---

## Key Capabilities

- **Powered by `acpterm`**: Reuses `acpterm`'s `ACPAgent` connection engine, `~/.acpterm/config.json` agent registry, auth token resolution, permission hooks, Rich streaming output, and Markdown transcript recording.
- **Per-Stage Agent, Persona & `apm` Skill Customization**: Each stage (`triage`, `spec`, `build`, `review`) configures its own `agent`, `model`, `mode` (`plan` vs `code`), `read_only` enforcement, role `persona` (`.md`), and `skills` (`SKILL.md` installed via `apm` or `.factory/skills/`).
- **Clean GitHub Integration (Zero Bot Comment Spam)**:
  - The **Issue Body** (`<!-- factory:start -->` block) is updated in-place as the single source of truth for the specification, assumptions, decisions, and any open questions.
  - **Issue Comments** are reserved strictly for humans (answering open questions or providing feedback).
  - **Two Mandatory Human Gates** (`factory:spec-ready` and `factory:pr-ready`) and **no automatic PR merging**.
- **Sequential Fail-Fast Checks & Self-Healing Loop**: Keeps the ACP session alive across retry turns with intelligent output truncation.

---

## Installation

```bash
uv sync
```

Or install globally as a CLI tool:

```bash
uv tool install -e .
```

---

## Quick Start

### 1. Scaffold a Multi-Stage `.factory/` Workspace

```bash
acp-factory init --multi-stage
```

This creates:
- `.factory/config.toml` — Stage definitions (`triage`, `spec`, `build`, `review`) and reusable checks (`lint`, `test`, `review`)
- `.factory/personas/{triage,architect,implementer,reviewer}.md` — Role personas per stage
- `.factory/prompts/{TRIAGE,SPEC,BUILD,REVIEW}.md` — Stage prompt contracts
- `apm.yml` — Agent Package Manager manifest for skills and MCP dependencies

*(For a simple single-file config, run `acp-factory init` to generate `factory.yaml`.)*

### 2. Run a GitHub Issue Workflow (or Dry-Run)

Check the queue of open `factory:*` issues and local `.worktrees/`:

```bash
acp-factory status
```

Run the current stage for a specific GitHub issue (add `--dry-run` to preview the proposed Issue/PR bodies locally and get copy-pasteable `gh` commands without mutating GitHub):

```bash
acp-factory issue 1 --dry-run
acp-factory issue 1
```

Or automatically process the next actionable issue in the repository queue:

```bash
acp-factory next
```

### Fleet Orchestration (Multi-Repo / Org-Wide)

Add a `[fleet]` section to `.factory/config.toml` to run the same 5-stage workflow across many repositories, cloning `"auto"` targets on demand and isolating per-repo errors:

```toml
[fleet]
workspaces_dir = "~/.acp-factory/repos"

[fleet.repositories]
"acme/core-lib" = "~/DEV/core-lib"   # existing local checkout
"acme/service-api" = "auto"          # cloned into workspaces_dir/acme__service-api
```

Advance one step per repo (with stage-aware admission control so each repo has at most one in-flight `build`/`review`/`pr-ready` branch) and inspect per-repo status:

```bash
acp-factory next --fleet
acp-factory status --fleet
```

`--fleet` is auto-enabled whenever a `[fleet]` section is present in the active config.

### 3. Review a Standalone Working Branch (With Optional PR Creation)

Review any checked-out branch in a target repository (even without a GitHub Issue or `.factory/` setup) using the bundled `code-reviewer` and `security-auditor` skills:

```bash
acp-factory review "Short description of what this branch changes" \
  --workspace /path/to/repo \
  --create-pr \
  --dry-run
```

### 4. Run an Ad-Hoc Stage or Task Locally

Run the `build` stage with its configured persona, skills, and checks:

```bash
acp-factory run "Implement pagination for GET /items" --stage build
```

Override agent, persona, or attach a skill on the CLI:

```bash
acp-factory run "Review the pagination implementation" \
  --stage review \
  --agent opencode \
  --persona .factory/personas/reviewer.md \
  --skill code-reviewer \
  --read-only
```

Export a full Markdown transcript of all attempts:

```bash
acp-factory run "Fix edge case in parser" --stage build --export transcript.md
```

---

## Documentation & Roadmap

- **[Workflow & Core Concepts (`docs/workflow.md`)](docs/workflow.md)**: Detailed guide to the 5-stage GitHub label state machine, Task definition, in-place Issue Body spec contract, two-tier open questions handling, and worktree lifecycle.
- **[Stateless Fleet Orchestration & Review Hardening (`docs/fleet-orchestration.md`)](docs/fleet-orchestration.md)**: Architecture and invariants for multi-repo admission control, delta-focused review rounds, 3-round circuit breakers, and `REVIEWED_AT == HEAD` publish guards.
- **[Implementation Roadmap (`TODO.md`)](TODO.md)**: Current status and upcoming milestones.
