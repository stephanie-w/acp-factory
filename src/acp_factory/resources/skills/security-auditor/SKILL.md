---
name: security-auditor
description: Audit branch changes from an attacker's perspective by tracing untrusted input to sinks and proving vulnerabilities with concrete trigger inputs.
---

# Security Auditor Skill (Exploit-Proven Review)

Use this skill during `review` (or as a dedicated security check) to evaluate a change for one thing only: **what an attacker or untrusted caller could do with it**.

## Resources in This Skill Package
- **Template**: `templates/security-report.md` — canonical Markdown security audit report skeleton.
- **Validation Script**: `scripts/validate_security.py` — deterministic validator verifying `### Security Audit Summary`, `### Security Findings`, severity/status consistency, and privacy rules.

## 1. Security Audit Procedure

1. **Trace Untrusted Input from Source to Sink**:
   - Inspect `git diff main...HEAD` and read modified files in full.
   - Identify every external or untrusted input touched by the change: CLI arguments, HTTP request parameters/bodies/headers, environment variables, file paths, uploaded files, serialized artifacts/models, or responses from external services.
2. **Audit Across 5 Security Vectors**:
   - **Injection**: Shell commands (`subprocess` with `shell=True` or unquoted interpolation), SQL queries built via string formatting, path traversal (`..` or unvalidated Paths escaping workspace roots), template/HTML injection, or unsafe deserialization (`pickle`, `yaml.load` without `SafeLoader`, `eval`/`exec`).
   - **Access Control & Privilege**: Missing authentication/authorization checks, overly broad IAM permissions (`*`), or ability to read/mutate resources belonging to another tenant/user.
   - **Secrets & Privacy Exposure**: API keys, tokens, passwords, private URLs, or local host paths (`/home/...`, `/Users/...`) leaked in source code, default configs, test fixtures, logs, error tracebacks, or GitHub outputs.
   - **Unsafe Defaults & Resource Exhaustion**: Permissive CORS (`*`), SSRF / open redirects to user-controlled URLs, unbounded memory/batch/file reads without size or timeout limits, or unpinned container/model artifacts (`:latest`).
   - **Dependencies & Supply Chain**: Newly added packages—are they explicitly approved in the spec, real (not typosquatted), maintained, and pinned in lockfiles?
3. **Prove Every Suspected Hole**:
   - For every `[blocking]` or `[major]` security finding, specify the **concrete input, payload, or configuration state (`Exploit / Trigger`)** that triggers the vulnerability.
   - Report only real security issues; leave general code style or non-security correctness to `code-reviewer`.

## 2. Security Severity Rubric

- **`blocking`**: Exploitable vulnerability now—command/SQL/path injection, secret or host-path leak, broken access control, unsafe deserialization, or unapproved/typosquatted dependency.
- **`major`**: A security weakness that requires one additional mistake or non-default condition to become exploitable (e.g., missing timeout/size bound on external input, overly broad permissions).
- **`minor`**: Defense-in-depth hardening recommendation that is not currently exploitable.

## 3. Output Contract

When running as a standalone security check, output ONLY the structured Markdown report matching `templates/security-report.md` ending with `STATUS:` (validated by `scripts/validate_security.py`).
