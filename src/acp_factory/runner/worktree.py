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


DEFAULT_WORKSPACES_DIR = "~/.acp-factory/repos"


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


async def detect_default_branch(repo: Path) -> str:
    """Return the repository's default branch by following `refs/remotes/origin/HEAD`."""
    rc, ref, err = await _run_git(
        repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"
    )
    if rc != 0 or not ref:
        raise WorktreeError(
            f"Cannot detect default branch in {repo}: {err or 'no origin/HEAD'}"
        )
    # ref looks like `origin/main`; strip the `origin/` prefix.
    return ref.split("/", 1)[1] if "/" in ref else ref


def _expand_path(raw: str) -> Path:
    """Expand a user path (leading `~`) into an absolute `Path`."""
    return Path(raw).expanduser() if raw.startswith("~") else Path(raw)


async def ensure_fleet_repo_checkout(
    repo_slug: str,
    *,
    repository_map_value: str,
    workspaces_dir: str | None = None,
) -> Path:
    """Resolve a fleet target repository into a local checkout, cloning on demand.

    - An explicit local path (containing `/` or starting with `~`/`.`) is used as-is.
    - The string `"auto"` clones `repo_slug` into `<workspaces_dir>/<owner>__<repo>`.
    - After resolving/creating the checkout, `git fetch --prune origin` guarantees a
      fresh baseline so new issue worktrees never branch from a stale local HEAD.
    """
    if repository_map_value != "auto":
        checkout = _expand_path(repository_map_value)
        if not checkout.exists():
            raise WorktreeError(
                f"Fleet repository '{repo_slug}' maps to missing path '{checkout}'"
            )
        checkout = checkout.resolve()
    else:
        ws_dir = _expand_path(workspaces_dir or DEFAULT_WORKSPACES_DIR)
        checkout = (ws_dir / repo_slug.replace("/", "__")).resolve()

    if not (checkout / ".git").exists():
        if repository_map_value != "auto":
            raise WorktreeError(f"'{checkout}' is not a git repository")
        checkout.parent.mkdir(parents=True, exist_ok=True)
        rc, _out, err = await _run_git(
            checkout.parent,
            "clone",
            f"https://github.com/{repo_slug}.git",
            str(checkout.name),
        )
        if rc != 0:
            raise WorktreeError(f"Failed to clone {repo_slug}: {err}")

    rc, _out, err = await _run_git(checkout, "fetch", "--prune", "origin")
    if rc != 0:
        raise WorktreeError(f"git fetch --prune origin failed in {checkout}: {err}")
    return checkout


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

    async def create(self, *, base_ref: str | None = None) -> Path:
        """Create or reuse a git worktree if enabled, or return repo_root.

        `base_ref` overrides the ref to branch from (default `HEAD`). Fleet runs pass
        `origin/<default-branch>` so new issue branches start from a fresh baseline.
        """
        if not self.enabled:
            return self.repo_root

        rc, top_level, err = await _run_git(
            self.repo_root, "rev-parse", "--show-toplevel"
        )
        if rc != 0 or not top_level:
            raise WorktreeError(
                f"Cannot create git worktree: '{self.repo_root}' is not a git repository ({err})."
            )

        effective_base = base_ref or "HEAD"

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
                    effective_base,
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
            effective_base,
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
