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

- [x] **Reusable Stage & Domain-Agnostic Skill Packages (`~/DEV/coding-ai-resources/skills/`)**:
  - [x] Packaged 5 reusable `SKILL.md` skills (`issue-triage`, `spec-writer`, `tdd-builder`, `code-reviewer`, `security-auditor`) adapted from `owainlewis/software-factory` for both general software and MLOps/Cloud IaC workflows, plus `html2md` for web documentation extraction.
  - [x] Migrated all stage personas (`agents/*.agent.md`), prompts (`prompts/*.md`), and skills (`skills/*`) into `~/DEV/coding-ai-resources` (`packages/software-factory/package.json`) so `acp-factory` acts purely as an orchestrator resolving resources via `resources_dir` / `FACTORY_RESOURCES_DIR`.
- [x] **Persona & Prompt Hardening (`~/DEV/coding-ai-resources/agents/` & `prompts/`)**:
  - [x] Added single-PR sizing heuristics (`<400` lines + build-order `SPLIT`), falsifiable `AC-n` acceptance tables (`| ID | Done when | How to check |`), cost-to-undo human question filtering, 7-point builder quality standards, `blocking`/`major`/`minor` severity rubrics, `LOW`/`MEDIUM`/`HIGH` risk path classification, and `security.agent.md` / `security.md`.
- [x] **Standalone Branch Review & Pull Request Command (`acp-factory review`)**:
  - [x] Added `acp-factory review [DESCRIPTION] --workspace ... [--branch ...] [--base main] [--create-pr] [--dry-run]` to review working branches outside the GitHub Issue pipeline and optionally push/open standalone PRs when `STATUS: ACCEPTED`.
- [ ] **Domain-Specific Skill Packs & Catalog Picker (`~/DEV/coding-ai-resources`)**:
  - [ ] Keep `acp-factory` core domain-agnostic while authoring domain skill packs in `~/DEV/coding-ai-resources` (e.g. `mlops-iac` for Terraform/Helm/K8s offline validation & IAM/cost escalation, `ml-endpoints-pipelines` for DAG compilation & stubbed model boundary checks).
  - [ ] Add a dynamic catalog skill picker (in `triage` / `spec`) that inspects the target workspace (`pyproject.toml`, `*.tf`, `Dockerfile`, pipeline DAGs, `apm.yml`) and attaches the matching domain skills from `~/DEV/coding-ai-resources`.

---

## 3. Next Major Milestone — Stateless Fleet Orchestration & Review Loop Hardening

> 📖 See **[docs/fleet-orchestration.md](docs/fleet-orchestration.md)** for the full architectural design, subtleties, and problem analysis.

- [x] **Stateless Review Loop Integrity & 3-Round Circuit Breaker (`task.py` & `loop.py`)**:
  - [x] Track `review_round` and `last_reviewed_sha` statelessly via `<!-- factory:meta ... -->` inside the Issue Body's `<!-- factory:start -->` block.
  - [x] Replace (instead of stacking) `## Latest Review Findings (Round N/3)` in-place on `REVISION_NEEDED`, and strip temporary findings from the spec block when `ACCEPTED` so obsolete failure notes never leak into the PR's `## Specification`.
  - [x] Inject a delta-focused review brief (`git diff <last_reviewed_sha>..HEAD` + prior findings) on Round 2+ to prevent reviewer "moving goalposts", and enforce a deterministic 3-round circuit breaker (`factory:needs-human`).
  - [x] Enforce a pre-review and pre-publish `REVIEWED_AT == HEAD` + clean working tree (`git status --porcelain == ""`) guard before pushing or opening/updating a PR, stamping `Reviewed-SHA` in the PR body.
- [x] **Stateless Multi-Repo / Organization-Wide Fleet Orchestration (`config.py`, `github.py`, `loop.py`, `cli.py`)**:
  - [x] Support `[fleet]` configuration mapping `"owner/repo"` to existing local `~/DEV/...` checkouts only (no on-demand cloning; `org` discovery stays deferred), branching new issue worktrees from a freshly fetched `origin/<default-branch>`.
  - [x] Keep prompts, personas, and skills centralized while optionally overlaying target-repo `pre` setup and `[checks.*]` (zero `.factory/` scaffolding required in target repos).
  - [x] Enforce stage-aware per-repo admission control in `acp-factory next --fleet` (`done` cleanups first; read-only `triage`/`spec` always allowed up to Gate 1; at most 1 active `build`/`review`/`pr-ready` branch per repo to prevent merge-conflict cascades) with per-repo error isolation and `acp-factory status --fleet`.
- [ ] **Continuous Orchestration (Daemon / Scheduling Loop)** *(deferred — discussion after fleet runner lands)*:
  - [ ] Decide trigger model: batch-only via external scheduler (cron / systemd timer / GitHub Actions / webhook) vs. in-process `next --fleet --watch` loop vs. GitHub webhook/event-driven.
  - [ ] If an in-process loop is chosen: add a configurable `poll_interval_seconds` (fetch frequency knob), concurrency/batching for many repos, and per-repo backoff/rate-limit handling around `gh` + `git fetch`.
  - [ ] Note the trade-off: an in-process resident loop reintroduces a long-lived process, weakening the "100% stateless on disk / no daemon" design goal in `docs/fleet-orchestration.md` §1.
- [ ] **Bot vs. Human Attribution on GitHub**:
  - [ ] Configure local git author (`user.name = "acp-factory[bot]"`) inside `.worktrees/issue-<id>` so agent commits are visually distinct from personal commits.
  - [ ] Support optional `FACTORY_GH_TOKEN` / GitHub App token override for `gh` CLI calls so issue edits and PR creation can use a dedicated bot account without changing personal `gh auth` state.
  - [ ] Include an explicit `> 🤖 acp-factory | Stage: ... | Agent: ...` attribution banner at the top of the `<!-- factory:start -->` Issue Body block.
- [ ] **Nice-to-have: GitHub org auto-discovery for fleet (`[fleet].org`)** *(deferred — only add with filtering)*:
  - [ ] Add optional `org` field that runs `gh repo list <org> --json nameWithOwner` and treats matches as `"auto"` targets.
  - [ ] Add inclusion/exclusion filtering (e.g. only repos with open `factory:*` issues) before enabling, to avoid cloning/scanning the entire org.
- [ ] **User-level default config (`~/.acp-factory/config.toml`)**:
  - [ ] Make `FactoryConfig.discover` fall back to a user-level config (e.g. `~/.acp-factory/config.toml`) so personal/fleet configs with `~/DEV/...` paths and usernames don't have to live in tracked repo files or require `-c` on every command.
  - [ ] Resolve persona/prompt/skill paths relative to the config file's directory (already done) AND document that copying a config home requires copying `.factory/personas` + `.factory/prompts` alongside it.
