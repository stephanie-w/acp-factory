"""Architecture tests for the extracted factory-run orchestrator seam."""

from __future__ import annotations

import ast
from pathlib import Path

from acp_factory.runner import FactoryRunOutcome, loop, orchestrator, run_factory


def test_package_reexports_orchestrator_symbols() -> None:
    assert run_factory is orchestrator.run_factory
    assert FactoryRunOutcome is orchestrator.FactoryRunOutcome


def test_loop_reexports_orchestrator_symbols() -> None:
    """`loop` keeps re-exporting the run seam for existing callers."""
    assert loop.run_factory is orchestrator.run_factory
    assert loop.FactoryRunOutcome is orchestrator.FactoryRunOutcome


def test_orchestrator_does_not_import_loop() -> None:
    source = Path(orchestrator.__file__).read_text(encoding="utf-8")
    imported_modules = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(name.endswith("runner.loop") for name in imported_modules)
