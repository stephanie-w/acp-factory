Audit the git diff and changed files from an attacker's perspective. Do not modify any files:
1. Trace every external or untrusted input touched by the diff from source to sink.
2. Check across 5 vectors: Injection (shell, SQL, path traversal, templates, unsafe deserialization), Access Control & IAM, Secrets & Host-Path Exposure, Unsafe Defaults & Resource Exhaustion, and Dependencies.
3. For every finding, provide the repository-relative `file:line`, severity (`[blocking]`, `[major]`, `[minor]`), `Problem`, concrete `Exploit / Trigger` input, and `Fix`.

Formatting & Privacy Rules:
- Output ONLY the structured Markdown security report (no conversational intro or outro).
- Never output local machine paths (`/home/...`, `/Users/...`).

End your response with an explicit final line:
- `STATUS: ACCEPTED` (or `VERDICT: PASS`) if there are no `blocking` or `major` security findings
- `STATUS: REVISION_NEEDED` (or `VERDICT: FAIL`) if any `blocking` or `major` security findings exist.
