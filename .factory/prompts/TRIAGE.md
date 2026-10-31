Inspect the repository and evaluate the supplied ticket/task. Check for duplicates and confirm whether the ticket is small enough for a single pull request.

Formatting & Privacy Rules:
- Start directly with `### Codebase Inspection` (no conversational intro or outro).
- Use only repository-relative file paths (never output `/home/...` or host paths).
- Note any missing project infrastructure (e.g. missing `tests/` directory or test runner) so the `spec` stage can address it.

End your response with an explicit final line:
- `STATUS: OK` if the ticket is valid and sized for 1 PR
- `STATUS: DUPLICATE`, `STATUS: TOO_BIG`, or `STATUS: NEEDS_HUMAN` otherwise.
