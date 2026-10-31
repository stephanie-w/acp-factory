Analyze the codebase, the issue description, any prior triage notes in the factory block, and all human comments. Write a complete specification in Markdown covering:
- `## Behavior`
- `## Files to Touch` (repository-relative paths only)
- `## Assumptions` (ONLY for minor, reversible, code-local choices within the requested files)
- `## Decisions` (ONLY for recording answers provided by humans in Issue Comments; write `- None yet.` if none)
- `## Out of Scope`
- `## Acceptance Criteria`

Strict Rules:
1. **No Conversational Wrapper**: Start directly with `# Specification` or `## Behavior` and end with the `STATUS:` line on the very last line. Do not add any intro or postscript after `STATUS:`.
2. **Privacy**: Never include absolute host paths (`/home/...`, `/Users/...`), usernames, or local environment details.
3. **When to Ask Human (`## Open Questions`)**: If fulfilling the issue requires adding/modifying project dependencies (`pyproject.toml`, `uv.lock`, `package.json`, etc.) not explicitly requested in the issue, choosing missing project-wide test/build tooling (e.g. `pytest` / `pytest-asyncio`), or resolving ambiguous product/architectural choices, you MUST add a `## Open Questions` section with numbered options and end with `STATUS: NEEDS_HUMAN`.
4. Otherwise, if everything is unambiguous and requires no unapproved dependency/tooling changes, end with `STATUS: SPEC_READY`.
