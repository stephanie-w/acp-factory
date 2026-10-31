---
name: tdd-builder
description: Implement approved specifications using AC-first test-driven development, bad-input probing, root-cause minimal scope, and a 7-point quality standard.
---

# TDD Builder & Implementation Quality Skill

Use this skill during the `build` stage to turn an approved specification into clean, tested, locally committed code that passes review on the first round.

## 1. Execution Workflow

1. **Read First**:
   - Read the repository's `AGENTS.md` / `CLAUDE.md`, the approved specification in the issue context (especially `## Behavior`, `## Files to Touch`, `## Decisions`, and the `AC-n` table), and the surrounding code and tests.
   - If running a revision round after review, read every `[blocking]` and `[major]` finding (`path:line`) and fix the root cause plus obvious variants of the same case.
2. **Checks & Tests First (`AC-n` Driven)**:
   - Turn each `AC-n` row from `## Acceptance Criteria` into an executable test (or local validation check for declarative/IaC files) and watch it fail before or alongside your implementation.
   - For bug fixes, write a test that reproduces the exact bug first.
3. **Minimal Root-Cause Implementation**:
   - Implement the smallest change at the root cause that satisfies the specification.
   - Stay strictly within `## Files to Touch`; do not perform drive-by refactors or reformat unrelated files.
4. **Probe Bad & Borderline Inputs**:
   - Whenever code parses or accepts input (CLI arguments, payloads, configs, files, dates, strings, batches, numerical features), test realistic bad and borderline inputs:
     - Empty values, whitespace-only strings, missing fields, out-of-range numbers, `NaN`/`Inf`/`null` values, malformed paths, or duplicate entries.
   - Every bad input must be understood or rejected clearly with an explicit error—never silently coerced into wrong data.
5. **Self-Review & Local Commit**:
   - Inspect `git diff` against every `AC-n` row and the 7-point quality standard below.
   - Remove temporary debug code, scratch files, or comments that only make sense with the ticket in mind.
   - Commit locally on the issue branch with a clear commit message. Never push or merge directly.

## 2. The 7-Point Quality Standard

The repository's own `AGENTS.md` comes first. The reviewer will hold your work to this rubric:

1. **Scope**: Only what the spec requires. Fix causes, not symptoms. No unrequested features or refactors.
2. **Fit**: Match existing naming, typing, and architectural patterns. Reuse existing utilities; never add dependencies unless explicitly approved in the spec.
3. **Correctness**: Handle real failure modes explicitly. Never swallow exceptions silently. Preserve public contracts unless the spec changes them (and update all callers).
4. **Clarity**: Prefer small functions, descriptive names, and straightforward control flow over clever abstractions.
5. **Comments**: Explain *why*, never *what*. Comments must read naturally to an engineer who never saw the ticket.
6. **Verification & Tests**: Test behavior through public interfaces rather than internal implementation details. Never weaken, skip, or mock away the core logic under test just to get green checks.
7. **Security**: No hardcoded secrets, tokens, or local host paths (`/home/...`, `/Users/...`). Parameterize queries and shell invocations; validate paths and external inputs.
