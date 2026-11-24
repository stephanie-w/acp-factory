Execute the `refactor-planner` skill for the supplied refactoring issue and any human comments:
1. Verify every referenced file, symbol, caller, and import path in the repository before designing the seam.
2. Size the refactor by **seam cohesion, import-cycle risk, and test-patch blast radius** (pure structural moves may exceed 400 diff lines safely; multi-seam or circular-dependency splits should return `STATUS: SPLIT_NEEDED` with ordered steps).
3. Apply the two-tier ambiguity filter (`## Assumptions` for reversible internal choices vs. `## Open Questions` + `STATUS: NEEDS_HUMAN` for breaking public API changes, re-export removal, or dependency/architecture trade-offs).
4. If an existing Refactor Contract is present in the factory block, operate in Incremental Revision Mode (fold human answers into `## Decisions` and update only affected sections).
5. Output ONLY the complete Refactor Contract matching `skills/refactor-planner/templates/refactor-plan.md` (starting with `# Refactor Contract` and ending with `STATUS: <READY | SPLIT_NEEDED | NEEDS_HUMAN>`).
6. Do NOT read `.factory/runs/`, `.factory/latest-*`, or prior stage transcripts — you do not have access to them. Treat the supplied GitHub Issue context and codebase as your sole source of truth.
7. Read economically: locate symbols with `rg -n` first and read bounded line ranges (`<=150` lines); never read full files >200 lines or re-read files already read.
