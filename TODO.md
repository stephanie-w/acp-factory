# TODO — `acp-factory` Roadmap & Architecture

> 📖 See **[docs/workflow.md](docs/workflow.md)** for the complete architecture, 5-stage GitHub label state machine, Task definition, and GitHub interaction rules.

---

## 1. Completed

### Phase 1 — Core ACP Runner & `acpterm` Integration
- [x] **Project Scaffolding & Tooling**:
  - [x] `pyproject.toml` with `acpterm` as an editable dependency, `typer`, `rich`, `pydantic`, `pyyaml`, `pytest`, `ruff`, `ty`.
  - [x] `justfile` integration (`just fix`, `just typecheck`, `just test-fast`, `just ci`).
- [x] **ACP Client Layer (`src/acp_factory/acp/`)**:
  - [x] `FactoryACPClient` wrapping `acpterm.acp_agent.ACPAgent` with persistent session state across retry turns.
  - [x] Workspace `cwd` and clean `.venv` environment injection (`build_workspace_env`) when spawning ACP agents.
  - [x] `TurnEventTracker` wrapping `acpterm.transcript.TranscriptRecorder` for per-turn metrics and Markdown transcript export.
- [x] **Core Execution & Verification Loop (`src/acp_factory/runner/`)**:
  - [x] Pre-execution `setup` shell commands (`uv sync`).
  - [x] Sequential fail-fast checks supporting both deterministic shell commands (`cmd`) and read-only agent review prompts (`prompt`).
  - [x] Intelligent check output truncation (`truncate_check_output`) preserving header and tail stack traces.
  - [x] Multi-attempt feedback retry loop in the same ACP session.
- [x] **End-to-End Demo (`demos/pagination_api/`)**:
  - [x] Verified full loop (`uv sync` -> `opencode` worker -> `ruff` -> `pytest` -> `Code Review` -> `transcript.md`).

### Phase 2 — Multi-Stage GitHub Label Workflow & Per-Stage Customization
- [x] **Per-Stage Agent, Persona, Skill & `apm` Configuration (`src/acp_factory/config.py` & `acp/client.py`)**:
  - [x] `.factory/config.toml` support with `[stages.*]` and `[checks.*]`.
  - [x] Per-stage `agent`, `command`, `model`, `mode`, `read_only`, `persona`, `skills`, `prompt`, `pre`, and `checks`.
  - [x] `resolve_persona_text` and `resolve_skill_paths` (resolving `.factory/skills/`, `skills/`, and `apm_modules/` from `apm`).
  - [x] Cross-agent / cross-model review check execution in `src/acp_factory/runner/checks.py`.
- [x] **Task & GitHub CLI Integration (`src/acp_factory/task.py` & `github.py`)**:
  - [x] `TaskContext` model with `original_body`, `factory_block`, `labels`, and chronological human `comments`.
  - [x] In-place Issue Body updater (`<!-- factory:start --> ... <!-- factory:end -->` via `gh issue edit --body-file -`) with zero bot comment spam.
  - [x] Two-tier Open Questions handling in `spec` (`## Assumptions` -> `factory:spec-ready` vs `## Open Questions` -> `factory:needs-human`).
  - [x] Orchestrator-controlled deterministic label transitions (`transition_issue_label`) and PR creation/updating (`push_and_create_or_update_pr`) without auto-merging.
- [x] **Structured Verdicts, Persistent Issue Worktrees & CLI Commands**:
  - [x] `parse_stage_verdict`, `_clean_markdown_stage_body`, and `parse_review_verdict_status` (supporting JSON `{"status": "pass"|"retry"|"stop"}` and `STATUS:` / `VERDICT:` lines while stripping conversational intros/outros).
  - [x] Issue-keyed persistent worktrees (`.worktrees/issue-<id>` on `factory/issue-<id>`) preserved across `build` and `review` and automatically cleaned up on `done` or when the GitHub Issue is `CLOSED` by a PR merge.
  - [x] Structured run artifacts saved under `.factory/runs/<run-id>/`.
  - [x] CLI commands: `acp-factory init --multi-stage`, `acp-factory issue <id>`, `acp-factory next`, `acp-factory status`.
  - [x] **`--dry-run` Mode & Read-Only Agent `gh` Guardrail**:
    - [x] Read-only `gh` PATH shim (`_ensure_readonly_gh_shim_dir`) blocking agents from directly running mutating `gh` subcommands (`edit`, `create`, `comment`, `close`, `merge`).
    - [x] `--dry-run` (`dry_run = true`) saving proposed Issue/PR bodies locally under `.factory/runs/<run-id>/` and printing copy-pasteable `gh` commands (including `-R <owner>/<repo>` when invoked outside the target workspace).
- [x] **Live GitHub Issue Dogfooding (`stephanie-w/acpterm#1`)**:
  - [x] Verified full 5-stage pipeline (`triage` -> `spec` with `## Open Questions` & human comment loop -> Gate 1 -> `build` in `.worktrees/issue-1` -> `review` -> Gate 2 PR merge -> `done` cleanup).
  - [x] Hardened default stage personas & prompts with strict privacy guardrails (no host paths), zero conversational chatter in Issue Bodies, and explicit dependency/tooling escalation rules.
  - [x] Scoped default `[checks.lint]` to branch-modified files (`git diff --name-only --diff-filter=ACMR main -- '*.py' | xargs -r uv run ruff check`).

---

## 2. Next Up — Phase 3: Reusable `apm` Skill Packages & Extended Personas

- [x] **Reusable Stage & Domain-Agnostic Skill Packages (`skills/`)**:
  - [x] Packaged 5 reusable `SKILL.md` skills (`issue-triage`, `spec-writer`, `tdd-builder`, `code-reviewer`, `security-auditor`) adapted from `owainlewis/software-factory` for both general software and MLOps/Cloud IaC workflows.
  - [x] Enhanced `resolve_skill_paths` to support `.agents/skills/`, nested `apm_modules/`, and bundled fallback skills.
- [x] **Persona & Prompt Hardening (`.factory/personas/` & `.factory/prompts/`)**:
  - [x] Added single-PR sizing heuristics (`<400` lines + build-order `SPLIT`), falsifiable `AC-n` acceptance tables (`| ID | Done when | How to check |`), cost-to-undo human question filtering, 7-point builder quality standards, `blocking`/`major`/`minor` severity rubrics, `LOW`/`MEDIUM`/`HIGH` risk path classification, and `security.md` / `SECURITY.md`.
- [ ] **Dynamic Catalog Skill Picker (`~/DEV/coding-ai-resources`)**:
  - [ ] Add a meta-skill / `triage`-`spec` step that inspects the target workspace & issue domain and selects the appropriate domain skills (e.g., MLOps, Terraform/IaC, ML endpoints, Python TDD) from `~/DEV/coding-ai-resources` via `apm`.

---

## 3. Later / Nice-to-Have Improvements

- [ ] **Multi-Repo / Organization-Wide Fleet Orchestration**:
  - [ ] Support running `acp-factory` from a central control directory across multiple repositories or an entire GitHub organization (`repos = ["owner/repo-a", "owner/repo-b"]` or `org = "owner"`).
  - [ ] Add a daily/scheduled fleet loop that discovers actionable `factory:*` issues and open PRs across all target repositories, manages local checkouts/worktrees per repo, and combines central personas/skills with repo-specific `.factory/config.toml` checks.
- [ ] **Bot vs. Human Attribution on GitHub**:
  - [ ] Configure local git author (`user.name = "acp-factory[bot]"`) inside `.worktrees/issue-<id>` so agent commits are visually distinct from personal commits.
  - [ ] Support optional `FACTORY_GH_TOKEN` / GitHub App token override for `gh` CLI calls so issue edits and PR creation can use a dedicated bot account without changing personal `gh auth` state.
  - [ ] Include an explicit `> 🤖 acp-factory | Stage: ... | Agent: ...` attribution banner at the top of the `<!-- factory:start -->` Issue Body block.
