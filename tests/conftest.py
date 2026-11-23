"""Shared pytest fixtures.

The autouse fixture below isolates run-artifact persistence for the whole suite so
`run_factory`/`RunArtifactStore.persist` never write into the developer's real
`~/.local/share/acp-factory/runs/` when a test passes `tmp_path` as its workspace.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_run_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect run artifacts and user-level config discovery into the test temp tree.

    Clears ``FACTORY_RUNS_DIR`` (so a developer's ambient override cannot leak into
    the real data home) and pins both ``XDG_DATA_HOME`` and ``HOME`` under
    ``tmp_path``. ``HOME`` is a belt-and-suspenders fallback for the case where a
    test clears ``XDG_DATA_HOME``; it is intentionally *not* pre-created so that
    ``test_config``'s ``_isolate_runs_env`` helper can still ``mkdir`` it.
    """
    monkeypatch.delenv("FACTORY_RUNS_DIR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
    xdg_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return xdg_home
