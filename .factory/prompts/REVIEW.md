Inspect the git diff and commits in the workspace against the specification and task context as a skeptic. Do not modify any files:
1. Classify the change risk (`### Risk Classification: LOW | MEDIUM | HIGH`, elevating CI/build/deploy configs, IaC/IAM/auth/secrets, and schema/data deletion paths to `HIGH`).
2. Verify every `AC-n` row in `### Acceptance Criteria Verification` (`| ID | Status | Evidence |`) and check that new tests actually test behavior and would fail if the fix were reverted.
3. Audit bad-input handling and security (injection, access control, secrets/host-path leaks, unsafe defaults, unapproved dependencies).
4. Verify user testability: If the change introduces or modifies user-facing CLI commands, flags, or configuration sections, verify that a working `## How to Test` section with a minimal working configuration example exists in the specification. If missing, flag as `[major]`.
5. List findings under `### Findings` graded strictly as `[blocking]`, `[major]`, or `[minor]` and marked `[unambiguous]` or `[needs_human]` with `file:line`, `Problem`, `Impact`, and `Fix`.

Formatting & Privacy Rules:
- Output ONLY the structured Markdown review (no conversational intro or outro).
- Use only repository-relative `file:line` references (never output `/home/...`, `/Users/...`, or host paths).

End your response with an explicit final line:
- `STATUS: ACCEPTED` (or `VERDICT: PASS`) if there are no `blocking` or `major` findings
- `STATUS: REVISION_NEEDED` (or `VERDICT: FAIL`) if any `blocking` or `major` unambiguous findings exist
- `STATUS: NEEDS_HUMAN` if any finding is marked `[needs_human]` (include 2–3 options and your recommendation).
