Execute the `code-reviewer` and `security-auditor` skills against the workspace diff (`git diff main...HEAD`). Do not modify any files:
1. Classify the change risk (`LOW | MEDIUM | HIGH`), verify every `AC-n` row and test effectiveness, check user-facing `## How to Test` and docs coverage, and audit untrusted inputs and edge cases.
2. Output ONLY the structured review report matching `skills/code-reviewer/templates/review-report.md` (starting with `### Risk Classification:` and ending with `STATUS: <ACCEPTED | REVISION_NEEDED | NEEDS_HUMAN>`).
