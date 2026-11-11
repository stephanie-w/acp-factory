---
name: spec-writer
description: Write verified, unambiguous technical specifications with falsifiable AC-n acceptance tables and a strict cost-to-undo filter for human questions vs. assumptions.
---

# Specification Writer & Acceptance Rubric Skill

Use this skill during the `spec` stage to turn a triaged ticket into a self-contained specification that an implementer and reviewer can execute and verify deterministically.

## Resources in This Skill Package
- **Template**: `templates/spec.md` — canonical Markdown specification skeleton.
- **Validation Script**: `scripts/validate_spec.py` — deterministic validator verifying headings, `AC-n` table format, `## Open Questions` rules, privacy guardrails, and terminal `STATUS:`.

## 1. Pre-Specification Verification

1. **Read All Context**:
   - Read the original issue description, prior triage findings in the `<!-- factory:start -->` block, and **all human comments** in chronological order.
   - Read `AGENTS.md` / `CLAUDE.md` and inspect every file and interface the change will touch.
2. **Verify Every Reference**:
   - Every `path/file:line` you cite must exist in the repository.
   - Never guess existing function signatures, schemas, or configuration keys—read them first.

## 2. Two-Tier Ambiguity Filter (`## Assumptions` vs. `## Open Questions`)

Ask the human **only what is theirs to decide**:

1. **Record as `## Assumptions` (`STATUS: SPEC_READY`)**:
   - Minor, code-local, easily reversible implementation choices that are not settled by the issue or codebase and do not alter public contracts, dependencies, or infrastructure cost/security.
2. **Escalate in `## Open Questions` (`STATUS: NEEDS_HUMAN`)**:
   - Any choice that **nothing in the ticket, comments, or codebase settles** AND **would be costly to undo**, including:
     - Adding or modifying project dependencies (`pyproject.toml`, `package.json`, `Cargo.toml`, lockfiles, container base images, Terraform providers) not explicitly requested in the issue.
     - Choosing missing project-wide test, build, or validation tooling.
     - Breaking public API/CLI/event/model payload contracts.
     - Cloud/MLOps cost, compute, or security decisions (GPU/instance types, scaling bounds, IAM permissions, network exposure, data retention/deletion policies).
   - For every open question, provide **2–3 concrete options** and state your **recommended option** with a one-line rationale under a `## Open Questions` heading.
3. **Fold Answered Questions into `## Decisions`**:
   - When human comments answer prior open questions, record the chosen option and rationale under `## Decisions` and remove the resolved `## Open Questions` section.

## 3. Falsifiable Acceptance Criteria Table (`AC-n`) & Testability Rules

Every `SPEC_READY` specification MUST include an `## Acceptance Criteria` table using stable `AC-1`, `AC-2`, ... identifiers (never renumber existing IDs on spec revisions):

| ID | Done when | How to check |
| :--- | :--- | :--- |
| `AC-1` | <Observable behavior, state change, or contract outcome> | <Deterministic local check: specific test to write/run, CLI command + expected output, DAG/schema compile check, or static IaC validation> |

Rules for `AC-n` rows and testability:
- **No vague words** ("appropriately", "correctly", "properly", "fast"). State exact inputs, outputs, status codes, or error messages.
- **Must be falsifiable**: Every `How to check` must be a concrete test or command that runs locally inside an isolated worktree without requiring live production credentials or external cloud mutations, and would fail if the requirement is not met.
- **Mandatory documentation check**: If the change introduces or modifies user-facing CLI commands, flags, configuration options/precedence, public interfaces, or workflows, include an explicit `AC-n` row verifying that relevant documentation (e.g. `README.md`, `docs/`, or CLI `--help`) is updated.
- **Mandatory `## How to Test` guide**: If the change touches user-facing CLI commands, flags, or configuration options, include a concrete `## How to Test` section with a minimal working configuration snippet, exact terminal command, and expected output.

## 4. Specification Revision Workflow (Incremental Mode)

When an existing specification is present in the context or when a `# Spec Revision Brief` is supplied:
1. **Incremental over Scratch**: Do not restart broad codebase exploration. Focus your attention exclusively on the requests and questions raised in `## Human Comments`.
2. **Stable Criteria**: Never delete or renumber existing `AC-n` rows that have already been vetted. Append newly requested criteria as `AC-(n+1)`.
3. **Resolve Open Questions**: Move answered questions from `## Open Questions` into `## Decisions` with the human's rationale.
4. **Targeted Diffs**: Only update `## Behavior`, `## Files to Touch`, or `## Out of Scope` where the human comment explicitly modifies scope or settles a question.
5. **Output Full Document**: Always emit the entire updated Markdown specification matching `templates/spec.md` (checked by `scripts/validate_spec.py`).
