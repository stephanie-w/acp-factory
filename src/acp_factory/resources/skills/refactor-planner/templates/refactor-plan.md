# Refactor Contract

## Problem & Motivation
<What structural friction, coupling, or maintenance hazard exists today, and why this seam extraction matters in 2–4 sentences.>

## Invariant
- <Observable behavior, public API/CLI contract, exit code, or persisted artifact format that MUST remain identical.>
- <Explicitly state: zero behavioral changes are permitted in this refactor.>

## Target Seam & Dependency Direction
```diff
 src/package/
 ├── existing_module.py   (delegates to new seam; no reverse import)
+└── new_seam.py          (owns cohesive responsibility; depends only on lower layers)
```
- **Dependency Direction:** `<caller> -> <new_seam> -> <lower_layer>` (acyclic; verify no circular imports).
- **Seam Depth:** <Why this boundary hides real complexity rather than acting as a shallow pass-through wrapper.>

## Compatibility & Re-export Policy
- **Public / External Re-exports:** <List symbols kept re-exported at the old import path for compatibility, or state `None (internal-only)`.>
- **Internal Caller Migration:** <List internal modules/tests updated to import from the new seam directly.>

## Files to Touch
- `path/to/new_module.py` — (new) <cohesive responsibility extracted>
- `path/to/existing_module.py` — <delegate to new module, remove extracted code>
- `tests/test_seam.py` — <characterization / seam tests>
- `AGENTS.md` — <update codebase map / module pointers if affected>

## Baseline & Seam Tests
- **Baseline command (must pass before edits):** `<e.g. just typecheck && just test-fast>`
- **Targeted seam tests:** `<existing or new tests pinning the invariant and new seam boundary>`

## Decisions
- <Settled decisions from human comments or parent issues — or `- None yet.`>

## Assumptions
- <Reversible internal choices made without blocking on human input — or `- None.`>

## Out of Scope
- <Any behavioral changes, feature additions, or unrelated module cleanups explicitly excluded.>

STATUS: <READY | SPLIT_NEEDED | NEEDS_HUMAN>
