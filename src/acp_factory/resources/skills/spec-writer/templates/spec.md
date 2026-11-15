# Specification

## Problem
<Who is affected, what goes wrong or is missing today, and why it matters in 2–4 sentences.>

## Design & Risk
- **Door:** <Two-way (reversible code-local choice) | One-way (costly to undo: public API, storage, dependency)>
- **Blast Radius:** <Isolated | Subsystem | Public Surface>
- **Compatibility / Version Bump:** <None (internal) | Breaking / major dependency bump — note downstream impact & whether to bump package `version`>

### Visual Sketch
<Pick the SMALLEST visual that makes the change clear. Avoid bloated diagrams. Prefer:>
- Call tree diff (runtime flow changes)
- Logic / state pseudocode diff (behavioral changes)
- Interface / signature diff (signature or type changes)
- Shallow directory diff (restructuring or new files)
(Use Mermaid ONLY if illustrating multi-process/service choreography.)

## Behavior
1. <Numbered, concise, testable behaviors covering success, invalid/borderline input, and failure handling.>

## Files to Touch
- `path/to/file.ext` — <concise summary of changes (mark `(new)` if creating a new file)>

## Decisions
- <Each settled decision, who made it (human comment, parent issue), and why — or `- None yet.`>

## Assumptions
- <Two-way door choices made without blocking on human input — or `- None.`>

## Acceptance Criteria
| ID | Done when | How to check |
| :--- | :--- | :--- |
| AC-1 | <Observable outcome> | <Test or local verification command> |
| AC-2 | Relevant documentation (`README.md`, `docs/`) updated with <feature/behavior> | <Inspection or command e.g. `git diff --exit-code README.md`> |

## How to Test
<MANDATORY for user-facing CLI/config changes: minimal configuration snippet, exact terminal command to run, and expected output.>

## Out of Scope
- <What this ticket explicitly does not change.>

STATUS: <SPEC_READY | NEEDS_HUMAN>
