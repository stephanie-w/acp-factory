---
name: refactor-builder
description: Execute behavior-preserving structural refactors from an approved Refactor Contract with baseline-green verification, seam tests, doc-pointer sync, and zero behavioral drift.
---

# Refactor Builder & Behavior-Preservation Skill

Use this skill during the `refactor-apply` stage to execute an approved Refactor Contract cleanly, preserving 100% of observable behavior while establishing the target seam.

## Resources in This Skill Package
- **Template**: `templates/refactor-notes.md` — concise refactor summary skeleton.
- **Scope Guardrail**: reuse `tdd-builder/scripts/check_build_scope.py` (when available) to ensure no local host paths leak into the diff.

## 1. Execution Workflow

1. **Read First**:
   - Read `AGENTS.md` / `CLAUDE.md` and the approved Refactor Contract (`## Invariant`, `## Target Seam & Dependency Direction`, `## Compatibility & Re-export Policy`, `## Files to Touch`, `## Baseline & Seam Tests`, `## Decisions`).
   - If `## Latest PR Review Findings`, `## PR Review Feedback`, or `## Human Review Comments` are present, fix every actionable finding (`[blocking]`, `[major]`, and `[minor]`) at its root cause.
2. **Baseline Green First**:
   - Before editing production code, run the baseline verification command from `## Baseline & Seam Tests` (e.g., `just typecheck && just test-fast`) and record the exact passing summary.
   - If baseline tests are missing coverage for a seam you are about to move, add characterization tests FIRST and verify they pass on the unmodified code.
3. **Behavior-Preserving Structural Move**:
   - Execute the move strictly within `## Files to Touch`. Do NOT slip in bug fixes, feature tweaks, or opportunistic formatting/rewrites outside the contract.
   - Enforce `## Compatibility & Re-export Policy` verbatim: preserve declared public re-exports and migrate declared internal callers cleanly.
   - Keep dependency direction strictly acyclic (no circular imports or runtime-only import hacks unless explicitly justified by the contract).
   - **Feedback Cadence**: Use a tight inner loop (`uv run pytest tests/test_focused.py`) during the move, then run the full project verification suite (`just fix`, `just typecheck`, `just test-fast`) once before self-review and commit.
4. **Never Weaken Existing Tests**:
   - Do not delete, skip, loosen, or mock away existing behavior assertions to make the refactor pass.
   - Only update import paths or internal patch targets when explicitly moving a symbol, and add direct unit tests for the newly extracted seam.
5. **Sync Documentation Pointers**:
   - Update every documentation pointer affected by the move (such as `AGENTS.md` codebase maps or module paths in `docs/`).
6. **Self-Review & Local Commit**:
   - Inspect `git diff` to confirm:
     - Zero unintended behavior changes.
     - No dead code, orphaned imports, or shallow pass-through wrappers.
     - No leaked local host paths (`/home/...`, `/Users/...`).
   - Summarize your work matching `templates/refactor-notes.md`, including Baseline Before → After evidence.
   - Commit locally on the current branch with a clear `refactor(scope): ...` Conventional Commit message. Never push or merge directly.

## 2. Refactoring Quality Standard

The reviewer (`refactor-reviewer`) will audit your diff against:
1. **Invariant Preservation**: Observable behavior, CLI/API contracts, error types, and artifact formats are unchanged.
2. **Seam Depth**: The extracted module owns a real, cohesive responsibility with a clean interface — not a thin forwarding wrapper.
3. **Acyclic Dependencies**: Dependency direction matches the contract with zero circular imports.
4. **Compatibility & Cleanliness**: Declared re-exports are intact; dead code and unused imports are removed.
5. **Test Integrity**: Existing behavior assertions remain intact; new seam tests are concise, parametrized where appropriate, and test through public interfaces.
6. **Doc Sync**: `AGENTS.md` and affected documentation pointers reflect the new layout.
7. **Security & Privacy**: No hardcoded secrets, tokens, or local host paths.
