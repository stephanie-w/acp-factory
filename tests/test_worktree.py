"""Unit tests for worktree root resolution and issue worktree lifecycle."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

from rich.console import Console

from acp_factory.runner import worktree as worktree_module
from acp_factory.runner.worktree import (
    WorktreeManager,
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
