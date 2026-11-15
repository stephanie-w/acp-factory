Execute the `issue-triage` skill against the supplied ticket and codebase:
1. Ground all claims in the repository code, check open/recent issues and PRs for duplicates (`DUPLICATE` vs `ALSO_FIXES`), verify single-PR sizing (<400 changed lines), and note available verification tooling.
2. Output ONLY the completed triage report matching `skills/issue-triage/templates/triage-report.md` (starting with `### Codebase Inspection` and ending with `STATUS: <OK | DUPLICATE | TOO_BIG | NEEDS_HUMAN>`).
3. Do NOT read `.factory/runs/`, `.factory/latest-*`, or prior stage transcripts — you do not have access to them. Treat the supplied GitHub Issue and repository codebase as your sole source of truth.
