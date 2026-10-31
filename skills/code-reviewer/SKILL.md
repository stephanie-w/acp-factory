---
name: code-reviewer
description: Skeptically review branch diffs against specifications, verify test effectiveness and AC-n coverage, classify change risk, and grade findings with a 3-tier severity rubric.
---

# Skeptical Code Reviewer & Risk Classification Skill

Use this skill during the `review` stage (or review checks) to verify a branch as if someone you do not trust wrote it, catching every blocking or major defect before a human reviews the pull request.

## 1. Review Procedure

1. **Read Context & Guidance**:
   - Read `AGENTS.md` / `CLAUDE.md`, the approved specification (`## Behavior`, `## Files to Touch`, `## Decisions`, `## Acceptance Criteria`), and any prior review findings.
2. **Inspect the Full Diff**:
   - Run `git status --short`, `git log --oneline -n 5`, and `git diff main...HEAD` (or the base branch) and read changed files in full context.
   - Treat commit messages and summaries as claims to verify against the diff, never as proof.
3. **Verify Every `AC-n` & Test Effectiveness**:
   - Confirm every `AC-n` row in `## Acceptance Criteria` is satisfied by the code and verified by a test or deterministic check.
   - Inspect new tests critically: would each test actually fail if the production change were reverted, or does it only test mocks/tautologies? A test that still passes when the fix is broken is at least `major`.
4. **Check Bad Input & Edge Cases**:
   - Trace how changed code handles empty, malformed, boundary, or unexpected inputs. Anything that silently corrupts or saves wrong data is `blocking`.
5. **Check Scope & Repo Standards**:
   - Flag unapproved files outside `## Files to Touch`, unapproved dependency changes, swallowed exceptions, dead code, stale docs, or leaked local paths (`/home/...`).

## 2. Severity Rubric

| Severity | Meaning | Blocks Acceptance? |
| :--- | :--- | :--- |
| `blocking` | Wrong behavior, data loss, silent data corruption, security vulnerability, broken contract, failing check, missing required `AC-n` test/check, or drift from the spec | **Yes** (`STATUS: REVISION_NEEDED`) |
| `major` | Real maintenance or reliability cost: likely unhandled edge case, weak/tautological test, violation of `AGENTS.md`, unapproved dependency, or stale documentation | **Yes** (`STATUS: REVISION_NEEDED`) |
| `minor` | Safe to ship: minor naming, wording, or small local simplification | **No** (`STATUS: ACCEPTED` if no `blocking`/`major` findings) |

Rules for grading findings:
- A fix that misses an obvious variant of the same bug is at least `major`.
- Drop purely subjective style opinions not backed by `AGENTS.md` or the specification.
- Mark each finding `[unambiguous]` (implementer can fix directly) or `[needs_human]` (only when neither the spec, codebase, nor an obviously safer choice settles it—provide 2–3 options and a recommendation, and return `STATUS: NEEDS_HUMAN`).

## 3. Change Risk Classification (`LOW` | `MEDIUM` | `HIGH`)

Always classify the change's risk tier based on what the diff touches:
- **Path Rules (Always `HIGH`)**:
  - CI/CD, build, and deployment configs (`.github/`, `Dockerfile`, `justfile`, `Makefile`, `deploy/`)
  - Infrastructure-as-Code, IAM, permissions, auth, sessions, billing, or secrets (`*.tf`, `helm/`, `k8s/`, auth modules)
  - Database schemas, migrations, or code that deletes/rewrites persistent data or model artifacts
- **`MEDIUM`**: Ordinary feature/product changes, bug fixes, new endpoints/CLI commands, pipeline definitions, or shared helpers.
- **`LOW`**: Documentation, comments, tests-only changes, or isolated internal refactors with no behavior change.

## 4. Review Report Format

Output ONLY the structured Markdown report ending with the `STATUS:` line:

```markdown
### Risk Classification: <LOW | MEDIUM | HIGH>
- **Reason**: <one sentence naming the touched files/behaviors and any path rule triggered>

### Acceptance Criteria Verification
| ID | Status | Evidence (`file:line` or test/check) |
| :--- | :--- | :--- |
| AC-1 | PASS / FAIL | `<repo-relative file:line and test name>` |

### Findings
1. `[blocking|major|minor]` `[unambiguous|needs_human]` `path/to/file.py:123`
   - **Problem**: <factual description of the defect>
   - **Impact**: <what breaks or goes wrong>
   - **Fix**: <concrete action to resolve>
*(Write `- None.` if no findings.)*

STATUS: <ACCEPTED | REVISION_NEEDED | NEEDS_HUMAN>
```
