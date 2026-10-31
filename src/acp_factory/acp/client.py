"""ACP client wrapper built directly on acpterm.acp_agent.ACPAgent."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import acpterm.acp_agent as acpterm_agent_mod
from acpterm.acp_agent import ACPAgent
from acpterm.config import Config as AcptermConfig

from ..config import AgentConfig, _load_markdown_or_inline
from .events import TurnEventTracker, TurnSummary


@dataclass
class TurnResult:
    """Result of sending a prompt turn to the ACP agent."""

    stop_reason: str
    summary: TurnSummary


def resolve_factory_agent_command(agent_cfg: AgentConfig) -> list[str]:
    """Resolve the command list for an agent using either explicit config or acpterm's config."""
    acpterm_cfg = AcptermConfig.load()

    if agent_cfg.command:
        if len(agent_cfg.command) == 1 and agent_cfg.command[0] in acpterm_cfg.agents:
            return acpterm_cfg.get_agent_command(agent_cfg.command[0])
        binary = agent_cfg.command[0]
        if not shutil.which(binary) and binary in acpterm_cfg.agents:
            return acpterm_cfg.get_agent_command(binary)
        return list(agent_cfg.command)

    return acpterm_cfg.get_agent_command(agent_cfg.name)


def resolve_persona_text(
    persona: str | None,
    *,
    workspace: Path,
    config_dir: Path | None = None,
) -> str | None:
    """Load a stage/agent persona from a Markdown file or return inline persona text."""
    if not persona or not persona.strip():
        return None
    return _load_markdown_or_inline(
        persona,
        config_dir=config_dir,
        workspace=workspace,
    ).strip()


def resolve_skill_paths(
    skills: list[str],
    *,
    workspace: Path,
    config_dir: Path | None = None,
) -> list[Path]:
    """Resolve configured skill paths or APM skill identifiers to concrete SKILL.md Paths.

    Search order for each skill entry:
    1. Absolute path (file or directory containing SKILL.md)
    2. Relative to config_dir or workspace (file or directory containing SKILL.md)
    3. `.factory/skills/<skill>/SKILL.md`, `skills/<skill>/SKILL.md`, `apm_modules/<skill>/SKILL.md`
    """
    resolved_paths: list[Path] = []
    search_roots: list[Path] = []
    if config_dir is not None:
        search_roots.append(config_dir.resolve())
    search_roots.append(workspace.resolve())

    for entry in skills:
        raw = entry.strip()
        if not raw:
            continue

        candidate = Path(raw)
        found: Path | None = None

        if candidate.is_absolute():
            if candidate.is_file():
                found = candidate
            elif candidate.is_dir() and (candidate / "SKILL.md").is_file():
                found = candidate / "SKILL.md"
        else:
            for root in search_roots:
                direct = (root / candidate).resolve()
                if direct.is_file():
                    found = direct
                    break
                if direct.is_dir() and (direct / "SKILL.md").is_file():
                    found = direct / "SKILL.md"
                    break

                # Check standard skill & APM directories
                for subdir in (
                    ".factory/skills",
                    "skills",
                    "apm_modules",
                    ".apm/skills",
                ):
                    skill_file = (root / subdir / raw / "SKILL.md").resolve()
                    if skill_file.is_file():
                        found = skill_file
                        break
                if found is not None:
                    break

        if found is None:
            raise FileNotFoundError(
                f"Skill '{raw}' not found. Expected a SKILL.md file path or skill package "
                f"in .factory/skills/, skills/, or apm_modules/ under {workspace}."
            )
        if found not in resolved_paths:
            resolved_paths.append(found)

    return resolved_paths


class FactoryACPClient:
    """Thin async ACP client wrapping acpterm.acp_agent.ACPAgent.

    Maintains a persistent ACP session across multiple feedback loop attempts
    and injects configured stage personas and APM/ACP skills (`SKILL.md`).
    """

    def __init__(
        self,
        workspace: Path,
        agent_config: AgentConfig,
        *,
        config_dir: Path | None = None,
        session_name: str | None = None,
        verbose: bool = False,
        silent: bool = False,
        read_only: bool | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.config_dir = config_dir.resolve() if config_dir else self.workspace
        self.agent_config = agent_config
        self.session_name = session_name or f"factory-{uuid.uuid4().hex[:8]}"
        self.verbose = verbose
        self.silent = silent
        self.read_only = agent_config.read_only if read_only is None else read_only

        self.persona_text: str | None = resolve_persona_text(
            agent_config.persona,
            workspace=self.workspace,
            config_dir=self.config_dir,
        )
        self.skill_paths: list[Path] = resolve_skill_paths(
            agent_config.skills,
            workspace=self.workspace,
            config_dir=self.config_dir,
        )
        self._persona_injected: bool = False

        self.tracker = TurnEventTracker()
        self._agent: ACPAgent | None = None

    @property
    def session_id(self) -> str | None:
        return self._agent.session_id if self._agent else None

    async def start(self, *, load_existing: bool = False) -> None:
        """Spawn the ACP agent process in the target workspace and initialize a session."""
        cmd = resolve_factory_agent_command(self.agent_config)
        acpterm_cfg = AcptermConfig.load()

        self._agent = ACPAgent(
            project_root=self.workspace,
            agent_binary=self.agent_config.name,
            session_name=self.session_name,
            auto_approve=True,
            verbose=self.verbose,
            read_only=self.read_only,
            silent=self.silent,
            transcript_recorder=self.tracker.recorder,
            permission_hooks=acpterm_cfg.hooks,
        )

        orig_spawn = acpterm_agent_mod.spawn_stdio_transport

        def _spawn_in_workspace(command: str, *args: str, **kwargs: Any) -> Any:
            from ..runner.checks import build_workspace_env

            kwargs["cwd"] = str(self.workspace)
            kwargs["env"] = build_workspace_env(self.workspace, restrict_gh_writes=True)
            return orig_spawn(command, *args, **kwargs)

        try:
            with (
                patch.object(
                    acpterm_agent_mod, "resolve_agent_command", return_value=cmd
                ),
                patch.object(
                    acpterm_agent_mod,
                    "spawn_stdio_transport",
                    side_effect=_spawn_in_workspace,
                ),
            ):
                await asyncio.wait_for(
                    self._agent.start(
                        load_existing=load_existing,
                        model_override=self.agent_config.model,
                        mode_override=self.agent_config.mode,
                    ),
                    timeout=self.agent_config.timeout,
                )
        except BaseException:
            await self.stop()
            raise

    def _decorate_first_turn_prompt(
        self, prompt: str, attached_skills: list[Path]
    ) -> str:
        """Prepend persona instructions and active skill references on the initial turn."""
        sections: list[str] = []
        if self.persona_text:
            sections.append(f"# Agent Persona & Role\n{self.persona_text}")
        if attached_skills:
            skill_lines = ["# Active Skills"]
            skill_lines.append(
                "Read and follow the instructions in these attached skill definitions:"
            )
            for sp in attached_skills:
                skill_lines.append(f"- `{sp}`")
            sections.append("\n".join(skill_lines))
        sections.append(prompt)
        return "\n\n".join(sections)

    async def send_prompt(
        self,
        prompt: str,
        *,
        timeout: int | None = None,
        resources: list[Path] | None = None,
    ) -> TurnResult:
        """Send a prompt in the active ACP session and stream updates via acpterm."""
        if self._agent is None:
            raise RuntimeError("FactoryACPClient has not been started.")

        effective_timeout = timeout or self.agent_config.timeout
        turn_resources: list[Path] = list(resources) if resources else []

        effective_prompt = prompt
        if not self._persona_injected:
            for sp in self.skill_paths:
                if sp not in turn_resources:
                    turn_resources.append(sp)
            if self.persona_text or self.skill_paths:
                effective_prompt = self._decorate_first_turn_prompt(
                    prompt, self.skill_paths
                )
            self._persona_injected = True

        self.tracker.begin_turn(effective_prompt)

        stop_reason = await asyncio.wait_for(
            self._agent.send_prompt(
                effective_prompt,
                resources=turn_resources or None,
            ),
            timeout=effective_timeout,
        )
        summary = self.tracker.end_turn(stop_reason or "end_turn")
        return TurnResult(stop_reason=stop_reason or "end_turn", summary=summary)

    async def set_read_only(self, read_only: bool) -> None:
        """Update read-only flag for review turns."""
        self.read_only = read_only
        if self._agent is not None:
            self._agent._read_only = read_only

    async def stop(self) -> None:
        """Cleanly close the ACP session and terminate the agent subprocess."""
        if self._agent is not None:
            with contextlib.suppress(Exception):
                await self._agent.close_session()
            with contextlib.suppress(Exception):
                await self._agent.stop()
            self._agent = None

    async def __aenter__(self) -> FactoryACPClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()
