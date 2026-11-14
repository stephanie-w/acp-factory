"""Sequential shell and review check runner with fail-fast logic and output truncation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from ..acp.client import FactoryACPClient
    from ..config import CheckConfig


class StructuredCheckVerdict(BaseModel):
    """Pydantic schema for structured JSON review check outputs."""

    status: Literal["pass", "retry", "stop"]
    feedback: str = ""


@dataclass
class CheckResult:
    """Outcome of a single validation check."""

    name: str
    check_type: Literal["cmd", "prompt"]
    command_or_prompt: str
    passed: bool
    exit_code: int
    raw_output: str
    truncated_output: str
    duration_seconds: float
    status: Literal["pass", "retry", "stop"] = "pass"


_GH_READONLY_SHIM_SCRIPT = """\
#!/usr/bin/env python3
import os
import shutil
import sys

FORBIDDEN_SUBCOMMANDS = {
    "edit", "create", "comment", "close", "reopen", "delete",
    "merge", "lock", "unlock", "transfer", "pin", "unpin",
    "ready", "review",
}

args = [a for a in sys.argv[1:] if not a.startswith("-")]
if any(a in FORBIDDEN_SUBCOMMANDS for a in args[:3]):
    sys.stderr.write(
        "[acp-factory policy] Mutating 'gh' commands are disabled for agents. "
        "Output your specification, findings, and STATUS in your final response; "
        "the orchestrator or human will apply GitHub updates.\\n"
    )
    sys.exit(1)

shim_dir = os.path.dirname(os.path.abspath(__file__))
path_parts = [
    p for p in os.environ.get("PATH", "").split(os.pathsep)
    if p and os.path.abspath(p) != shim_dir
]
clean_env = dict(os.environ)
clean_env["PATH"] = os.pathsep.join(path_parts)
real_gh = shutil.which("gh", path=clean_env["PATH"])
if not real_gh:
    sys.stderr.write("gh: command not found\\n")
    sys.exit(127)
os.execvpe(real_gh, [real_gh, *sys.argv[1:]], clean_env)
"""

# Environment variables from host orchestrator sessions that leak into
# and pollute child workspace subprocesses (checks, setup, ACP agent).
DEFAULT_HOST_ENV_STRIP: tuple[str, ...] = (
    # Python / Virtualenv / Conda
    "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT",
    "PYTHONPATH",
    "PYTHONHOME",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "CONDA_PROMPT_MODIFIER",
    # Node / npm / pnpm / yarn
    "NODE_PATH",
    "NODE_OPTIONS",
    "INIT_CWD",
    "npm_config_prefix",
    # Ruby / Bundler
    "BUNDLE_GEMFILE",
    "BUNDLE_BIN_PATH",
    "GEM_HOME",
    "GEM_PATH",
    "RUBYOPT",
)


def _ensure_readonly_gh_shim_dir() -> Path:
    """Create and return the directory containing the read-only `gh` shim for agents."""
    shim_dir = Path(tempfile.gettempdir()) / "acp-factory-gh-shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_file = shim_dir / "gh"
    if (
        not shim_file.exists()
        or shim_file.read_text(encoding="utf-8") != _GH_READONLY_SHIM_SCRIPT
    ):
        shim_file.write_text(_GH_READONLY_SHIM_SCRIPT, encoding="utf-8")
        shim_file.chmod(
            stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
        )
    return shim_dir


def build_workspace_env(
    cwd: Path,
    *,
    restrict_gh_writes: bool = False,
    extra_env: dict[str, str] | None = None,
    resources_dir: Path | None = None,
    strip_env: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Prepare the environment for subprocesses executing in the target workspace/worktree.

    Language-agnostic: strips host runner environment leaks, optionally prepends a
    read-only `gh` shim so agents cannot directly mutate GitHub issues, PRs, or labels,
    and applies any caller-provided overrides.
    """
    from ..config import resolve_resources_dir

    _ = cwd
    env = dict(os.environ)

    # 1. Purge host virtual environment bin paths from PATH before stripping keys
    host_venv = env.get("VIRTUAL_ENV")
    host_conda = env.get("CONDA_PREFIX")
    paths_to_clean: set[str] = set()
    if host_venv:
        paths_to_clean.add(
            os.path.abspath(Path(host_venv) / ("Scripts" if os.name == "nt" else "bin"))
        )
    if host_conda:
        paths_to_clean.add(
            os.path.abspath(
                Path(host_conda) / ("Scripts" if os.name == "nt" else "bin")
            )
        )

    if paths_to_clean and "PATH" in env:
        current_paths = env["PATH"].split(os.pathsep)
        env["PATH"] = os.pathsep.join(
            p for p in current_paths if os.path.abspath(p) not in paths_to_clean
        )

    # 2. Strip declarative default host variables and any custom strip_env keys
    keys_to_strip = set(DEFAULT_HOST_ENV_STRIP)
    if strip_env:
        keys_to_strip.update(strip_env)

    for k in keys_to_strip:
        env.pop(k, None)

    bundled_repo_root = Path(__file__).resolve().parents[3]
    resolved_res = resources_dir or resolve_resources_dir()
    if resolved_res is not None and (resolved_res / "skills").is_dir():
        skills_dir = str((resolved_res / "skills").resolve())
    elif resolved_res is not None and resolved_res.is_dir():
        skills_dir = str(resolved_res.resolve())
    else:
        skills_dir = str(bundled_repo_root / "skills")
    env.setdefault("FACTORY_SKILLS_DIR", skills_dir)
    env.setdefault("FACTORY_BUNDLED_SKILLS_DIR", skills_dir)

    if restrict_gh_writes:
        shim_dir = str(_ensure_readonly_gh_shim_dir())
        current_path = env.get("PATH", "")
        env["PATH"] = (
            f"{shim_dir}{os.pathsep}{current_path}" if current_path else shim_dir
        )
    if extra_env:
        env.update(extra_env)
    return env


def truncate_check_output(text: str, max_lines: int = 150) -> str:
    """Truncate check output intelligently to avoid overflowing the agent's context window.

    Preserves the first few lines (command header/summary) and the last `max_lines`
    lines where stack traces and assertion failures typically appear.
    """
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text

    head_count = min(15, max(5, max_lines // 10))
    tail_count = max(1, max_lines - head_count)
    omitted = len(lines) - (head_count + tail_count)
    if omitted <= 0:
        return text

    head = "\n".join(lines[:head_count])
    tail = "\n".join(lines[-tail_count:])
    return (
        f"{head}\n\n"
        f"... [truncated {omitted} lines of output; showing last {tail_count} lines] ...\n\n"
        f"{tail}"
    )


async def run_shell_command(
    cmd: str,
    cwd: Path,
    *,
    timeout: int = 300,
    extra_env: dict[str, str] | None = None,
    strip_env: list[str] | set[str] | tuple[str, ...] | None = None,
) -> tuple[int, str, float]:
    """Execute a shell command in its own process group with clean termination on timeout/cancel."""
    start = time.monotonic()
    env = build_workspace_env(cwd, extra_env=extra_env, strip_env=strip_env)

    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )

    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        duration = max(0.0, time.monotonic() - start)
        output = stdout_bytes.decode("utf-8", errors="replace")
        return proc.returncode or 0, output, duration
    except TimeoutError:
        _terminate_process_group(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        duration = max(0.0, time.monotonic() - start)
        return 124, f"Command timed out after {timeout} seconds: {cmd}", duration
    except asyncio.CancelledError:
        _terminate_process_group(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        raise


def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the process group created by start_new_session=True."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        proc.kill()


_VERDICT_PASS_RE = re.compile(r"\bVERDICT\s*:\s*PASS\b", re.IGNORECASE)
_VERDICT_FAIL_RE = re.compile(r"\bVERDICT\s*:\s*(?:FAIL|RETRY)\b", re.IGNORECASE)
_VERDICT_STOP_RE = re.compile(r"\bVERDICT\s*:\s*STOP\b", re.IGNORECASE)
_JSON_BLOCK_RE = re.compile(
    r"\{[^{}]*\"status\"\s*:\s*\"(?:pass|retry|stop)\"[^{}]*\}", re.DOTALL
)


def parse_review_verdict_status(
    response_text: str,
) -> tuple[Literal["pass", "retry", "stop"], str]:
    """Parse structured JSON or VERDICT line from a review check response."""
    json_match = _JSON_BLOCK_RE.search(response_text)
    if json_match:
        with contextlib.suppress(Exception):
            parsed = StructuredCheckVerdict.model_validate_json(json_match.group(0))
            return parsed.status, (parsed.feedback or response_text)

    if _VERDICT_STOP_RE.search(response_text):
        return "stop", response_text
    if _VERDICT_FAIL_RE.search(response_text):
        return "retry", response_text
    if _VERDICT_PASS_RE.search(response_text):
        return "pass", response_text

    non_empty = [line.strip() for line in response_text.splitlines() if line.strip()]
    if non_empty:
        last_line = non_empty[-1].upper()
        if "STOP" in last_line:
            return "stop", response_text
        if "FAIL" in last_line and "PASS" not in last_line:
            return "retry", response_text
        if "PASS" in last_line and "FAIL" not in last_line:
            return "pass", response_text
    return "retry", response_text


def parse_review_verdict(response_text: str) -> bool:
    """Parse whether an agent review response indicates PASS or FAIL."""
    status, _ = parse_review_verdict_status(response_text)
    return status == "pass"


async def run_single_check(
    check: CheckConfig,
    cwd: Path,
    *,
    acp_client: FactoryACPClient | None = None,
    config_dir: Path | None = None,
    resources_dir: Path | None = None,
    max_output_lines: int = 150,
    extra_env: dict[str, str] | None = None,
    strip_env: list[str] | set[str] | tuple[str, ...] | None = None,
) -> CheckResult:
    """Execute a single check (either deterministic shell command or agent review prompt)."""
    if check.cmd is not None:
        rc, output, duration = await run_shell_command(
            check.cmd,
            cwd,
            timeout=check.timeout,
            extra_env=extra_env,
            strip_env=strip_env,
        )
        passed = rc == 0
        chk_status: Literal["pass", "retry", "stop"] = "pass" if rc == 0 else "retry"
        return CheckResult(
            name=check.name,
            check_type="cmd",
            command_or_prompt=check.cmd,
            passed=passed,
            exit_code=rc,
            raw_output=output,
            truncated_output=truncate_check_output(output, max_lines=max_output_lines),
            duration_seconds=duration,
            status=chk_status,
        )

    if check.prompt is not None:
        from ..acp.client import (
            FactoryACPClient as _FactoryACPClient,
        )
        from ..acp.client import (
            resolve_persona_text,
            resolve_skill_paths,
        )
        from ..config import AgentConfig

        start = time.monotonic()
        persona_header = ""
        if check.persona:
            resolved_persona = resolve_persona_text(
                check.persona,
                workspace=cwd,
                config_dir=config_dir,
                resources_dir=resources_dir,
            )
            if resolved_persona:
                persona_header = f"# Reviewer Persona\n{resolved_persona}\n\n"

        skill_resources = resolve_skill_paths(
            check.skills,
            workspace=cwd,
            config_dir=config_dir,
            resources_dir=resources_dir,
        )

        review_instruction = (
            f"{persona_header}"
            "## Verification Review Check\n"
            f"**Check Name:** {check.name}\n\n"
            "Evaluate the current workspace implementation against the following criteria. "
            "Do NOT modify any files during this review check.\n\n"
            f"**Criteria:**\n{check.prompt}\n\n"
            "Provide a concise explanation of your findings, and conclude your response "
            "with an explicit final line:\n"
            "`VERDICT: PASS` if all criteria are satisfied, or `VERDICT: FAIL` if any criteria are not met."
        )

        # If the check defines a custom agent or model (cross-agent/cross-model review),
        # spawn a dedicated read-only session for the review check.
        if check.agent or check.model or acp_client is None:
            if acp_client is None and not check.agent:
                raise RuntimeError(
                    f"Cannot execute review prompt check '{check.name}' without an active FactoryACPClient."
                )
            base_agent_name = check.agent or (
                acp_client.agent_config.name if acp_client else "opencode"
            )
            review_agent_cfg = AgentConfig(
                name=base_agent_name,
                model=check.model
                or (acp_client.agent_config.model if acp_client else None),
                read_only=True,
                timeout=check.timeout,
            )
            async with _FactoryACPClient(
                workspace=cwd,
                agent_config=review_agent_cfg,
                config_dir=config_dir,
                resources_dir=resources_dir,
                read_only=True,
                deny_runs=True,
                extra_env=extra_env,
                strip_env=strip_env,
            ) as reviewer_client:
                turn_res = await reviewer_client.send_prompt(
                    review_instruction,
                    timeout=check.timeout,
                    resources=skill_resources or None,
                )
        else:
            prev_ro = acp_client.read_only
            await acp_client.set_read_only(True)
            try:
                turn_res = await acp_client.send_prompt(
                    review_instruction,
                    timeout=check.timeout,
                    resources=skill_resources or None,
                )
            finally:
                await acp_client.set_read_only(prev_ro)

        duration = max(0.0, time.monotonic() - start)
        response_text = turn_res.summary.response_text
        rev_status, feedback_text = parse_review_verdict_status(response_text)
        passed = rev_status == "pass"
        return CheckResult(
            name=check.name,
            check_type="prompt",
            command_or_prompt=check.prompt,
            exit_code=0 if passed else (1 if rev_status == "retry" else 2),
            raw_output=response_text,
            truncated_output=truncate_check_output(
                feedback_text, max_lines=max_output_lines
            ),
            duration_seconds=duration,
            status=rev_status,
            passed=passed,
        )

    raise ValueError(f"Invalid check configuration for '{check.name}'")


_UNSAFE_RESPONSE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _isolated_response_file(
    response_file: Path, snapshot: bytes, index: int, check_name: str
) -> Path:
    """Write a per-check copy of the response file so checks cannot mutate shared state."""
    slug = (
        _UNSAFE_RESPONSE_FILENAME_RE.sub("-", check_name.strip()).strip("-") or "check"
    )
    suffix = response_file.suffix or ".md"
    isolated = response_file.with_name(f"check-{index}-{slug}{suffix}")
    isolated.write_bytes(snapshot)
    return isolated


def _restore_response_file(response_file: Path | None, snapshot: bytes | None) -> None:
    """Best-effort restore of the original bytes if a check mutated the shared path."""
    if response_file is None or snapshot is None:
        return
    with contextlib.suppress(OSError):
        if not response_file.is_file() or response_file.read_bytes() != snapshot:
            response_file.write_bytes(snapshot)


async def run_checks_sequential(
    checks: list[CheckConfig],
    cwd: Path,
    *,
    acp_client: FactoryACPClient | None = None,
    config_dir: Path | None = None,
    resources_dir: Path | None = None,
    max_output_lines: int = 150,
    on_check_complete: Callable[[CheckResult], None] | None = None,
    extra_env: dict[str, str] | None = None,
    strip_env: list[str] | set[str] | tuple[str, ...] | None = None,
) -> tuple[bool, list[CheckResult]]:
    """Run all configured checks in sequence, stopping immediately at the first failure (fail-fast)."""
    response_file: Path | None = None
    response_snapshot: bytes | None = None
    if extra_env:
        raw_response_path = extra_env.get("FACTORY_RESPONSE_FILE")
        if raw_response_path:
            candidate = Path(raw_response_path)
            if not candidate.is_absolute():
                candidate = cwd / candidate
            if candidate.is_file():
                response_file = candidate
                response_snapshot = candidate.read_bytes()

    results: list[CheckResult] = []
    for index, check in enumerate(checks, start=1):
        check_env = extra_env
        isolated_file: Path | None = None
        try:
            if response_file is not None and response_snapshot is not None:
                isolated_file = _isolated_response_file(
                    response_file, response_snapshot, index, check.name
                )
                check_env = {
                    **(extra_env or {}),
                    "FACTORY_RESPONSE_FILE": str(isolated_file),
                }
            result = await run_single_check(
                check,
                cwd,
                acp_client=acp_client,
                config_dir=config_dir,
                resources_dir=resources_dir,
                max_output_lines=max_output_lines,
                extra_env=check_env,
                strip_env=strip_env,
            )
        finally:
            if isolated_file is not None:
                with contextlib.suppress(OSError):
                    isolated_file.unlink()
            _restore_response_file(response_file, response_snapshot)
        results.append(result)
        if on_check_complete is not None:
            on_check_complete(result)
        if not result.passed:
            return False, results
    return True, results
