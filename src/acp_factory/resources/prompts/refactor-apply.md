Execute the `refactor-builder` skill to apply the approved Refactor Contract:
1. Read `AGENTS.md` / `CLAUDE.md` and the approved Refactor Contract (`## Invariant`, `## Target Seam & Dependency Direction`, `## Compatibility & Re-export Policy`, `## Files to Touch`, `## Baseline & Seam Tests`).
2. Run the baseline test suite FIRST before making structural edits and record the passing baseline output.
3. Perform the minimal behavior-preserving structural move strictly within `## Files to Touch`, maintaining the documented compatibility/re-export policy and adding any required characterization or seam tests without weakening existing assertions.
4. Sync all affected documentation pointers (such as `AGENTS.md` codebase maps or module paths) listed in the contract.
5. If `## Latest PR Review Findings` or `## PR Review Feedback` / `## Human Review Comments` are present, fix every actionable finding (`[blocking]`, `[major]`, and `[minor]`) at its root cause.
6. Self-review `git diff` for accidental behavior changes, dead code, or circular imports, run the full verification suite, and commit locally on the current branch without pushing.
7. Output your final refactor summary matching `skills/refactor-builder/templates/refactor-notes.md` (including concrete Baseline Before/After test evidence).
8. Do NOT read `.factory/runs/`, `.factory/latest-*`, or prior stage transcripts — you do not have access to them. Treat the supplied Refactor Contract and codebase as your sole source of truth.
