"""Persist and re-read the artifacts a factory stage run leaves behind."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..acp.events import UsageTotals
from ..config import FactoryConfig
from ..verdict import _clean_markdown_stage_body, _find_terminal_status_or_verdict
from .checks import CheckResult

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-") or "check"


def _build_review_report(response_text: str) -> str:
    """Return a standalone review report that retains the terminal verdict/status token."""
    cleaned = _clean_markdown_stage_body(response_text) or response_text.strip()
    terminal_match, _ = _find_terminal_status_or_verdict(response_text)
    if terminal_match is not None:
        terminal_line = response_text[
            terminal_match.start() : terminal_match.end()
        ].strip()
        if terminal_line and terminal_line not in cleaned:
            cleaned = f"{cleaned}\n\n{terminal_line}".strip()
    return cleaned


_IMPLEMENTATION_SUMMARY_RE = re.compile(r"(?m)^#{1,4}\s+Implementation\s+Summary\b")


def _extract_implementation_notes(response_text: str) -> str | None:
    """Return the standalone `### Implementation Summary` section, or None if absent."""
    match = _IMPLEMENTATION_SUMMARY_RE.search(response_text)
    if match is None:
        return None

    notes = response_text[match.start() :]
    terminal_match, _ = _find_terminal_status_or_verdict(notes)
    if terminal_match is not None:
        notes = notes[: terminal_match.start()]

    notes = notes.strip()
    return notes or None


def _update_latest_run_symlink(runs_dir: Path, run_id: str) -> None:
    """Point `runs_dir/latest` at the newest run directory."""
    latest_run = runs_dir / "latest"

    try:
        if latest_run.is_symlink() or latest_run.exists():
            latest_run.unlink()
        latest_run.symlink_to(run_id, target_is_directory=True)
    except OSError as exc:
        logger.debug("Could not update latest run symlink: %s", exc)


def _artifact_pointer_filenames(
    kind: str,
    *,
    issue_number: int | None,
    branch: str | None,
) -> list[str]:
    """Return the scoped `latest-*` pointer names for an issue and/or branch.

    Issue builds also update their branch pointer so a later standalone review of
    the same branch sees the notes, while disjoint branches stay isolated.
    """
    filenames: list[str] = []
    if issue_number is not None:
        filenames.append(f"latest-{kind}-issue-{issue_number}.md")
    if branch and branch.strip():
        filenames.append(f"latest-{kind}-branch-{_slugify(branch)}.md")
    if not filenames:
        filenames.append(f"latest-{kind}.md")
    return filenames


def _update_latest_artifact_pointer(
    runs_dir: Path,
    run_id: str,
    *,
    filename: str,
    relative_target: Path,
    text: str,
) -> None:
    """Point a stage-specific `latest-*` artifact at the newest run.

    Symlinks are preferred; when the platform forbids them, the text is copied to
    the artifact so callers always have a discoverable file.
    """
    latest_artifact = runs_dir / filename

    try:
        if latest_artifact.is_symlink() or latest_artifact.exists():
            latest_artifact.unlink()
        latest_artifact.symlink_to(Path(run_id) / relative_target)
    except OSError as exc:
        logger.debug("Could not symlink %s, writing a copy instead: %s", filename, exc)
        try:
            latest_artifact.write_text(text, encoding="utf-8")
        except OSError as write_exc:
            logger.warning("Could not write %s pointer: %s", filename, write_exc)


def _read_run_metadata(run_dir: Path) -> dict[str, Any] | None:
    """Return the parsed `run.json` metadata for `run_dir`, if any."""
    run_json = run_dir / "run.json"
    try:
        data = json.loads(run_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _build_notes_metadata_matches(
    data: dict[str, Any],
    *,
    branch: str | None,
    head_sha: str | None,
) -> bool:
    """Return True when run metadata belongs to the reviewed build branch/HEAD."""
    if data.get("stage") != "build":
        return False
    recorded_branch = data.get("branch")
    if not recorded_branch or recorded_branch != branch:
        return False
    recorded_sha = data.get("head_sha")
    # A missing SHA on either side means the build predates SHA recording; the
    # branch match is the strongest evidence available in that case.
    return not (head_sha and recorded_sha and recorded_sha != head_sha)


def _scan_matching_build_notes(
    runs_dir: Path,
    *,
    branch: str | None,
    head_sha: str | None,
) -> str | None:
    """Return notes from the newest build run matching `branch` and `head_sha`."""
    if not branch:
        return None

    matches: list[tuple[str, Path]] = []
    for run_json in runs_dir.glob("*/run.json"):
        run_dir = run_json.parent
        if run_dir.name in {"latest", "dry-run"}:
            continue
        meta = _read_run_metadata(run_dir)
        if meta is None or not _build_notes_metadata_matches(
            meta, branch=branch, head_sha=head_sha
        ):
            continue
        matches.append((str(meta.get("timestamp") or run_dir.name), run_dir))

    for _timestamp, run_dir in sorted(matches, reverse=True):
        candidate = run_dir / "implementation-notes.md"
        try:
            notes = candidate.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.debug("Could not read prior build notes at %s: %s", candidate, exc)
            continue
        if notes:
            return notes
    return None


class RunArtifactStore:
    """Persist and re-read the artifacts of factory stage runs.

    The store binds one workspace and its resolved runs directory so every
    artifact written and read for a run stays in the same bucket.
    """

    def __init__(self, workspace: Path, config: FactoryConfig) -> None:
        self._config = config
        self._runs_dir = config.resolve_runs_dir(workspace)

    def persist(
        self,
        run_id: str,
        *,
        succeeded: bool,
        attempts_used: int,
        total_duration: float,
        setup_logs: list[str],
        turn_transcripts: list[str],
        check_history: list[list[CheckResult]],
        failure_reason: str | None,
        usage: UsageTotals | None = None,
        turn_summaries: list[dict[str, Any]] | None = None,
        turn_condensed: list[dict[str, Any]] | None = None,
        last_response_text: str = "",
        issue_number: int | None = None,
        issue_title: str | None = None,
        repo_slug: str | None = None,
        branch: str | None = None,
        base_ref: str | None = None,
        session_id: str | None = None,
        session_title: str | None = None,
        head_sha: str | None = None,
    ) -> Path:
        """Write structured run logs into `<runs_dir>/<run-id>/` and return that dir."""
        runs_dir = self._runs_dir
        config = self._config
        usage = usage or UsageTotals()
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        if setup_logs:
            (run_dir / "setup.log").write_text(
                "\n\n".join(setup_logs), encoding="utf-8"
            )

        max_turns = max(
            len(turn_transcripts),
            len(turn_summaries or []),
            len(turn_condensed or []),
        )
        for idx in range(1, max_turns + 1):
            attempt_dir = run_dir / f"attempt-{idx}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            if idx - 1 < len(turn_transcripts):
                (attempt_dir / "transcript.md").write_text(
                    turn_transcripts[idx - 1], encoding="utf-8"
                )
            if turn_summaries and idx - 1 < len(turn_summaries):
                (attempt_dir / "transcript.json").write_text(
                    json.dumps(turn_summaries[idx - 1], indent=2), encoding="utf-8"
                )
            if turn_condensed and idx - 1 < len(turn_condensed):
                (attempt_dir / "transcript.summary.json").write_text(
                    json.dumps(turn_condensed[idx - 1], indent=2), encoding="utf-8"
                )

        for idx, attempt_checks in enumerate(check_history, start=1):
            attempt_dir = run_dir / f"attempt-{idx}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            for chk_idx, chk_res in enumerate(attempt_checks, start=1):
                slug = _slugify(chk_res.name)
                log_file = attempt_dir / f"check-{chk_idx}-{slug}.log"
                log_file.write_text(chk_res.raw_output, encoding="utf-8")

        run_meta = {
            "run_id": run_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "stage": config.active_stage,
            "agent": config.agent.name,
            "model": config.agent.model,
            "mode": config.agent.mode,
            "thought_level": config.agent.thought_level,
            "read_only": config.agent.read_only,
            "persona": config.agent.persona,
            "skills": config.agent.skills,
            "issue_number": issue_number,
            "issue_title": issue_title,
            "repo_slug": repo_slug,
            "branch": branch,
            "base_ref": base_ref,
            "session_id": session_id,
            "session_title": session_title,
            "head_sha": head_sha,
            "succeeded": succeeded,
            "attempts_used": attempts_used,
            "max_attempts": config.retry.max_attempts,
            "duration_seconds": round(total_duration, 3),
            "failure_reason": failure_reason,
            "budget": {
                "max_tokens_per_run": config.budget.max_tokens_per_run,
                "max_cost_usd": config.budget.max_cost_usd,
                "max_turns": config.budget.max_turns,
            },
            "usage": {
                "total_tokens": usage.tokens,
                "total_cost_usd": round(usage.cost_usd, 6),
                "total_turns": usage.turns,
                "telemetry_available": bool(usage.tokens > 0 or usage.cost_usd > 0),
            },
        }
        (run_dir / "run.json").write_text(
            json.dumps(run_meta, indent=2), encoding="utf-8"
        )

        has_transcripts = bool(turn_transcripts or turn_summaries or turn_condensed)
        if not config.dry_run and has_transcripts:
            _update_latest_run_symlink(runs_dir, run_id)

        if config.active_stage == "review" and last_response_text.strip():
            report_text = _build_review_report(last_response_text)
            if report_text:
                (run_dir / "report.md").write_text(report_text, encoding="utf-8")
                for filename in _artifact_pointer_filenames(
                    "report", issue_number=issue_number, branch=branch
                ):
                    _update_latest_artifact_pointer(
                        runs_dir,
                        run_id,
                        filename=filename,
                        relative_target=Path("report.md"),
                        text=report_text,
                    )

        if config.active_stage == "build":
            notes_text = _extract_implementation_notes(last_response_text)
            if notes_text:
                (run_dir / "implementation-notes.md").write_text(
                    notes_text, encoding="utf-8"
                )
                for filename in _artifact_pointer_filenames(
                    "build-notes", issue_number=issue_number, branch=branch
                ):
                    _update_latest_artifact_pointer(
                        runs_dir,
                        run_id,
                        filename=filename,
                        relative_target=Path("implementation-notes.md"),
                        text=notes_text,
                    )

        return run_dir

    def prior_build_notes(
        self,
        *,
        issue_number: int | None = None,
        branch: str | None = None,
        head_sha: str | None = None,
        require_match: bool = False,
    ) -> str | None:
        """Return scoped build notes from the runs directory, or None when none apply.

        Never falls back to repo-wide `<workspace>/.factory/` pointers: issue and
        branch scope are required so stale notes cannot leak into unrelated reviews.
        With ``require_match`` the backing run must belong to ``branch`` (and share
        ``head_sha`` when both sides record one), otherwise no notes are returned.
        """
        runs_dir = self._runs_dir
        candidates: list[Path] = []
        if issue_number is not None:
            candidates.append(runs_dir / f"latest-build-notes-issue-{issue_number}.md")
        if branch and branch.strip():
            candidates.append(
                runs_dir / f"latest-build-notes-branch-{_slugify(branch)}.md"
            )
        if not candidates:
            candidates.append(runs_dir / "latest-build-notes.md")

        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                notes = candidate.read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.debug(
                    "Could not read prior build notes at %s: %s", candidate, exc
                )
                continue
            if not notes:
                continue

            target = candidate.resolve() if candidate.is_symlink() else candidate
            meta = _read_run_metadata(target.parent)
            if meta is None:
                # A plain-copy pointer has no sibling run.json; only a copy is safe
                # to trust when no branch/HEAD verification was requested.
                if require_match:
                    continue
            else:
                if issue_number is not None and meta.get("issue_number") not in (
                    None,
                    issue_number,
                ):
                    continue
                if require_match and not _build_notes_metadata_matches(
                    meta, branch=branch, head_sha=head_sha
                ):
                    continue
            return notes

        if require_match:
            return _scan_matching_build_notes(
                runs_dir, branch=branch, head_sha=head_sha
            )
        return None
