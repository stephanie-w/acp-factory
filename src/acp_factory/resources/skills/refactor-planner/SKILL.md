---
name: refactor-planner
description: Design behavior-preserving Refactor Contracts with explicit invariants, target seam & dependency direction, compatibility/re-export policy, and seam-cohesion sizing.
---

# Refactor Planner & Contract Authoring Skill

Use this skill during the `refactor-plan` stage to turn a structural refactoring issue into a verifiable **Refactor Contract** that an implementer (`refactor-builder`) and reviewer (`refactor-reviewer`) can execute and audit without unintended behavior drift.

## Resources in This Skill Package
- **Template**: `templates/refactor-plan.md` — canonical Markdown Refactor Contract skeleton.
- **Validation Script**: `scripts/validate_refactor_plan.py` — deterministic validator verifying required sections, `## Open Questions` / `STATUS:` consistency, privacy guardrails, and terminal `STATUS:`.

## 1. Pre-Plan Verification

1. **Read All Context**:
   - Read the issue description, any prior factory block, and **all human comments** in chronological order.
   - Read `AGENTS.md` / `CLAUDE.md` and inspect the target modules, callers, and test files.
2. **Verify Every Reference**:
   - Every `path/file:line`, symbol, and import path you cite must exist in the repository.
   - Trace all callers and re-exports with `rg -n` before deciding a compatibility policy.
3. **Read Economically (Bounded Slices)**:
   - Locate symbols with `rg -n` (or `rg -n "^(def |async def |class )" <file>`), then read bounded line ranges (`<=150` lines).
   - Never read full files over ~200 lines or re-read lines already inspected in the same turn.

## 2. Sizing Heuristic (Seam Cohesion, Not Diff Line Count)

Unlike feature PRs, a pure structural move (e.g., extracting a cohesive module or splitting a large file) can cleanly exceed 400 diff lines with low behavioral risk. Size a refactor by **blast radius**, not raw line count:

- **`STATUS: READY`** when:
  - The change targets **one cohesive seam** with a clear, acyclic dependency direction.
  - Existing tests cover the observable behavior (or a small set of characterization/seam tests can be added first).
  - Test-patch blast radius is bounded and well-understood.
- **`STATUS: SPLIT_NEEDED`** when:
  - The issue spans multiple independent seams, tangled bidirectional dependencies, or mixes structural moves with behavioral changes.
  - Include a `## Proposed Split` section with 2–4 ordered, independently mergeable sub-refactors (each with its own seam and invariant) so a human can split the issue cleanly.

## 3. Two-Tier Ambiguity & Risk Filter (`## Assumptions` vs. `## Open Questions`)

- **Two-way door** (internal module layout, private helper naming, local import organization):
  - Record under `## Assumptions` (`STATUS: READY`).
- **One-way door** (breaking public API/CLI/config contracts, dropping backward-compatible re-exports used externally, changing persisted artifact schemas, or adding new dependencies):
  - Escalate under `## Open Questions` (`STATUS: NEEDS_HUMAN`) with **2–3 concrete options** and your **recommended option**.
- **Fold Answered Questions into `## Decisions`**:
  - When human comments settle a prior open question, record the decision and rationale under `## Decisions` and remove `## Open Questions`.

## 4. Contract Requirements

Every `READY` Refactor Contract MUST specify:
1. **`## Invariant`**: Exact observable behaviors, CLI/API contracts, exit codes, and artifact formats that MUST remain unchanged.
2. **`## Target Seam & Dependency Direction`**: A shallow directory or import-direction diff showing the new boundary and guaranteeing **no circular imports** (prefer a small text diff over verbose prose).
3. **`## Compatibility & Re-export Policy`**: Explicitly state which symbols stay re-exported at the old module path vs. which internal callers/tests are migrated directly (and why).
4. **`## Files to Touch`**: Every source file, test file, and **documentation pointer** (e.g., `AGENTS.md` codebase map, `docs/`) that must move or update.
5. **`## Baseline & Seam Tests`**: Exact commands to verify the green baseline before editing, plus any new characterization/seam tests required to pin the invariant.
6. **`## Out of Scope`**: Explicit boundary preventing behavioral changes, opportunistic rewrites, or unrelated cleanups.

## 5. Incremental Revision Mode

When an existing `# Refactor Contract` is present in the factory block:
- Focus on the feedback in `## Human Comments` rather than restarting full exploration.
- Move answered questions from `## Open Questions` into `## Decisions`.
- Emit the complete updated contract matching `templates/refactor-plan.md` ending with `STATUS: <READY | SPLIT_NEEDED | NEEDS_HUMAN>`.
