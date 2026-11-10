Analyze the codebase, the issue description, any prior triage notes in the factory block, and all human comments. Write a complete specification in Markdown covering:
- `## Problem` (2–4 sentences on what goes wrong or is missing and why it matters)
- `## Behavior` (numbered, testable behaviors covering success, invalid/borderline input, and failure handling)
- `## Design` (include ONLY when the change spans multiple modules/services: a concise Mermaid diagram and interface/schema changes)
- `## Files to Touch` (repository-relative paths only, verified to exist or explicitly marked new)
- `## Decisions` (ONLY for recording settled answers from humans in Issue Comments or linked parent issues; write `- None yet.` if none)
- `## Assumptions` (ONLY for minor, reversible, code-local choices within the requested files)
- `## Acceptance Criteria` (a Markdown table `| ID | Done when | How to check |` with `AC-1`, `AC-2`, ... where every row states an observable result and a concrete local test or verification command that could fail; never renumber existing `AC-n` IDs. For any change modifying user-facing CLI commands, flags, configuration options, or workflows, an explicit row verifying documentation update in README.md or docs/ is MANDATORY)
- `## How to Test` (MANDATORY for any change touching user-facing CLI commands, flags, or configuration options: provide a concrete minimal configuration snippet, the exact terminal command to run, and the expected output for a human operator)
- `## Out of Scope`

Strict Rules:
1. **No Conversational Wrapper**: Start directly with `# Specification` or `## Problem` / `## Behavior` and end with the `STATUS:` line on the very last line. Do not add any intro or postscript after `STATUS:`.
2. **Privacy**: Never include absolute host paths (`/home/...`, `/Users/...`), usernames, or local environment details.
3. **When to Ask Human (`## Open Questions`)**: Ask ONLY about choices that nothing settles AND that would be costly to undo—including adding/modifying project dependencies (`pyproject.toml`, `uv.lock`, `package.json`, container images, Terraform providers) not explicitly requested in the issue, choosing missing project-wide test/validation tooling, breaking public contracts, or cloud/compute/IAM footprint choices. Provide 2–3 numbered options with your recommendation and end with `STATUS: NEEDS_HUMAN`.
4. **Human Testability**: If the ticket introduces or modifies user-facing CLI commands, flags, or configuration sections, the specification is NOT ready (`STATUS: SPEC_READY`) without a concrete `## How to Test` section with minimal working configuration examples and exact commands.
5. **Mandatory Documentation Acceptance Criterion**: If the ticket introduces or modifies user-facing CLI commands, flags, configuration options/precedence, public APIs, or workflows, the `## Acceptance Criteria` table MUST include a dedicated `AC-n` row verifying that relevant documentation (e.g. `README.md`, `docs/`, or CLI `--help`) is updated. The specification is NOT ready (`STATUS: SPEC_READY`) without this row.
6. **Revision Mode (Incremental Updates)**: If an existing specification is present under `## Current Factory Specification / Status Block` (or a `Spec Revision Brief` is provided), do NOT re-explore the codebase from scratch or rewrite the specification from zero. Treat the existing specification as the baseline, surgically incorporate the feedback from `## Human Comments`, record decisions in `## Decisions`, keep existing `AC-n` rows stable, add newly requested criteria as subsequent `AC-n` rows, and output the updated complete specification.
7. Otherwise, if everything is unambiguous and requires no unapproved dependency/tooling/infrastructure changes, end with `STATUS: SPEC_READY`.
