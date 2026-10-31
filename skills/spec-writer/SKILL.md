---
name: spec-writer
description: Write verified, unambiguous technical specifications with falsifiable AC-n acceptance tables and a strict cost-to-undo filter for human questions vs. assumptions.
---

# Specification Writer & Acceptance Rubric Skill

Use this skill during the `spec` stage to turn a triaged ticket into a self-contained specification that an implementer and reviewer can execute and verify deterministically.

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
   - For every open question, provide **2–3 concrete options** and state your **recommended option** with a one-line rationale.
3. **Fold Answered Questions into `## Decisions`**:
   - When human comments answer prior open questions, record the chosen option and rationale under `## Decisions` and remove the resolved `## Open Questions` section.

## 3. Falsifiable Acceptance Criteria Table (`AC-n`)

Every specification MUST include an `## Acceptance Criteria` table using stable `AC-1`, `AC-2`, ... identifiers (never renumber existing IDs on spec revisions):

| ID | Done when | How to check |
| :--- | :--- | :--- |
| `AC-1` | <Observable behavior, state change, or contract outcome> | <Deterministic local check: specific test to write/run, CLI command + expected output, DAG/schema compile check, or static IaC validation> |

Rules for `AC-n` rows:
- **No vague words** ("appropriately", "correctly", "properly", "fast"). State exact inputs, outputs, status codes, or error messages.
- **Must be falsifiable**: Every `How to check` must be a concrete test or command that runs locally inside an isolated worktree without requiring live production credentials or external cloud mutations, and would fail if the requirement is not met.

## 4. Specification Template

```markdown
# Specification

## Problem
<Who is affected, what goes wrong or is missing today, and why it matters in 2–4 sentences.>

## Behavior
1. <Numbered, testable behaviors covering success, invalid/borderline input, and failure handling.>

## Design
<Include ONLY when the change spans multiple modules or services: a concise Mermaid diagram of the flow and the module/interface/schema boundaries.>

## Files to Touch
- `path/to/file.ext` — <concise summary of changes>

## Decisions
- <Each settled decision, who made it (human comment, parent issue), and why — or `- None yet.`>

## Assumptions
- <Reversible, code-local choices made without blocking on human input — or `- None.`>

## Acceptance Criteria
| ID | Done when | How to check |
| :--- | :--- | :--- |
| AC-1 | <Observable outcome> | <Test or local verification command> |

## Out of Scope
- <What this ticket explicitly does not change.>

STATUS: <SPEC_READY | NEEDS_HUMAN>
```
*(If `STATUS: NEEDS_HUMAN`, include `## Open Questions` right before `STATUS: NEEDS_HUMAN`.)*
