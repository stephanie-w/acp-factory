---
name: retro
description: Conduct an engineering retrospective on coding sessions or automated factory runs to improve the agent environment, automated guardrails, reviewer rubrics, and navigation pointers.
---

# Engineering Retrospective Skill (`retro`)

Use this skill to analyze completed coding sessions, multi-attempt transcripts, or review loops and propose concrete, minimal improvements to the **agent environment** so future runs succeed faster and with fewer retries.

## Core Principle: Implementation vs. Review Context Pressure

All engineering work flows through two distinct cognitive postures:
1. **Implementation (`build` / interactive coding)**: Has the **highest context pressure**. The agent must explore the codebase, edit files, run tests, and debug failures. Keep always-on context (`AGENTS.md` / `CLAUDE.md`) as lean as possible—primarily **navigation pointers** and non-negotiable operational commands.
2. **Review (`review` / automated checks)**: Has the **lowest context pressure**. The reviewer inspects a bounded diff (`git diff`) and does not need to write or debug code. Push subjective or cross-file coding standards to the **reviewer** (or deterministic checks), never into bloated implementation prompts.

---

## Retrospective Procedure

### Step 1: Read Primary Session Sources
Inspect the session logs, transcripts, check histories, or issue/PR review rounds specified by the user or stage prompt (e.g., `.factory/runs/<run-id>/`, `~/.pi/agent/sessions/`, or the current session history). Focus on:
- Runs that required multiple retry attempts (`attempts_used > 1`) or failed validation checks.
- Issues that required multiple review rounds (`review_round > 1`) or human escalations (`NEEDS_HUMAN`).
- Turns with excessive file searches, failed shell commands, or high token usage.

### Step 2: Evaluate Across 7 Environment Categories

Look for evidence-backed improvement candidates in these categories:

1. **Navigation**:
   - How many tool calls did the agent spend finding the right files, entrypoints, or runtime artifacts?
   - Are there hidden couplings between modules or undocumented directories?
   - *Action*: Propose a 1–2 line **navigation pointer** in `AGENTS.md` (or a codebase map) when the agent wasted turns hunting for where something lives.

2. **Automated Checks (Highest Leverage)**:
   - Did the agent or reviewer catch a mistake that a deterministic check (`[checks.*]`, linter rule, typecheck, schema/template validator, or `just` recipe) could catch automatically?
   - Read the repo's existing check commands first (`pyproject.toml`, `justfile`, `.factory/config.toml`, CI workflows) so an existing check that sits unwired is wired up rather than reinvented.
   - *Rule*: Any **mechanical** requirement (syntactic pattern, forbidden API/path, required heading/status format, import boundary) gets a **deterministic check**, full stop—never a prose instruction.

3. **Coding Standards & Reviewer Rubric**:
   - Did the reviewer miss a real defect, or conversely flag noisy false positives?
   - Classify the issue first: if it is mechanical, build an automated check (Category 2). Reserve reviewer skills/rubrics (`code-reviewer`, `security-auditor`, `CODING_STANDARDS.md`) for genuine **judgment calls** (test effectiveness, revert-test heuristic, domain edge cases).

4. **`AGENTS.md` Diet**:
   - Are there long prose rules in `AGENTS.md` (local or global) that belong in an automated check, a domain skill, or a reviewer rubric instead?
   - *Use when*: `AGENTS.md` is growing large or contains task-specific instructions that do not apply to every turn.

5. **Tool Economy**:
   - Did the agent run expensive, un-truncated, or redundant tool calls (e.g., raw `curl` instead of `html2md --stdout`, `grep` over vendor dirs instead of `rg`, running the full slow test suite instead of targeted tests)?
   - *Action*: Recommend helper scripts, CLI flags, or check scoping to save tokens and wall-clock time.

6. **No-ops**:
   - Identify instructions in steering files (`AGENTS.md`, prompts, personas, or skills) that agents consistently ignore or that do not change behavior.
   - *Action*: Propose deleting or tightening them.

7. **Information Access**:
   - Was the agent blocked or forced to guess because crucial local context (environment variables, CLI `--help`, dry-run output, or fixture schemas) was inaccessible?
   - *Action*: Expose read-only inspection commands or environment pointers.

---

## Step 3: Output Ranked Findings & Concrete Diffs

Present findings ordered by **impact / severity** (`HIGH`, `MEDIUM`, `LOW`). Omit any category that has no evidence-backed finding. For every finding, include:
1. **Category & Severity**: e.g., `[HIGH] Automated Checks` or `[MEDIUM] Navigation`
2. **Evidence**: Exact run ID, transcript turn, check failure, or file/command that demonstrated the friction.
3. **Proposed Fix (Unified Diff)**: Exact `diff` targeting the specific file (`AGENTS.md`, `.factory/config.toml`, `justfile`, or skill/prompt file).
