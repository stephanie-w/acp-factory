### Risk Classification: <LOW | MEDIUM | HIGH>
- **Reason**: <one sentence naming the touched modules, compatibility surface, and any path rule triggered>

### Behavior Preservation & Seam Verification
| Dimension | Status | Evidence (`file:line` or test/check) |
| :--- | :--- | :--- |
| Behavior & Invariant Preservation | PASS / FAIL | `<repo-relative file:line and verification command>` |
| Compatibility & Re-export Policy | PASS / FAIL | `<re-export / caller check>` |
| Seam Depth & Dependency Direction | PASS / FAIL | `<import direction and seam boundary check>` |
| Test Integrity & Seam Coverage | PASS / FAIL | `<existing assertions intact + new seam tests>` |
| Documentation Pointer Sync | PASS / FAIL | `<AGENTS.md / docs updated or N/A>` |

### Findings
1. `[blocking|major|minor]` `[unambiguous|needs_human]` `path/to/file.py:123`
   - **Problem**: <factual description of the defect>
   - **Impact**: <what behavior or boundary is compromised>
   - **Fix**: <concrete action to resolve>
*(Write `- None.` if no findings.)*

STATUS: <ACCEPTED | REVISION_NEEDED | NEEDS_HUMAN>
