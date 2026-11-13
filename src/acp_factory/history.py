"""Run-history discovery over `.factory/runs/*/run.json` artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_SKIP_DIRS = {"latest", "dry-run"}


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

    records.sort(
        key=lambda r: str(r.get("timestamp") or r.get("run_id") or ""),
        reverse=True,
    )
    return records
