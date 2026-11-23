"""Run-history discovery over resolved and legacy `runs/*/run.json` artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import FactoryConfig, resolve_runs_dir
from .runner.worktree import (
    WorktreeError,
    resolve_fleet_repo_path,
    resolve_main_repo_root,
)

_SKIP_DIRS = {"latest", "dry-run"}


def _sort_records(records: list[dict[str, Any]]) -> None:
    """Sort records newest first by ISO timestamp, falling back to run id."""
    records.sort(
        key=lambda r: str(r.get("timestamp") or r.get("run_id") or ""),
        reverse=True,
    )


def _candidate_runs_dirs(workspace: Path, runs_dir: Path | None = None) -> list[Path]:
    """Return existing run directories to search, primary first.

    The resolved `runs_dir` wins, then the legacy `<workspace>/.factory/runs/`
    directory, then the main repository's legacy directory when `workspace` is a
    linked worktree. Duplicate paths are collapsed so a run is never listed twice.
    """
    ws = workspace.resolve()
    primary = (runs_dir or resolve_runs_dir(ws)).resolve()
    candidates = [primary, (ws / ".factory" / "runs").resolve()]
    main_root = resolve_main_repo_root(ws).resolve()
    if main_root != ws:
        candidates.append((main_root / ".factory" / "runs").resolve())

    seen: set[Path] = set()
    existing: list[Path] = []
    for candidate in candidates:
        if candidate in seen or not candidate.is_dir():
            continue
        seen.add(candidate)
        existing.append(candidate)
    return existing


def list_run_history(
    workspace: Path, *, runs_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Return run metadata records from resolved and legacy run directories.

    Only directories containing a `run.json` are considered; the `latest` symlink
    and `dry-run` staging directory are skipped. Records are sorted by their ISO
    `timestamp` (falling back to `run_id`) in descending order. When the same
    `run_id` exists in both the resolved and legacy directories, the resolved
    entry wins.
    """
    records: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()

    for candidate_dir in _candidate_runs_dirs(workspace, runs_dir):
        for run_json in sorted(candidate_dir.glob("*/run.json")):
            run_dir = run_json.parent
            if run_dir.name in _SKIP_DIRS:
                continue
            try:
                data = json.loads(run_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            run_id = str(data.get("run_id") or run_dir.name)
            if run_id in seen_run_ids:
                continue
            seen_run_ids.add(run_id)
            records.append(data)

    _sort_records(records)
    return records


def list_fleet_run_history(config: FactoryConfig) -> list[dict[str, Any]]:
    """Return merged run metadata records across every fleet checkout, newest first.

    Each record is attributed to its fleet map key when it does not already carry a
    `repo_slug`. Repositories with an invalid slug, missing checkout, absent run
    directories, or unreadable `run.json` files are skipped rather than aborting
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
        for record in list_run_history(
            checkout, runs_dir=config.resolve_runs_dir(checkout)
        ):
            if not record.get("repo_slug"):
                record["repo_slug"] = repo_slug
            records.append(record)

    _sort_records(records)
    return records
