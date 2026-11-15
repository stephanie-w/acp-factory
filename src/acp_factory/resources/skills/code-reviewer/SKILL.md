---
name: code-reviewer
description: Skeptically review branch diffs against specifications, verify test effectiveness and AC-n coverage, classify change risk, and grade findings with a 3-tier severity rubric.
---

# Skeptical Code Reviewer & Risk Classification Skill

Use this skill during the `review` stage (or review checks) to verify a branch as if someone you do not trust wrote it, catching every blocking or major defect before a human reviews the pull request.

## Resources in This Skill Package
- **Template**: `templates/review-report.md` — canonical Markdown review report skeleton.
- **Validation Script**: `scripts/validate_review.py` — deterministic validator verifying `### Risk Classification`, `### Findings`, severity/status consistency, and privacy rules.

## 1. Two-Axis Review Procedure

Evaluate the diff along two separate axes so code style doesn't mask broken requirements, and working code doesn't excuse bad architecture:

### Axis 1: Spec Fidelity & AC Coverage
1. **Inspect Full Context**:
   - Read `AGENTS.md` / `CLAUDE.md`, the approved specification (`## Behavior`, `## Files to Touch`, `## Decisions`, `## Acceptance Criteria`), and any prior review findings.
   - Run `git status --short`, `git log --oneline -n 5`, and `git diff main...HEAD` (or base branch).
2. **Verify Every `AC-n` & Test Effectiveness**:
   - Confirm every `AC-n` row in `## Acceptance Criteria` is satisfied by the code and verified by an executable test or deterministic check.
   - Inspect new tests critically: would each test actually fail if the production change were reverted? Flag tautological tests or mock-only tests as at least `[major]`.
   - If user-facing CLI/config changed, verify documentation and `## How to Test` instructions are present and working; otherwise flag `[major]`.
3. **Check Scope & Bad-Input Probing**:
   - Detect unrequested behavior or scope creep (flag as `[major]`).
   - Verify bad, empty, boundary, and malformed inputs are explicitly rejected with errors rather than silently corrupted.

### Axis 2: Software Craftsmanship & Code Smell Baseline
Verify that the code adheres to repository standards (`AGENTS.md`) and does not introduce maintenance hazards. Use this **Code Smell Baseline**:

- **Speculative Generality** (`[major]`): Adding flags, configuration hooks, generic wrappers, or helpers for hypothetical future needs not asked for in the spec. &rarr; *Delete or inline back.*
- **Duplicated Code** (`[major]`): Copy-pasting logic or re-implementing an algorithm across multiple hunks rather than reusing an existing helper. &rarr; *Extract shared helper.*
- **Shotgun Surgery** (`[major]`): A single logical change that forces scattered, shallow modifications across many unrelated files. &rarr; *Consolidate responsibilities into a cohesive module.*
- **Primitive Obsession** (`[major]`): Passing raw unstructured strings, dicts, or tuples around where a typed model, dataclass, or domain value object belongs.
- **Data Clumps** (`[major]`): The same group of 3+ parameters traveling together across multiple function calls. &rarr; *Bundle into a parameter type.*
- **Feature Envy** (`[major]`): A function that reaches into another module/object's internal fields more than its own. &rarr; *Move the logic onto the owning object.*
- **Mysterious Name** (`[minor]`, or `[major]` if public contract): Names that obscure what a function, variable, or type does.

> *Note: Organizational compliance, regulatory checks, and licensing policies are audited separately by domain policy checkers (e.g. `policy-auditor`). Keep this review strictly focused on technical correctness, specification fidelity, and software craftsmanship.*

## 2. Severity Rubric

| Severity | Meaning | Blocks Acceptance? |
| :--- | :--- | :--- |
| `blocking` | Wrong behavior, data loss, silent data corruption, security vulnerability, broken contract, failing check, missing required `AC-n` test/check, or drift from the spec | **Yes** (`STATUS: REVISION_NEEDED`) |
| `major` | Real maintenance or reliability cost: likely unhandled edge case, weak/tautological test, violation of `AGENTS.md`, unapproved dependency, or stale documentation | **Yes** (`STATUS: REVISION_NEEDED`) |
| `minor` | Safe to ship: minor naming, wording, or small local simplification | **No** (`STATUS: ACCEPTED` if no `blocking`/`major` findings) |

Rules for grading findings:
- A fix that misses an obvious variant of the same bug is at least `major`.
- Drop purely subjective style opinions not backed by `AGENTS.md` or the specification.
- Mark each finding `[unambiguous]` (implementer can fix directly) or `[needs_human]` (only when neither the spec, codebase, nor an obviously safer choice settles it—provide 2–3 options and a recommendation, and return `STATUS: NEEDS_HUMAN`).

## 3. Change Risk Classification (`LOW` | `MEDIUM` | `HIGH`)

Always classify the change's risk tier based on what the diff touches:
- **Path Rules (Always `HIGH`)**:
   - CI/CD, build, and deployment configs (`.github/`, `Dockerfile`, `justfile`, `Makefile`, `deploy/`)
   - Infrastructure-as-Code, IAM, permissions, auth, sessions, billing, or secrets (`*.tf`, `helm/`, `k8s/`, auth modules)
   - Database schemas, migrations, or code that deletes/rewrites persistent data or model artifacts
- **`MEDIUM`**: Ordinary feature/product changes, bug fixes, new endpoints/CLI commands, pipeline definitions, or shared helpers.
- **`LOW`**: Documentation, comments, tests-only changes, or isolated internal refactors with no behavior change.

## 4. Output Contract

Output ONLY the structured Markdown report matching `templates/review-report.md` ending with the `STATUS:` line (validated by `scripts/validate_review.py`).
