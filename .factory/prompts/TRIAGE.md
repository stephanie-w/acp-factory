Inspect the repository and evaluate the supplied ticket/task against the codebase and existing issues/PRs:
1. Verify claims about current behavior against the actual code and identify the touched repository-relative files.
2. Check for duplicates (`DUPLICATE` if this ticket is a copy or already merged; `ALSO_FIXES` if a newer open issue duplicates this one).
3. Verify single-PR sizing: must fit one reviewable PR in one cohesive area (roughly under ~400 changed lines). If larger, include a `### Suggested Split` section with numbered ticket titles in build order (one line of scope each) and return `STATUS: TOO_BIG`.
4. Note available verification commands (linters, tests, DAG/schema/IaC validators) or missing project infrastructure for the `spec` stage.

Formatting & Privacy Rules:
- Start directly with `### Codebase Inspection` (no conversational intro or outro).
- Use only repository-relative file paths (never output `/home/...`, `/Users/...`, or host paths).

End your response with an explicit final line:
- `STATUS: OK` if the ticket is valid and sized for 1 PR
- `STATUS: DUPLICATE`, `STATUS: TOO_BIG`, or `STATUS: NEEDS_HUMAN` otherwise.
