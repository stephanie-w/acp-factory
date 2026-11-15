---
name: issue-triage
description: Evaluate incoming tickets against the codebase, detect duplicates or related issues, and verify single-PR sizing (<400 lines) with build-order split suggestions.
---

# Issue Triage & Scope Heuristic Skill

Use this skill during the `triage` stage to ensure the builder never starts from a vague, duplicate, or oversized ticket.

## Resources in This Skill Package
- **Template**: `templates/triage-report.md` — canonical Markdown output skeleton.
- **Validation Script**: `scripts/validate_triage.py` — deterministic validator verifying required sections, privacy rules, and terminal `STATUS:` token.

## 1. Triage Procedure

1. **Read & Ground in the Codebase**:
   - Read the issue title, description, and any human comments or linked parent issues.
   - Read the repository's agent guidance (`AGENTS.md`, `CLAUDE.md`, `README.md`) and inspect the actual files and modules the ticket touches.
   - Verify every claim about current behavior against the code. For bug reports, trace the root cause in the codebase.

2. **Check for Duplicates & Related Work**:
   - Inspect existing open and recently closed issues and pull requests (`gh issue list --state all --limit 100`, `gh pr list --state all --limit 50`).
   - Two tickets are duplicates when a single change fixes both:
     - **`DUPLICATE`**: If this ticket is a copy of an earlier open issue or the work is already merged, stop and report `STATUS: DUPLICATE` citing the canonical issue/PR number.
     - **`ALSO_FIXES`**: If this ticket is the primary one and a newer open issue duplicates it, continue and list the related issue under `ALSO_FIXES`.

3. **Size It (One Ticket = One Reviewable PR)**:
   - A valid ticket must fit a single pull request that a human can review in one sitting: **one cohesive area, roughly under ~400 changed lines**.
   - If the ticket spans multiple independent subsystems or exceeds a single reviewable PR, report `STATUS: TOO_BIG` and provide a **Suggested Split**:
     - A numbered list of smaller ticket titles in **build order**, with one line of concrete scope each.
     - Never create the split issues yourself; let the human review the split first.

4. **Check Project Infrastructure**:
   - Note which existing verification commands (linters, typecheckers, test runners, DAG compilers, or IaC validators) apply to the touched area, or flag if project-wide test/validation infrastructure is missing so the `spec` stage can escalate it.

## 2. Output Contract

Format your triage report strictly matching `templates/triage-report.md` (with no conversational intro or outro). Your output is validated by `scripts/validate_triage.py`.
