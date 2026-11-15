"""Run-history discovery over `.factory/runs/*/run.json` artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import FactoryConfig
from .runner.worktree import WorktreeError, resolve_fleet_repo_path

_SKIP_DIRS = {"latest", "dry-run"}


def _sort_records(records: list[dict[str, Any]]) -> None:
    """Sort records newest first by ISO timestamp, falling back to run id."""
    records.sort(
        key=lambda r: str(r.get("timestamp") or r.get("run_id") or ""),
        reverse=True,
    )


def list_run_history(workspace: Path) -> list[dict[str, Any]]:
    """Return run metadata records under `<workspace>/.factory/runs/`, newest first.

    Only directories containing a `run.json` are considered; the `latest` symlink
    and `dry-run` staging directory are skipped. Records are sorted by their ISO
    `timestamp` (falling back to `run_id`) in descending order.
    """
    runs_dir = workspace / ".factory" / "runs"
    if not runs_dir.is_dir():
        return []

    records: list[dict[str, Any]] = []
    for run_json in sorted(runs_dir.glob("*/run.json")):
        run_dir = run_json.parent
        if run_dir.name in _SKIP_DIRS:
            continue
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            records.append(data)

    _sort_records(records)
    return records


def list_fleet_run_history(config: FactoryConfig) -> list[dict[str, Any]]:
    """Return merged run metadata records across every fleet checkout, newest first.

    Each record is attributed to its fleet map key when it does not already carry a
    `repo_slug`. Repositories with an invalid slug, missing checkout, absent
    `.factory/runs/`, or unreadable `run.json` files are skipped rather than aborting
    the aggregation.
    """
    if config.fleet is None:
        return []

    records: list[dict[str, Any]] = []
    for repo_slug, repo_value in config.fleet.repositories.items():
        try:
            checkout = resolve_fleet_repo_path(
                repo_slug,
                repository_map_value=repo_value,
            )
        except WorktreeError:
            continue
        for record in list_run_history(checkout):
            if not record.get("repo_slug"):
                record["repo_slug"] = repo_slug
            records.append(record)

    _sort_records(records)
    return records
