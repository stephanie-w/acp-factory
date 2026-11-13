Execute the `tdd-builder` skill to implement the approved specification:
1. Read `AGENTS.md` / `CLAUDE.md` and turn each `AC-n` row from `## Acceptance Criteria` into a failing test or deterministic check first.
2. Implement the minimal root-cause change strictly within `## Files to Touch`, probing bad and borderline inputs so invalid data is explicitly rejected.
3. Self-review `git diff` against the 7-point quality standard and commit your changes locally on the current branch without pushing.
4. Output your final implementation summary matching `skills/tdd-builder/templates/implementation-notes.md` (including concrete Before/After test evidence).
