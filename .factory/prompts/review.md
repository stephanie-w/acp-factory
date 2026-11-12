Execute the `code-reviewer` and `security-auditor` skills against the workspace diff (`git diff main...HEAD`). Do not modify any files:
1. Classify the change risk (`LOW | MEDIUM | HIGH`), verify every `AC-n` row and test effectiveness, check user-facing `## How to Test` and docs coverage, and audit untrusted inputs and edge cases.
2. Output ONLY the unified review and security report combining `skills/code-reviewer/templates/review-report.md` and `skills/security-auditor/templates/security-report.md`:
   - `### Risk Classification: <LOW | MEDIUM | HIGH>` followed by `- **Reason**: ...`
   - `### Acceptance Criteria Verification` table
   - `### Findings` (code quality/correctness findings or `- None.`)
   - `### Security Audit Summary` (`- **Untrusted Inputs Traced**: ...` and `- **Findings Count**: ...`)
   - `### Security Findings` (security vulnerability findings or `- None.`)
   - End with `STATUS: <ACCEPTED | REVISION_NEEDED | NEEDS_HUMAN>`
