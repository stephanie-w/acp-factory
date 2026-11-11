# Specification

## Problem
<Who is affected, what goes wrong or is missing today, and why it matters in 2–4 sentences.>

## Behavior
1. <Numbered, testable behaviors covering success, invalid/borderline input, and failure handling.>

## Design
<Include ONLY when the change spans multiple modules or services: a concise Mermaid diagram of the flow and the module/interface/schema boundaries.>

## Files to Touch
- `path/to/file.ext` — <concise summary of changes (mark `(new)` if creating a new file)>

## Decisions
- <Each settled decision, who made it (human comment, parent issue), and why — or `- None yet.`>

## Assumptions
- <Reversible, code-local choices made without blocking on human input — or `- None.`>

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
