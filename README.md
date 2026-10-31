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

### 3. Run an Ad-Hoc Stage or Task Locally

Run the `build` stage with its configured persona, skills, and checks:

```bash
acp-factory run "Implement pagination for GET /items" --stage build
```

Override agent, persona, or attach an `apm` skill on the CLI:

```bash
acp-factory run "Review the pagination implementation" \
  --stage review \
  --agent opencode \
  --persona .factory/personas/reviewer.md \
  --skill llm_judge \
  --read-only
```

Export a full Markdown transcript of all attempts:

```bash
acp-factory run "Fix edge case in parser" --stage build --export transcript.md
```

---

## Documentation & Roadmap

- **[Workflow & Core Concepts (`docs/workflow.md`)](docs/workflow.md)**: Detailed guide to the 5-stage GitHub label state machine, Task definition, in-place Issue Body spec contract, two-tier open questions handling, and worktree lifecycle.
- **[Implementation Roadmap (`TODO.md`)](TODO.md)**: Current status and Phase 2 backlog.
