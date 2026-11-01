"""GitHub CLI (`gh`) integration for fetching issues, updating specs in-place, and transitioning labels."""

from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .task import (
    ALL_FACTORY_STAGE_LABELS,
    StageVerdict,
    TaskContext,
    clean_spec_block,
    render_issue_body,
)


@dataclass
class DryRunPlan:
    """Saved local files and copy-pasteable `gh`/`git` commands for `--dry-run` mode."""

    issue_number: int
    stage: str
    target_label: str
    updated_factory_block: str
    issue_body_file: Path | None = None
    pr_body_file: Path | None = None
    commands: list[str] = field(default_factory=list)


class GitHubError(RuntimeError):
    """Raised when a `gh` CLI command fails."""


async def run_gh(
    args: list[str],
    cwd: Path,
    *,
    input_text: str | None = None,
) -> str:
    """Execute a `gh` CLI command asynchronously and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE if input_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdin_bytes = input_text.encode("utf-8") if input_text is not None else None
    stdout_bytes, stderr_bytes = await proc.communicate(input=stdin_bytes)
    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        raise GitHubError(
            f"gh {' '.join(args)} failed (exit {proc.returncode}): {stderr or stdout}"
        )
    return stdout


async def fetch_issue(issue_number: int, cwd: Path) -> TaskContext:
    """Fetch a single GitHub Issue and parse its body, factory block, labels, and human comments."""
    out = await run_gh(
        [
            "issue",
            "view",
            str(issue_number),
            "--json",
            "number,title,state,body,labels,comments,url",
        ],
        cwd=cwd,
    )
    data = json.loads(out)
    return TaskContext.from_gh_json(data)


async def list_actionable_issues(
    cwd: Path,
    *,
    stage_label: str | None = None,
    limit: int = 50,
) -> list[TaskContext]:
    """List GitHub Issues that have an actionable `factory:*` stage (including closed issues with leftover worktrees)."""
    cmd = [
        "issue",
        "list",
        "--state",
        "open",
        "--limit",
        str(limit),
        "--json",
        "number,title,state,body,labels,comments,url",
    ]
    if stage_label:
        cmd.extend(["--label", stage_label])

    out = await run_gh(cmd, cwd=cwd)
    items = json.loads(out) if out else []
    tasks: list[TaskContext] = []
    seen_numbers: set[int] = set()
    for item in items:
        task = TaskContext.from_gh_json(item)
        if stage_label or task.current_stage is not None:
            tasks.append(task)
            seen_numbers.add(task.issue_number)

    # Also check local .worktrees/issue-<id> directories in case their issue was closed by a PR merge
    worktrees_root = cwd / ".worktrees"
    if not stage_label and worktrees_root.is_dir():
        for entry in sorted(worktrees_root.iterdir()):
            if entry.is_dir() and entry.name.startswith("issue-"):
                num_str = entry.name.removeprefix("issue-")
                if num_str.isdigit():
                    issue_num = int(num_str)
                    if issue_num not in seen_numbers:
                        try:
                            wt_task = await fetch_issue(issue_num, cwd=cwd)
                        except GitHubError:
                            continue
                        if wt_task.current_stage == "done":
                            tasks.append(wt_task)
                            seen_numbers.add(issue_num)

    # Process oldest issue numbers first
    tasks.sort(key=lambda t: t.issue_number)
    return tasks


async def list_all_factory_issues(cwd: Path, *, limit: int = 50) -> list[TaskContext]:
    """Return open GitHub Issues carrying any `factory:*` label plus any closed issues that still have a local worktree."""
    out = await run_gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,state,body,labels,comments,url",
        ],
        cwd=cwd,
    )
    items = json.loads(out) if out else []
    tasks: list[TaskContext] = []
    seen_numbers: set[int] = set()
    for item in items:
        task = TaskContext.from_gh_json(item)
        if any(lbl.startswith("factory:") for lbl in task.labels):
            tasks.append(task)
            seen_numbers.add(task.issue_number)

    worktrees_root = cwd / ".worktrees"
    if worktrees_root.is_dir():
        for entry in sorted(worktrees_root.iterdir()):
            if entry.is_dir() and entry.name.startswith("issue-"):
                num_str = entry.name.removeprefix("issue-")
                if num_str.isdigit():
                    issue_num = int(num_str)
                    if issue_num not in seen_numbers:
                        try:
                            wt_task = await fetch_issue(issue_num, cwd=cwd)
                        except GitHubError:
                            continue
                        tasks.append(wt_task)
                        seen_numbers.add(issue_num)

    tasks.sort(key=lambda t: t.issue_number)
    return tasks


async def update_issue_factory_block(
    task: TaskContext,
    new_factory_block: str,
    cwd: Path,
) -> str:
    """Update the `<!-- factory:start --> ... <!-- factory:end -->` block in-place on the GitHub Issue.

    Never posts comments on the issue thread; updates the Issue Body via stdin (`--body-file -`).
    """
    full_body = render_issue_body(task.original_body, new_factory_block)
    await run_gh(
        ["issue", "edit", str(task.issue_number), "--body-file", "-"],
        cwd=cwd,
        input_text=full_body,
    )
    task.factory_block = new_factory_block.strip()
    return full_body


LABEL_COLOR_MAP: dict[str, str] = {
    "factory:triage": "D4C5F9",
    "factory:spec": "C2E0C6",
    "factory:spec-ready": "FEF2C0",
    "factory:build": "1D76DB",
    "factory:review": "5319E7",
    "factory:pr-ready": "0E8A16",
    "factory:done": "BFDADC",
    "factory:completed": "0E8A16",
    "factory:needs-human": "D93F0B",
}


def _extract_repo_slug(url: str) -> str | None:
    """Extract `owner/repo` from a GitHub issue URL (`https://github.com/owner/repo/issues/1`)."""
    if "github.com/" not in url:
        return None
    tail = url.split("github.com/", 1)[1].strip("/")
    parts = tail.split("/")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return None


async def ensure_label_exists(label: str, cwd: Path) -> None:
    """Ensure a `factory:*` label exists in the GitHub repository."""
    color = LABEL_COLOR_MAP.get(label, "EDEDED")
    await run_gh(
        ["label", "create", label, "--color", color, "--force"],
        cwd=cwd,
    )


async def transition_issue_label(
    task: TaskContext,
    target_label: str,
    cwd: Path,
) -> list[str]:
    """Deterministically replace existing `factory:*` stage labels on the issue with `target_label`."""
    await ensure_label_exists(target_label, cwd=cwd)

    labels_to_remove = [
        lbl
        for lbl in task.labels
        if lbl in ALL_FACTORY_STAGE_LABELS and lbl != target_label
    ]
    if labels_to_remove:
        rm_cmd = ["issue", "edit", str(task.issue_number)]
        for rem in labels_to_remove:
            rm_cmd.extend(["--remove-label", rem])
        await run_gh(rm_cmd, cwd=cwd)

    await run_gh(
        ["issue", "edit", str(task.issue_number), "--add-label", target_label],
        cwd=cwd,
    )

    updated_labels = [lbl for lbl in task.labels if lbl not in labels_to_remove]
    if target_label not in updated_labels:
        updated_labels.append(target_label)
    task.labels = updated_labels
    return updated_labels


async def push_and_create_or_update_pr(
    task: TaskContext,
    *,
    pr_body: str,
    cwd: Path,
) -> str:
    """Push `task.branch` to origin and open or update a GitHub Pull Request (without ever merging)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "push",
        "-u",
        "origin",
        task.branch,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout_b, stderr_b = await proc.communicate()
    if proc.returncode != 0:
        err = stderr_b.decode("utf-8", errors="replace").strip()
        raise GitHubError(f"git push origin {task.branch} failed: {err}")

    pr_title = f"{task.title} (#{task.issue_number})"
    full_pr_body = f"Closes #{task.issue_number}\n\n{pr_body.strip()}\n"

    existing_json = await run_gh(
        [
            "pr",
            "list",
            "--head",
            task.branch,
            "--state",
            "open",
            "--json",
            "number,url",
        ],
        cwd=cwd,
    )
    existing = json.loads(existing_json) if existing_json else []
    if existing:
        pr_num = str(existing[0]["number"])
        pr_url = str(existing[0]["url"])
        await run_gh(
            ["pr", "edit", pr_num, "--title", pr_title, "--body-file", "-"],
            cwd=cwd,
            input_text=full_pr_body,
        )
        task.pr_url = pr_url
        return pr_url

    pr_url = await run_gh(
        [
            "pr",
            "create",
            "--head",
            task.branch,
            "--title",
            pr_title,
            "--body-file",
            "-",
        ],
        cwd=cwd,
        input_text=full_pr_body,
    )
    task.pr_url = pr_url
    return pr_url


async def push_and_create_or_update_standalone_pr(
    branch: str,
    *,
    base: str = "main",
    pr_title: str,
    pr_body: str,
    cwd: Path,
) -> str:
    """Push `branch` to origin and open or update a standalone GitHub Pull Request (no issue required)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "push",
        "-u",
        "origin",
        branch,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout_b, stderr_b = await proc.communicate()
    if proc.returncode != 0:
        err = stderr_b.decode("utf-8", errors="replace").strip()
        raise GitHubError(f"git push origin {branch} failed: {err}")

    clean_body = f"{pr_body.strip()}\n"
    existing_json = await run_gh(
        [
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,url",
        ],
        cwd=cwd,
    )
    existing = json.loads(existing_json) if existing_json else []
    if existing:
        pr_num = str(existing[0]["number"])
        pr_url = str(existing[0]["url"])
        await run_gh(
            ["pr", "edit", pr_num, "--title", pr_title, "--body-file", "-"],
            cwd=cwd,
            input_text=clean_body,
        )
        return pr_url

    return await run_gh(
        [
            "pr",
            "create",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            pr_title,
            "--body-file",
            "-",
        ],
        cwd=cwd,
        input_text=clean_body,
    )


def build_dry_run_standalone_pr_plan(
    branch: str,
    *,
    base: str = "main",
    pr_title: str,
    pr_body: str,
    verdict: StageVerdict,
    workspace: Path,
    run_dir: Path | None = None,
    create_pr: bool = False,
) -> DryRunPlan:
    """Save standalone review/PR body locally and build manual `git`/`gh` commands for `--dry-run`."""
    out_dir = run_dir or (workspace / ".factory" / "runs" / "dry-run")
    out_dir.mkdir(parents=True, exist_ok=True)

    cwd = Path.cwd().resolve()
    ws_resolved = workspace.resolve()
    outside_workspace = cwd != ws_resolved

    safe_branch = branch.replace("/", "-")
    pr_body_file = out_dir / f"pr-{safe_branch}-body.md"
    pr_body_file.write_text(f"{pr_body.strip()}\n", encoding="utf-8")

    commands: list[str] = []
    if create_pr and verdict.publish_pr:
        if outside_workspace:
            commands.append(f"cd {shlex.quote(str(ws_resolved))}")
        rel_pr_file = _rel_or_abs(pr_body_file, ws_resolved)
        commands.append(f"git push -u origin {shlex.quote(branch)}")
        commands.append(
            f"gh pr create --base {shlex.quote(base)} --head {shlex.quote(branch)} "
            f"--title {shlex.quote(pr_title)} "
            f"--body-file {shlex.quote(str(rel_pr_file))}"
        )
    else:
        rel_pr_file = _rel_or_abs(pr_body_file, cwd)
        commands.append(f"# Review verdict: {verdict.status} (saved to {rel_pr_file})")

    return DryRunPlan(
        issue_number=0,
        stage="review",
        target_label=verdict.target_label,
        updated_factory_block=verdict.updated_factory_block,
        pr_body_file=pr_body_file,
        commands=commands,
    )


def build_issue_pr_body(
    task: TaskContext,
    verdict: StageVerdict,
    *,
    reviewed_sha: str | None = None,
) -> str:
    """Build the inner PR description body (summary + clean spec + optional Reviewed-SHA)."""
    spec_text = clean_spec_block(verdict.updated_factory_block) or task.original_body
    body = f"## Summary\n{verdict.summary}\n\n## Specification\n{spec_text}"
    if reviewed_sha:
        body += f"\n\nReviewed-SHA: `{reviewed_sha}`"
    return body


def build_dry_run_gh_plan(
    task: TaskContext,
    verdict: StageVerdict,
    *,
    workspace: Path,
    run_dir: Path | None = None,
    reviewed_sha: str | None = None,
) -> DryRunPlan:
    """Save proposed Issue/PR bodies locally and return the exact `gh` commands for the human."""
    out_dir = run_dir or (workspace / ".factory" / "runs" / "dry-run")
    out_dir.mkdir(parents=True, exist_ok=True)

    cwd = Path.cwd().resolve()
    ws_resolved = workspace.resolve()
    outside_workspace = cwd != ws_resolved
    repo_slug = _extract_repo_slug(task.url)
    repo_flag = (
        f" -R {shlex.quote(repo_slug)}" if (outside_workspace and repo_slug) else ""
    )

    commands: list[str] = []
    if outside_workspace and not repo_slug:
        commands.append(f"cd {shlex.quote(str(ws_resolved))}")

    issue_body_file: Path | None = None
    pr_body_file: Path | None = None
    path_base = cwd if (outside_workspace and repo_slug) else ws_resolved

    # 1. Issue body update command (if factory_block changed)
    if verdict.updated_factory_block and (
        verdict.updated_factory_block != task.factory_block
    ):
        full_body = render_issue_body(task.original_body, verdict.updated_factory_block)
        issue_body_file = out_dir / f"issue-{task.issue_number}-body.md"
        issue_body_file.write_text(full_body, encoding="utf-8")
        rel_issue_file = _rel_or_abs(issue_body_file, path_base)
        commands.append(
            f"gh issue edit {task.issue_number}{repo_flag} --body-file {shlex.quote(str(rel_issue_file))}"
        )

    # 2. PR push & create command (if review accepted)
    if verdict.publish_pr:
        pr_title = f"{task.title} (#{task.issue_number})"
        inner_body = build_issue_pr_body(task, verdict, reviewed_sha=reviewed_sha)
        full_pr_body = f"Closes #{task.issue_number}\n\n{inner_body.strip()}\n"
        pr_body_file = out_dir / f"pr-{task.issue_number}-body.md"
        pr_body_file.write_text(full_pr_body, encoding="utf-8")
        rel_pr_file = _rel_or_abs(pr_body_file, path_base)
        git_prefix = (
            f"git -C {shlex.quote(str(ws_resolved))} " if outside_workspace else "git "
        )
        commands.append(f"{git_prefix}push -u origin {shlex.quote(task.branch)}")
        commands.append(
            f"gh pr create{repo_flag} --head {shlex.quote(task.branch)} "
            f"--title {shlex.quote(pr_title)} "
            f"--body-file {shlex.quote(str(rel_pr_file))}"
        )

    # 3. Label creation & transition commands (split remove/add to avoid gh parallel mutation race)
    if verdict.stage == "done":
        wt_rel = _rel_or_abs(
            ws_resolved / ".worktrees" / f"issue-{task.issue_number}", path_base
        )
        git_prefix = (
            f"git -C {shlex.quote(str(ws_resolved))} " if outside_workspace else "git "
        )
        commands.append(
            f"{git_prefix}worktree remove --force {shlex.quote(str(wt_rel))}"
        )

    label_color = LABEL_COLOR_MAP.get(verdict.target_label, "EDEDED")
    commands.append(
        f"gh label create {shlex.quote(verdict.target_label)} "
        f"--color {label_color} --force{repo_flag}"
    )

    labels_to_remove = [
        lbl
        for lbl in task.labels
        if lbl in ALL_FACTORY_STAGE_LABELS and lbl != verdict.target_label
    ]
    if labels_to_remove:
        rm_parts = ["gh", "issue", "edit", str(task.issue_number)]
        if repo_flag:
            rm_parts.extend(["-R", shlex.quote(repo_slug or "")])
        for rem in labels_to_remove:
            rm_parts.extend(["--remove-label", shlex.quote(rem)])
        commands.append(" ".join(rm_parts))

    add_parts = ["gh", "issue", "edit", str(task.issue_number)]
    if repo_flag:
        add_parts.extend(["-R", shlex.quote(repo_slug or "")])
    add_parts.extend(["--add-label", shlex.quote(verdict.target_label)])
    commands.append(" ".join(add_parts))

    return DryRunPlan(
        issue_number=task.issue_number,
        stage=verdict.stage,
        target_label=verdict.target_label,
        updated_factory_block=verdict.updated_factory_block,
        issue_body_file=issue_body_file,
        pr_body_file=pr_body_file,
        commands=commands,
    )


def _rel_or_abs(path: Path, base: Path) -> Path:
    try:
        return path.resolve().relative_to(base.resolve())
    except ValueError:
        return path.resolve()
