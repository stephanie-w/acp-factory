"""Tests for Phase 1 bundled refactoring prompts, skills, templates, and validators."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from acp_factory.resources import (
    get_bundled_resources_dir,
    load_bundled_text,
    parse_frontmatter_summary,
    resolve_resources,
)


def _load_script_module(script_path: Path, module_name: str) -> Any:
    """Dynamically import a standalone validator script for unit testing."""
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_refactor_plan_validator() -> Any:
    script = (
        get_bundled_resources_dir()
        / "skills"
        / "refactor-planner"
        / "scripts"
        / "validate_refactor_plan.py"
    )
    return _load_script_module(script, "validate_refactor_plan")


def _load_review_validator() -> Any:
    script = (
        get_bundled_resources_dir()
        / "skills"
        / "code-reviewer"
        / "scripts"
        / "validate_review.py"
    )
    return _load_script_module(script, "validate_review")


VALID_READY_CONTRACT = """\
# Refactor Contract

## Problem & Motivation
`loop.py` mixes dispatch with prompt construction.

## Invariant
- All existing CLI flags and artifact schemas remain unchanged.

## Target Seam & Dependency Direction
`loop.py -> prompts.py` (acyclic).

## Compatibility & Re-export Policy
- Internal-only migration; update callers directly.

## Files to Touch
- `src/acp_factory/runner/prompts.py` — (new) prompt builders
- `src/acp_factory/runner/loop.py` — delegate to `prompts.py`
- `AGENTS.md` — update codebase map

## Baseline & Seam Tests
- `just typecheck && just test-fast`

## Decisions
- None yet.

## Assumptions
- Keep helper names identical.

## Out of Scope
- No behavioral changes.

STATUS: READY
"""


def test_bundled_refactor_resources_exist_and_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundled refactoring prompts, skills, and templates resolve without external catalogs."""
    monkeypatch.delenv("FACTORY_RESOURCES_DIR", raising=False)
    monkeypatch.delenv("FACTORY_RESOURCES_DIRS", raising=False)

    bundled = get_bundled_resources_dir()
    for prompt_name in (
        "refactor-plan.md",
        "refactor-apply.md",
        "refactor-review.md",
    ):
        assert (bundled / "prompts" / prompt_name).is_file()
        assert load_bundled_text(f"prompts/{prompt_name}").strip()

    refactor_skills = ["refactor-planner", "refactor-builder", "refactor-reviewer"]
    resolved = resolve_resources(
        persona="agents/architect.agent.md",
        skills=refactor_skills,
        workspace=tmp_path,
        resource_dirs=[],
        discover_apm=False,
    )
    assert len(resolved.skills) == len(refactor_skills)
    assert all(sp.is_relative_to(bundled) for sp in resolved.skills)

    for skill_path, expected_name in zip(resolved.skills, refactor_skills, strict=True):
        meta = parse_frontmatter_summary(skill_path)
        assert meta.get("name") == expected_name
        assert meta.get("description")

    assert (
        bundled / "skills" / "refactor-planner" / "templates" / "refactor-plan.md"
    ).is_file()
    assert (
        bundled
        / "skills"
        / "refactor-planner"
        / "scripts"
        / "validate_refactor_plan.py"
    ).is_file()
    assert (
        bundled / "skills" / "refactor-builder" / "templates" / "refactor-notes.md"
    ).is_file()
    assert (
        bundled
        / "skills"
        / "refactor-reviewer"
        / "templates"
        / "refactor-review-report.md"
    ).is_file()


def test_validate_refactor_plan_accepts_valid_ready_contract() -> None:
    """A complete READY Refactor Contract passes validation and normalizes cleanly."""
    validator = _load_refactor_plan_validator()
    preamble = "Sure, here is the contract:\n\n" + VALID_READY_CONTRACT + "\n---\n"
    errors, normalized = validator.validate_refactor_plan_text(preamble)
    assert errors == []
    assert normalized.startswith("# Refactor Contract\n")
    assert normalized.endswith("\n\nSTATUS: READY\n")


def test_validate_refactor_plan_accepts_split_needed_and_needs_human() -> None:
    """SPLIT_NEEDED with Proposed Split and NEEDS_HUMAN with Open Questions pass."""
    validator = _load_refactor_plan_validator()

    split_text = (
        "# Refactor Contract\n\n"
        "## Proposed Split\n"
        "1. Extract seam A.\n"
        "2. Extract seam B.\n\n"
        "STATUS: SPLIT_NEEDED\n"
    )
    errors, normalized = validator.validate_refactor_plan_text(split_text)
    assert errors == []
    assert normalized.endswith("STATUS: SPLIT_NEEDED\n")

    human_text = (
        "# Refactor Contract\n\n"
        "## Open Questions\n"
        "1. Should legacy re-exports stay?\n"
        "   - Option A (Recommended): Keep shim.\n"
        "   - Option B: Remove.\n\n"
        "STATUS: NEEDS_HUMAN\n"
    )
    errors, normalized = validator.validate_refactor_plan_text(human_text)
    assert errors == []
    assert normalized.endswith("STATUS: NEEDS_HUMAN\n")


@pytest.mark.parametrize(
    ("raw_text", "expected_fragment"),
    [
        ("", "empty refactor plan"),
        ("# Refactor Contract\n\n## Invariant\n- Keep behavior.\n", "Missing terminal"),
        (
            "# Refactor Contract\n\n## Invariant\n- Keep.\n\nSTATUS: BOGUS\n",
            "Invalid refactor plan STATUS 'BOGUS'",
        ),
        (
            "# Refactor Contract\n\n## Problem & Motivation\nToo big.\n\nSTATUS: SPLIT_NEEDED\n",
            "Proposed Split",
        ),
        (
            "# Refactor Contract\n\n## Problem & Motivation\nUnclear.\n\nSTATUS: NEEDS_HUMAN\n",
            "Open Questions",
        ),
        (
            VALID_READY_CONTRACT.replace("## Assumptions", "## Open Questions"),
            "STATUS is READY, but '## Open Questions' is present",
        ),
        (
            VALID_READY_CONTRACT.replace("## Invariant\n", ""),
            "Missing required section '## Invariant'",
        ),
        (
            VALID_READY_CONTRACT + "\nSee /home/alice/repo/file.py\nSTATUS: READY\n",
            "Privacy violation",
        ),
    ],
)
def test_validate_refactor_plan_rejects_invalid_inputs(
    raw_text: str, expected_fragment: str
) -> None:
    """Missing sections, mismatched statuses, bad tokens, and host paths fail validation."""
    validator = _load_refactor_plan_validator()
    errors, _ = validator.validate_refactor_plan_text(raw_text)
    assert errors
    assert any(expected_fragment in err for err in errors)


def test_validate_refactor_plan_cli_normalizes_file_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI reads $FACTORY_RESPONSE_FILE, normalizes in-place, and returns exit code 0."""
    validator = _load_refactor_plan_validator()
    resp_file = tmp_path / "response.md"
    resp_file.write_text("Preamble\n\n" + VALID_READY_CONTRACT, encoding="utf-8")
    monkeypatch.setenv("FACTORY_RESPONSE_FILE", str(resp_file))

    rc = validator.main([])
    assert rc == 0
    written = resp_file.read_text(encoding="utf-8")
    assert written.startswith("# Refactor Contract\n")
    assert written.endswith("STATUS: READY\n")


def test_validate_refactor_plan_cli_missing_file_returns_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI returns exit code 2 when response file is missing or unset."""
    validator = _load_refactor_plan_validator()
    monkeypatch.delenv("FACTORY_RESPONSE_FILE", raising=False)
    assert validator.main([]) == 2
    assert validator.main([str(tmp_path / "missing.md")]) == 2


def test_refactor_review_report_is_compatible_with_validate_review() -> None:
    """A populated refactor-review-report.md passes the shared validate_review.py validator."""
    review_validator = _load_review_validator()
    sample_report = """\
### Risk Classification: LOW
- **Reason**: Internal seam extraction in `runner/prompts.py` with no public API change.

### Behavior Preservation & Seam Verification
| Dimension | Status | Evidence (`file:line` or test/check) |
| :--- | :--- | :--- |
| Behavior & Invariant Preservation | PASS | `just test-fast` (640 passed) |
| Compatibility & Re-export Policy | PASS | `src/acp_factory/runner/loop.py:12` |
| Seam Depth & Dependency Direction | PASS | `loop.py -> prompts.py` acyclic |
| Test Integrity & Seam Coverage | PASS | `tests/test_prompts.py` |
| Documentation Pointer Sync | PASS | `AGENTS.md` updated |

### Findings
- None.

STATUS: ACCEPTED
"""
    errors, normalized = review_validator.validate_review_text(sample_report)
    assert errors == []
    assert normalized.endswith("STATUS: ACCEPTED\n")
