### Refactor Summary
- **Files Modified**: `<repo-relative paths>`
- **Seam & Dependency Direction Verified**: `<caller> -> <new_seam> -> <lower_layer> (acyclic)`
- **Compatibility / Re-exports**: `<preserved re-exports and migrated internal callers>`
- **Doc Pointers Updated**: `<e.g. AGENTS.md codebase map, or None required>`
- **Evidence**:
  - **Baseline (Before)**: `<passing baseline command and summary count>`
  - **Verification (After)**: `<passing lint, typecheck, and test commands + summary count>`
- **Seam / Characterization Tests Added**: `<test names and what invariant they pin>`
- **Local Commit**: `<short Conventional Commit message>`
