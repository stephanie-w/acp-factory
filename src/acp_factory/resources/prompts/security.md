Execute the `security-auditor` skill against the workspace diff (`git diff main...HEAD`). Do not modify any files:
1. Trace every external or untrusted input from source to sink across Injection, Access Control, Secrets/Host-Path Exposure, Unsafe Defaults, and Dependencies, proving each finding with a concrete `Exploit / Trigger`.
2. Output ONLY the structured security report matching `skills/security-auditor/templates/security-report.md` (starting with `### Security Audit Summary` and ending with `STATUS: <ACCEPTED | REVISION_NEEDED>`).
