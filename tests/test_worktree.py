"""Unit tests for worktree root resolution and issue worktree lifecycle."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from rich.console import Console

from acp_factory.runner import worktree as worktree_module
from acp_factory.runner.worktree import (
    GitSyncError,
    WorktreeManager,
    ensure_workspace_git_sync,
    resolve_main_repo_root,
)
from acp_factory.task import TaskContext


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "factory@example.com", cwd=path)
    _git("config", "user.name", "Factory Tests", cwd=path)
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    _git("add", "README.md", cwd=path)
    _git("commit", "-qm", "initial", cwd=path)
    return path.resolve()


def _add_issue_worktree(main_root: Path, issue_number: int) -> Path:
    wt_path = main_root / ".worktrees" / f"issue-{issue_number}"
    _git(
        "worktree",
        "add",
        "-q",
        "-b",
        f"factory/issue-{issue_number}",
        str(wt_path),
        cwd=main_root,
    )
    return wt_path


def test_resolve_main_repo_root(tmp_path: Path) -> None:
    main_root = _init_repo(tmp_path / "main")
    sub_dir = main_root / "src" / "pkg"
    sub_dir.mkdir(parents=True)
    wt_path = _add_issue_worktree(main_root, 5)
    wt_sub_dir = wt_path / "nested"
    wt_sub_dir.mkdir()

    assert resolve_main_repo_root(main_root) == main_root
    assert resolve_main_repo_root(sub_dir) == main_root
    assert resolve_main_repo_root(wt_path) == main_root
    assert resolve_main_repo_root(wt_sub_dir) == main_root


def test_resolve_main_repo_root_fallback_without_git(tmp_path: Path) -> None:
    main_root = tmp_path / "fake-main"
    (main_root / ".git").mkdir(parents=True)
    sub_dir = main_root / "nested"
    sub_dir.mkdir()
    assert resolve_main_repo_root(sub_dir) == main_root.resolve()

    linked_wt = tmp_path / "fake-wt"
    linked_wt.mkdir()
    gitdir = main_root / ".git" / "worktrees" / "issue-1"
    gitdir.mkdir(parents=True)
    (linked_wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert resolve_main_repo_root(linked_wt) == main_root.resolve()


def test_resolve_main_repo_root_outside_repo_returns_resolved_path(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert resolve_main_repo_root(plain) == plain.resolve()
    missing = tmp_path / "missing"
    assert resolve_main_repo_root(missing) == missing.resolve()


def test_resolve_main_repo_root_fast_path_skips_git(tmp_path: Path) -> None:
    main_root = _init_repo(tmp_path / "main")
    link = tmp_path / "link-main"
    link.symlink_to(main_root, target_is_directory=True)

    with patch(
        "acp_factory.runner.worktree.subprocess.run",
        side_effect=AssertionError("git must not run for a resolved main root"),
    ):
        assert resolve_main_repo_root(main_root) == main_root
        assert resolve_main_repo_root(link) == main_root


def test_resolve_main_repo_root_timeout(tmp_path: Path) -> None:
    main_root = _init_repo(tmp_path / "main")
    sub_dir = main_root / "src"
    sub_dir.mkdir()

    def fake_run(cmd: list[str], **kwargs: object) -> None:
        assert kwargs.get("timeout") == 10.0
        raise subprocess.TimeoutExpired(cmd="git", timeout=10.0)

    with patch("acp_factory.runner.worktree.subprocess.run", side_effect=fake_run):
        assert resolve_main_repo_root(sub_dir) == main_root


def test_issue_worktree_path_no_subprocess(tmp_path: Path) -> None:
    main_root = _init_repo(tmp_path / "main")

    with patch(
        "acp_factory.runner.worktree.subprocess.run",
        side_effect=AssertionError("git must not run for an already-resolved root"),
    ):
        assert (
            WorktreeManager.issue_worktree_path(main_root, 7)
            == main_root / ".worktrees" / "issue-7"
        )


async def test_create_reuses_active_worktree_symlink(tmp_path: Path) -> None:
    main_root = _init_repo(tmp_path / "real-main")
    wt_path = _add_issue_worktree(main_root, 5)
    link = tmp_path / "link-main"
    link.symlink_to(main_root, target_is_directory=True)
    symlinked_common = link / ".git"

    def fake_run(
        cmd: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["git", "rev-parse", "--path-format=absolute"]:
            return subprocess.CompletedProcess(cmd, 0, f"{symlinked_common}\n", "")
        raise AssertionError(f"unexpected git invocation: {cmd}")

    real_run_git = worktree_module._run_git
    calls: list[tuple[str, ...]] = []

    async def spy_run_git(cwd: Path, *args: str):
        calls.append(args)
        return await real_run_git(cwd, *args)

    with (
        patch("acp_factory.runner.worktree.subprocess.run", side_effect=fake_run),
        patch("acp_factory.runner.worktree._run_git", new=spy_run_git),
    ):
        manager = WorktreeManager(
            link / ".worktrees" / "issue-5", enabled=True, issue_number=5
        )
        result = await manager.create()

    assert result == wt_path
    assert manager.worktree_path == wt_path
    assert not any(args[:2] == ("worktree", "add") for args in calls)


def test_issue_worktree_path_anchoring(tmp_path: Path) -> None:
    main_root = _init_repo(tmp_path / "main")
    sub_dir = main_root / "src"
    sub_dir.mkdir()
    wt_path = _add_issue_worktree(main_root, 5)

    assert WorktreeManager.issue_worktree_path(wt_path, 5) == wt_path
    assert (
        WorktreeManager.issue_worktree_path(sub_dir, 7)
        == main_root / ".worktrees" / "issue-7"
    )


async def test_create_reuses_active_worktree(tmp_path: Path) -> None:
    main_root = _init_repo(tmp_path / "main")
    wt_path = _add_issue_worktree(main_root, 5)
    real_run_git = worktree_module._run_git
    calls: list[tuple[str, ...]] = []

    async def spy_run_git(cwd: Path, *args: str):
        calls.append(args)
        return await real_run_git(cwd, *args)

    manager = WorktreeManager(wt_path, enabled=True, issue_number=5)

    with patch("acp_factory.runner.worktree._run_git", new=spy_run_git):
        result = await manager.create()

    assert result == wt_path
    assert manager.worktree_path == wt_path
    assert manager.branch_name == "factory/issue-5"
    assert not any(args[:2] == ("worktree", "add") for args in calls)
    assert not (wt_path / ".worktrees").exists()


async def test_create_from_different_worktree_anchors_to_main(tmp_path: Path) -> None:
    main_root = _init_repo(tmp_path / "main")
    wt_a = _add_issue_worktree(main_root, 5)

    manager = WorktreeManager(wt_a, enabled=True, issue_number=6)
    result = await manager.create()

    expected = main_root / ".worktrees" / "issue-6"
    assert result == expected
    assert result.exists()
    assert (result / ".git").is_file()
    assert not (wt_a / ".worktrees").exists()
    assert manager.branch_name == "factory/issue-6"


async def test_worktree_git_identity_isolation(tmp_path: Path) -> None:
    main_root = _init_repo(tmp_path / "main")

    wt_path = await WorktreeManager(main_root, enabled=True, issue_number=5).create()

    assert _git("config", "user.name", cwd=wt_path).stdout.strip() == "acp-factory[bot]"
    assert (
        _git("config", "user.email", cwd=wt_path).stdout.strip()
        == "acp-factory[bot]@users.noreply.github.com"
    )
    # The parent repository must keep the developer's interactive identity untouched.
    assert _git("config", "user.name", cwd=main_root).stdout.strip() == "Factory Tests"
    assert (
        _git("config", "user.email", cwd=main_root).stdout.strip()
        == "factory@example.com"
    )

    (wt_path / "bot.txt").write_text("bot\n", encoding="utf-8")
    _git("add", "bot.txt", cwd=wt_path)
    _git("commit", "-qm", "feat: bot commit", cwd=wt_path)
    assert (
        _git("log", "-1", "--pretty=%an <%ae>", cwd=wt_path).stdout.strip()
        == "acp-factory[bot] <acp-factory[bot]@users.noreply.github.com>"
    )

    # Reusing an existing worktree re-applies the bot identity and stays isolated.
    reused = await WorktreeManager(main_root, enabled=True, issue_number=5).create()
    assert reused == wt_path
    assert _git("config", "user.name", cwd=reused).stdout.strip() == "acp-factory[bot]"
    assert _git("config", "user.name", cwd=main_root).stdout.strip() == "Factory Tests"


async def test_remove_issue_worktree_from_subdirectory(tmp_path: Path) -> None:
    main_root = _init_repo(tmp_path / "main")
    wt_path = _add_issue_worktree(main_root, 5)
    sub_dir = main_root / "src"
    sub_dir.mkdir()

    removed = await WorktreeManager.remove_issue_worktree(sub_dir, 5)

    assert removed is True
    assert not wt_path.exists()
    branches = _git("branch", "--list", "factory/issue-5", cwd=main_root)
    assert branches.stdout.strip() == ""


def test_status_worktree_detection_from_subdirectory(tmp_path: Path) -> None:
    from acp_factory import cli

    main_root = _init_repo(tmp_path / "main")
    wt_path = _add_issue_worktree(main_root, 5)
    sub_dir = main_root / "src"
    sub_dir.mkdir()
    task = TaskContext(issue_number=5, title="Issue 5", labels=["factory:build"])

    buffer = io.StringIO()
    with (
        patch(
            "acp_factory.cli.list_all_factory_issues",
            new=AsyncMock(return_value=[task]),
        ),
        patch(
            "acp_factory.cli.console",
            new=Console(file=buffer, width=200),
        ),
    ):
        cli.status_cmd(workspace=sub_dir, fleet=False)

    assert str(wt_path.relative_to(main_root)) in buffer.getvalue()


def _setup_remote_and_local(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "-q", cwd=bare)

    local = tmp_path / "local"
    _git("clone", "-q", str(bare), str(local), cwd=tmp_path)
    _git("config", "user.email", "factory@example.com", cwd=local)
    _git("config", "user.name", "Factory Tests", cwd=local)
    (local / "README.md").write_text("initial\n", encoding="utf-8")
    _git("add", "README.md", cwd=local)
    _git("commit", "-qm", "initial", cwd=local)
    _git("push", "-q", "-u", "origin", "HEAD", cwd=local)
    return bare, local


def _push_remote_commit(
    bare: Path, tmp_path: Path, filename: str, content: str, message: str
) -> None:
    other = tmp_path / "other"
    _git("clone", "-q", str(bare), str(other), cwd=tmp_path)
    _git("config", "user.email", "other@example.com", cwd=other)
    _git("config", "user.name", "Other Dev", cwd=other)
    (other / filename).write_text(content, encoding="utf-8")
    _git("add", filename, cwd=other)
    _git("commit", "-qm", message, cwd=other)
    _git("push", "-q", "origin", "HEAD", cwd=other)


@pytest.mark.parametrize(
    ("mode", "offline"),
    [
        ("non_git", True),
        ("non_git", False),
        ("no_remote", False),
        ("up_to_date", False),
    ],
    ids=["offline", "not_git_repo", "no_remote", "up_to_date"],
)
async def test_ensure_workspace_git_sync_noop_cases(
    tmp_path: Path, mode: str, offline: bool
) -> None:
    if mode == "non_git":
        target = tmp_path / "non_git"
        target.mkdir()
    elif mode == "no_remote":
        target = _init_repo(tmp_path / "local_no_remote")
    else:
        _, target = _setup_remote_and_local(tmp_path)

    assert await ensure_workspace_git_sync(target, offline=offline) == 0


async def test_ensure_workspace_git_sync_auto_fast_forwards(tmp_path: Path) -> None:
    bare, local = _setup_remote_and_local(tmp_path)
    _push_remote_commit(
        bare,
        tmp_path,
        "feature.txt",
        "upstream feature\n",
        "feat: new upstream feature",
    )

    synced = await ensure_workspace_git_sync(local)
    assert synced == 1
    assert (local / "feature.txt").read_text(encoding="utf-8") == "upstream feature\n"


async def test_ensure_workspace_git_sync_dirty_aborts(tmp_path: Path) -> None:
    bare, local = _setup_remote_and_local(tmp_path)
    _push_remote_commit(
        bare,
        tmp_path,
        "feature.txt",
        "upstream feature\n",
        "feat: new upstream feature",
    )

    (local / "README.md").write_text("dirty uncommitted change\n", encoding="utf-8")
    with pytest.raises(GitSyncError, match="uncommitted changes"):
        await ensure_workspace_git_sync(local)


async def test_ensure_workspace_git_sync_diverged_aborts(tmp_path: Path) -> None:
    bare, local = _setup_remote_and_local(tmp_path)
    _push_remote_commit(bare, tmp_path, "remote.txt", "remote\n", "remote commit")

    (local / "local.txt").write_text("local\n", encoding="utf-8")
    _git("add", "local.txt", cwd=local)
    _git("commit", "-qm", "local commit", cwd=local)

    with pytest.raises(GitSyncError, match="diverged"):
        await ensure_workspace_git_sync(local)
