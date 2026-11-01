"""Git worktree lifecycle management for isolated and issue-persistent acp-factory runs."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
import uuid
from pathlib import Path


class WorktreeError(RuntimeError):
    """Raised when git worktree creation or cleanup fails."""


async def _run_git(cwd: Path, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def get_head_sha(cwd: Path) -> str:
    """Return the current `HEAD` commit SHA in `cwd`, raising on failure."""
    rc, sha, err = await _run_git(cwd, "rev-parse", "HEAD")
    if rc != 0 or not sha:
        raise WorktreeError(f"git rev-parse HEAD failed in {cwd}: {err or 'no SHA'}")
    return sha


async def is_worktree_clean(cwd: Path) -> bool:
    """Return True when `git status --porcelain` is empty in `cwd`."""
    rc, out, _err = await _run_git(cwd, "status", "--porcelain")
    return rc == 0 and out == ""


class WorktreeManager:
    """Manages an isolated git worktree (temporary or issue-persistent)."""

    def __init__(
        self,
        repo_root: Path,
        *,
        enabled: bool = False,
        issue_number: int | None = None,
        keep_on_failure: bool = True,
        keep_on_success: bool = False,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.enabled = enabled or (issue_number is not None)
        self.issue_number = issue_number
        # Issue worktrees persist across build/review rounds until the `done` stage
        self.keep_on_failure = True if issue_number is not None else keep_on_failure
        self.keep_on_success = True if issue_number is not None else keep_on_success

        self.worktree_path: Path | None = None
        self.branch_name: str | None = None
        self.preserved_path: Path | None = None

    @staticmethod
    def issue_worktree_path(repo_root: Path, issue_number: int) -> Path:
        """Return the deterministic worktree path `.worktrees/issue-<id>` for a GitHub Issue."""
        return repo_root.resolve() / ".worktrees" / f"issue-{issue_number}"

    @staticmethod
    def issue_branch_name(issue_number: int) -> str:
        """Return the deterministic branch name `factory/issue-<id>` for a GitHub Issue."""
        return f"factory/issue-{issue_number}"

    async def create(self) -> Path:
        """Create or reuse a git worktree if enabled, or return repo_root."""
        if not self.enabled:
            return self.repo_root

        rc, top_level, err = await _run_git(
            self.repo_root, "rev-parse", "--show-toplevel"
        )
        if rc != 0 or not top_level:
            raise WorktreeError(
                f"Cannot create git worktree: '{self.repo_root}' is not a git repository ({err})."
            )

        if self.issue_number is not None:
            self.branch_name = self.issue_branch_name(self.issue_number)
            wt_path = self.issue_worktree_path(self.repo_root, self.issue_number)
            # Reuse existing worktree if already created in a prior build/review round
            if wt_path.exists() and (wt_path / ".git").exists():
                self.worktree_path = wt_path
                return wt_path

            wt_path.parent.mkdir(parents=True, exist_ok=True)
            # Check if branch already exists locally
            branch_rc, _, _ = await _run_git(
                self.repo_root, "rev-parse", "--verify", self.branch_name
            )
            if branch_rc == 0:
                rc, _out, err = await _run_git(
                    self.repo_root,
                    "worktree",
                    "add",
                    str(wt_path),
                    self.branch_name,
                )
            else:
                rc, _out, err = await _run_git(
                    self.repo_root,
                    "worktree",
                    "add",
                    "-b",
                    self.branch_name,
                    str(wt_path),
                    "HEAD",
                )
            if rc != 0:
                raise WorktreeError(
                    f"Failed to create issue worktree at {wt_path}: {err}"
                )
            self.worktree_path = wt_path
            return wt_path

        run_id = uuid.uuid4().hex[:8]
        self.branch_name = f"acp-factory/{run_id}"
        base_dir = Path(tempfile.gettempdir()) / "acp-factory-worktrees"
        base_dir.mkdir(parents=True, exist_ok=True)
        wt_path = base_dir / f"{self.repo_root.name}-{run_id}"

        rc, _out, err = await _run_git(
            self.repo_root,
            "worktree",
            "add",
            "-b",
            self.branch_name,
            str(wt_path),
            "HEAD",
        )
        if rc != 0:
            raise WorktreeError(f"Failed to create git worktree at {wt_path}: {err}")

        self.worktree_path = wt_path
        return wt_path

    async def cleanup(self, *, succeeded: bool) -> Path | None:
        """Remove or preserve the worktree based on run outcome and policy."""
        if not self.enabled or self.worktree_path is None:
            return None

        should_keep = self.keep_on_success if succeeded else self.keep_on_failure
        if should_keep:
            self.preserved_path = self.worktree_path
            return self.preserved_path

        wt_path = self.worktree_path
        branch = self.branch_name
        self.worktree_path = None

        await _remove_worktree_and_branch(self.repo_root, wt_path, branch)
        return None

    @classmethod
    async def remove_issue_worktree(cls, repo_root: Path, issue_number: int) -> bool:
        """Remove the persistent `.worktrees/issue-<id>` worktree and local branch during the `done` stage."""
        wt_path = cls.issue_worktree_path(repo_root, issue_number)
        branch = cls.issue_branch_name(issue_number)
        existed = wt_path.exists()
        await _remove_worktree_and_branch(repo_root.resolve(), wt_path, branch)
        return existed


async def _remove_worktree_and_branch(
    repo_root: Path, wt_path: Path, branch: str | None
) -> None:
    with contextlib.suppress(Exception):
        await _run_git(repo_root, "worktree", "remove", "--force", str(wt_path))
    if wt_path.exists():
        with contextlib.suppress(Exception):
            shutil.rmtree(wt_path, ignore_errors=True)
    if branch:
        with contextlib.suppress(Exception):
            await _run_git(repo_root, "branch", "-D", branch)
