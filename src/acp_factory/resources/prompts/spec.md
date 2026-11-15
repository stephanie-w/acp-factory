Execute the `spec-writer` skill for the supplied ticket, triage notes (the `## Triage Report` in the Issue Body), and human comments:
1. Apply the two-tier ambiguity filter (`## Assumptions` for reversible code-local choices vs. `## Open Questions` + `STATUS: NEEDS_HUMAN` for costly-to-undo or unapproved dependency/contract/tooling choices).
2. If an existing specification is present in the factory block, operate in Incremental Revision Mode (preserve stable `AC-n` IDs, fold human answers into `## Decisions`, and append new criteria).
3. Include a documentation `AC-n` row and a concrete `## How to Test` guide whenever user-facing CLI commands, flags, or configuration options change.
4. Output ONLY the complete specification matching `skills/spec-writer/templates/spec.md` (starting with `# Specification` and ending with `STATUS: <SPEC_READY | NEEDS_HUMAN>`).
5. Do NOT read `.factory/runs/`, `.factory/latest-*`, or prior stage transcripts — you do not have access to them. Treat the supplied GitHub Issue context and codebase as your sole source of truth.
