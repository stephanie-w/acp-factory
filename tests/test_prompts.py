"""Architecture tests for the extracted prompt-builder seam."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from acp_factory.runner import loop, prompts

_REEXPORTED_SYMBOLS = (
    "EMPTY_OUTPUT_FAILURE_REASON",
    "build_delta_review_brief",
    "build_initial_prompt",
    "build_retry_prompt",
    "build_spec_revision_brief",
    "check_turn_cap",
    "check_usage_cap",
    "has_existing_spec",
)


@pytest.mark.parametrize("name", _REEXPORTED_SYMBOLS)
def test_loop_reexports_prompt_symbols(name: str) -> None:
    assert getattr(loop, name) is getattr(prompts, name)


def test_format_response_excerpt_lives_in_prompts() -> None:
    assert callable(prompts._format_response_excerpt)


def test_prompts_does_not_import_loop() -> None:
    """The shared prompt module must not depend on the orchestration loop."""
    source = Path(prompts.__file__).read_text(encoding="utf-8")
    imported_modules = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(name.endswith("runner.loop") for name in imported_modules)
