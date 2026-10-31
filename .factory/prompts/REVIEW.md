Inspect the git diff and commits in the workspace against the specification and task context. Do not modify any files.

Formatting & Privacy Rules:
- Output ONLY the structured Markdown review (no conversational intro or outro).
- Use only repository-relative `file:line` references (never output `/home/...` or host paths).

End your response with an explicit final line:
- `STATUS: ACCEPTED` (or `VERDICT: PASS`) if all criteria and quality standards are met
- `STATUS: REVISION_NEEDED` (or `VERDICT: FAIL`) with specific `file:line` findings if fixes are needed
- `STATUS: NEEDS_HUMAN` if a human decision is required.
