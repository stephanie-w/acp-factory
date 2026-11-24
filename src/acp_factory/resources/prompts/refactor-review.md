Execute the `refactor-reviewer` skill against the workspace diff (`git diff main...HEAD`). Do not modify any files:
1. Conduct a Two-Axis Refactor Review:
   - Axis 1 (Behavior Preservation & Contract Fidelity): Verify zero unintended behavior changes, public API/re-export compliance with `## Compatibility & Re-export Policy`, and intact test assertions (flag any weakened, skipped, or deleted behavior assertions as `[blocking]`). If Builder Refactor Notes are present, treat claimed baseline/after results as claims to verify against the diff.
   - Axis 2 (Seam Depth, Dependency Direction & Doc Sync): Verify the new seam is genuinely deep (not a shallow pass-through wrapper), has clean acyclic dependency direction, leaves no dead code or out-of-scope edits, and updates documentation pointers (`AGENTS.md`, etc.) listed in the contract.
2. Output ONLY the structured review report matching `skills/refactor-reviewer/templates/refactor-review-report.md`:
   - `### Risk Classification: <LOW | MEDIUM | HIGH>` followed by `- **Reason**: ...`
   - `### Behavior Preservation & Seam Verification` table
   - `### Findings` (code quality/preservation/seam findings or `- None.`)
   - End with `STATUS: <ACCEPTED | REVISION_NEEDED | NEEDS_HUMAN>`
3. Do NOT read `.factory/runs/`, `.factory/latest-*`, or prior stage transcripts — you do not have access to them. Treat the supplied Refactor Contract and workspace diff as your sole source of truth.
