"""Git worktree lifecycle management for isolated and issue-persistent acp-factory runs."""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path


class WorktreeError(RuntimeError):
    """Raised when git worktree creation or cleanup fails."""


_REPO_SLUG_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)


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


_GITDIR_RE = re.compile(r"^gitdir:\s*(.+?)\s*$")


def _main_root_from_gitdir(gitdir: Path) -> Path:
    """Return the main repository root that owns a linked-worktree `gitdir`."""
    parts = gitdir.parts
    if ".git" in parts:
        index = parts.index(".git")
        if index > 0:
            return Path(*parts[:index])
    return gitdir


def _fallback_main_repo_root(path: Path) -> Path:
    """Climb from `path` to the nearest `.git` entry and derive the main root."""
    for candidate in (path, *path.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return candidate
        if dot_git.is_file():
            try:
                match = _GITDIR_RE.match(dot_git.read_text(encoding="utf-8").strip())
            except OSError:
                continue
            if match is None:
                continue
            gitdir = Path(match.group(1)).expanduser()
            if not gitdir.is_absolute():
                gitdir = (candidate / gitdir).resolve()
            return _main_root_from_gitdir(gitdir)
    return path


def resolve_main_repo_root(path: Path) -> Path:
    """Return the canonical main repository root for `path`.

    Handles being invoked from a repository subdirectory, a linked worktree, or a
    worktree subdirectory. Prefers git's common directory; falls back to climbing
    the filesystem for `.git` files/directories, then to `path.resolve()` when no
    repository marker exists (e.g. mocked unit tests).
    """
    resolved = path.resolve()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=resolved,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return _fallback_main_repo_root(resolved)

    if proc.returncode == 0 and proc.stdout.strip():
        common_dir = Path(proc.stdout.strip())
        return common_dir.parent if common_dir.name == ".git" else common_dir
    return _fallback_main_repo_root(resolved)


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
    """Return the repository's default branch.

    Prefers `refs/remotes/origin/HEAD`; falls back to `git ls-remote --symref origin HEAD`
    (works for shallow/partial clones where the symbolic ref is absent).
    """
    rc, ref, _err = await _run_git(
        repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"
    )
    if rc == 0 and ref:
        return ref.split("/", 1)[1] if "/" in ref else ref

    rc2, out, err2 = await _run_git(repo, "ls-remote", "--symref", "origin", "HEAD")
    if rc2 != 0 or not out:
        raise WorktreeError(
            f"Cannot detect default branch in {repo}: {err2 or 'no origin/HEAD'}"
        )
    # `git ls-remote --symref origin HEAD` prints e.g.
    #   ref: refs/heads/main\tHEAD
    for line in out.splitlines():
        if line.startswith("ref: refs/heads/"):
            return line.split("refs/heads/", 1)[1].split("\t", 1)[0].strip()
    raise WorktreeError(f"Cannot parse default branch from ls-remote in {repo}: {out}")


def _expand_path(raw: str) -> Path:
    """Expand a user path (leading `~`) into an absolute `Path`."""
    return Path(raw).expanduser() if raw.startswith("~") else Path(raw)


def resolve_fleet_repo_path(
    repo_slug: str,
    *,
    repository_map_value: str,
) -> Path:
    """Resolve a fleet repository to its local checkout path (explicit path only)."""
    if not _REPO_SLUG_RE.match(repo_slug):
        raise WorktreeError(
            f"Invalid fleet repository slug '{repo_slug}'. Expected 'owner/repo'."
        )
    return _expand_path(repository_map_value).resolve()


async def ensure_fleet_repo_checkout(
    repo_slug: str,
    *,
    repository_map_value: str,
) -> Path:
    """Resolve a fleet target repository to a local checkout and ensure a fresh baseline.

    `repository_map_value` must be an explicit local checkout path (absolute or
    `~`-relative); on-demand cloning is intentionally not supported. After resolving,
    `git fetch --prune origin` guarantees new issue worktrees never branch from a stale
    local HEAD.
    """
    checkout = resolve_fleet_repo_path(
        repo_slug,
        repository_map_value=repository_map_value,
    )

    if not checkout.exists():
        raise WorktreeError(
            f"Fleet repository '{repo_slug}' maps to missing path '{checkout}'"
        )
    if not (checkout / ".git").exists():
        raise WorktreeError(f"'{checkout}' is not a git repository")

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
        self.invocation_root = repo_root.resolve()
        self.repo_root = resolve_main_repo_root(self.invocation_root)
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
        main_root = resolve_main_repo_root(repo_root)
        return main_root / ".worktrees" / f"issue-{issue_number}"

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
            # Invoked from inside the target issue's own worktree: reuse it in place.
            if self.invocation_root.is_relative_to(wt_path):
                self.worktree_path = wt_path
                return wt_path
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
        main_root = resolve_main_repo_root(repo_root)
        wt_path = cls.issue_worktree_path(main_root, issue_number)
        branch = cls.issue_branch_name(issue_number)
        existed = wt_path.exists()
        await _remove_worktree_and_branch(main_root, wt_path, branch)
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
