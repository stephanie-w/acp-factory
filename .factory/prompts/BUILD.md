Read relevant project instructions (`AGENTS.md`, `CLAUDE.md`) and the approved specification:
1. Turn each `AC-n` row from `## Acceptance Criteria` into an executable test (or local validation check for declarative/pipeline/IaC files) and watch it fail first. For bugs, reproduce the bug in a test first.
2. Implement the smallest root-cause change that satisfies the specification. Do not modify or reformat unrelated files outside `## Files to Touch` or add unapproved dependencies.
3. Probe realistic bad and borderline inputs (empty values, invalid types, boundary numbers, `NaN`/`Inf`/`null`, malformed paths/payloads) to ensure each is clearly rejected rather than silently coerced into wrong data.
4. Self-review `git diff` against the 7-point quality standard (Scope, Fit, Correctness, Clarity, Comments explain why not what, Verification/Tests on public interfaces, Security/Privacy with no `/home/...` paths).
5. Commit your changes locally on the current branch and do not push or merge.
