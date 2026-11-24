---
name: refactor-reviewer
description: Skeptically review refactoring diffs for strict behavior preservation, seam depth, acyclic dependency direction, test integrity, and documentation sync.
---

# Skeptical Refactor Reviewer Skill

Use this skill during the `refactor-review` stage to audit a refactoring branch as if someone you do not trust wrote it, catching any hidden behavior change, weakened test, shallow wrapper, or broken import boundary before human review.

## Resources in This Skill Package
- **Template**: `templates/refactor-review-report.md` — canonical Markdown review report skeleton (compatible with `code-reviewer/scripts/validate_review.py`).

## 1. Two-Axis Refactor Review Procedure

Evaluate the diff (`git diff main...HEAD`) along two separate axes:

### Axis 1: Behavior Preservation & Contract Fidelity
1. **Inspect Full Context**:
   - Read `AGENTS.md` / `CLAUDE.md`, the approved Refactor Contract (`## Invariant`, `## Target Seam & Dependency Direction`, `## Compatibility & Re-export Policy`, `## Files to Touch`, `## Baseline & Seam Tests`), and any prior review findings.
   - Run `git status --short`, `git log --oneline -n 5`, and `git diff main...HEAD`.
2. **Verify Zero Unintended Behavior Changes**:
   - Inspect every modified logic block, default argument, exception type, return value, and side effect.
   - Flag any uncontracted behavior change, silent coercion, or altered error message/type as `[blocking]`.
3. **Verify Compatibility & Re-export Policy**:
   - Confirm every declared public re-export remains importable from its original path with identical signature/semantics.
   - Confirm internal callers were migrated cleanly without leaving broken references or stale patches.
4. **Audit Test Integrity**:
   - Verify no existing behavior assertions were deleted, skipped, weakened, or replaced with tautological mocks (`[blocking]`).
   - Verify new seam/characterization tests actually exercise the extracted seam and would fail if the seam were broken (`[major]` if tautological or mock-only).

### Axis 2: Seam Depth, Dependency Direction & Hygiene
1. **Seam Depth (Deep Module Check)**:
   - Flag shallow pass-through wrappers, pointless delegation layers, or speculative abstractions not justified by the contract as `[major]`.
2. **Dependency Direction & Cycles**:
   - Verify imports flow strictly in the direction declared in `## Target Seam & Dependency Direction` with no circular imports or hidden reverse coupling (`[blocking]` if circular or inverted).
3. **Scope, Dead Code & Doc Sync**:
   - Flag dead code, orphaned helpers, unused imports, or out-of-scope edits (`[major]`).
   - Verify documentation pointers listed in the contract (e.g., `AGENTS.md` codebase map) were updated (`[major]` if stale).

## 2. Severity Rubric

| Severity | Meaning | Blocks Acceptance? |
| :--- | :--- | :--- |
| `blocking` | Unintended behavior change, broken public API/re-export, circular/inverted dependency, weakened/deleted behavior test, failing check, or security/privacy leak | **Yes** (`STATUS: REVISION_NEEDED`) |
| `major` | Shallow pass-through wrapper, dead code, out-of-scope edit, weak/tautological seam test, violation of `AGENTS.md`, or stale doc pointers | **Yes** (`STATUS: REVISION_NEEDED`) |
| `minor` | Safe to ship: minor naming, wording, or small local simplification | **No** (`STATUS: ACCEPTED` if no `blocking`/`major` findings) |

Rules for grading findings:
- Drop purely subjective style opinions not backed by `AGENTS.md` or the Refactor Contract.
- Mark each finding `[unambiguous]` (implementer can fix directly) or `[needs_human]` (only when neither the contract nor the codebase settles it — provide 2–3 options and a recommendation, and return `STATUS: NEEDS_HUMAN`).

## 3. Change Risk Classification (`LOW` | `MEDIUM` | `HIGH`)

- **`HIGH`**: Touches CI/CD, build/deploy configs, auth/security/secrets, persistent storage schemas, or breaks/removes public exports.
- **`MEDIUM`**: Cross-module seam extraction affecting multiple callers or public re-exports.
- **`LOW`**: Isolated internal refactor with zero public surface change and full test coverage.

## 4. Output Contract

Output ONLY the structured Markdown report matching `templates/refactor-review-report.md` ending with the `STATUS:` line (validated by `code-reviewer/scripts/validate_review.py`).
