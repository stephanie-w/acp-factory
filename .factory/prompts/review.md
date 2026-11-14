Execute the `code-reviewer` and `security-auditor` skills against the workspace diff (`git diff main...HEAD`). Do not modify any files:
1. Conduct a Two-Axis Review:
   - Axis 1 (Spec Fidelity): Verify every `AC-n` row and test falsifiability. If a Builder Implementation Summary is present in the context, treat its claimed tests and Before/After evidence as claims to verify against the diff.
   - Axis 2 (Craftsmanship & Code Smells): Audit against AGENTS.md and the Fowler Code Smell Baseline (flag Speculative Generality, Duplicated Code, Shotgun Surgery, etc. as `[major]`).
2. Audit security boundaries and untrusted inputs (`security-auditor`).
3. Output ONLY the unified review and security report combining `skills/code-reviewer/templates/review-report.md` and `skills/security-auditor/templates/security-report.md`:
   - `### Risk Classification: <LOW | MEDIUM | HIGH>` followed by `- **Reason**: ...`
   - `### Acceptance Criteria Verification` table
   - `### Findings` (code quality/correctness findings or `- None.`)
   - `### Security Audit Summary` (`- **Untrusted Inputs Traced**: ...` and `- **Findings Count**: ...`)
   - `### Security Findings` (security vulnerability findings or `- None.`)
   - End with `STATUS: <ACCEPTED | REVISION_NEEDED | NEEDS_HUMAN>`
4. Do NOT read `.factory/runs/`, `.factory/latest-*`, or prior stage transcripts — you do not have access to them. Treat the supplied GitHub Issue/PR context and workspace diff as your sole source of truth.
