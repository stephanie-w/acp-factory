"""ACP client wrapper built directly on acpterm.acp_agent.ACPAgent."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
import uuid
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any
from unittest.mock import patch

import acpterm.acp_agent as acpterm_agent_mod
from acpterm.acp_agent import ACPAgent
from acpterm.config import Config as AcptermConfig

from ..config import AgentConfig, GuardrailsConfig
from ..resources import (
    parse_frontmatter_summary,
    resolve_resource_dirs,
    resolve_resources,
)
from ..resources import (
    resolve_persona_text as resolve_persona_text,
)
from ..resources import (
    resolve_skill_paths as resolve_skill_paths,
)
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
        resources_dir: Path | None = None,
        resource_dirs: list[Path] | None = None,
        instructions: list[str] | None = None,
        discover_apm_resources: bool | None = None,
        session_name: str | None = None,
        verbose: bool = False,
        silent: bool = False,
        read_only: bool | None = None,
        extra_env: dict[str, str] | None = None,
        strip_env: list[str] | set[str] | tuple[str, ...] | None = None,
        keep_env: list[str] | set[str] | tuple[str, ...] | None = None,
        guardrails: GuardrailsConfig | None = None,
        deny_runs: bool = False,
        thought_max_chars: int | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.config_dir = config_dir.resolve() if config_dir else self.workspace
        self.resource_dirs: list[Path] = resolve_resource_dirs(
            resources_dir=resources_dir,
            resource_dirs=resource_dirs,
            config_dir=self.config_dir,
        )
        self.resources_dir: Path | None = (
            self.resource_dirs[0] if self.resource_dirs else None
        )
        self.agent_config = agent_config
        self.session_name = session_name or f"factory-{uuid.uuid4().hex[:8]}"
        self.verbose = verbose
        self.silent = silent
        self.read_only = agent_config.read_only if read_only is None else read_only
        self.extra_env = extra_env
        self.strip_env = strip_env
        self.keep_env = keep_env
        self.guardrails = guardrails or GuardrailsConfig()
        self.deny_runs = deny_runs
        self.thought_max_chars = thought_max_chars

        effective_discover = (
            discover_apm_resources
            if discover_apm_resources is not None
            else (
                agent_config.discover_apm_resources
                if agent_config.discover_apm_resources is not None
                else True
            )
        )
        merged_instructions = list(
            dict.fromkeys([*(instructions or []), *agent_config.instructions])
        )
        resolved = resolve_resources(
            persona=agent_config.persona,
            instructions=merged_instructions,
            skills=agent_config.skills,
            workspace=self.workspace,
            config_dir=self.config_dir,
            resource_dirs=self.resource_dirs,
            discover_apm=effective_discover,
        )
        self.persona_text: str | None = resolved.persona_text
        self.instruction_paths: list[Path] = resolved.instructions
        self.explicit_skill_paths: list[Path] = resolved.explicit_skills
        self.discovered_skill_paths: list[Path] = resolved.discovered_skills
        self.skill_paths: list[Path] = resolved.skills
        self._persona_injected: bool = False

        self.tracker = TurnEventTracker(thought_max_chars=thought_max_chars)
        self._agent: ACPAgent | None = None

    @property
    def session_id(self) -> str | None:
        return self._agent.session_id if self._agent else None

    async def start(self, *, load_existing: bool = False) -> None:
        """Spawn the ACP agent process in the target workspace and initialize a session."""
        cmd = resolve_factory_agent_command(self.agent_config)
        acpterm_cfg = AcptermConfig.load()

        hooks: list[Any] = []
        if self.guardrails and self.guardrails.enabled:
            from acpterm.hooks import HookDefinition

            hook_cmd = f'"{sys.executable}" -m acp_factory.runner.guardrails'
            for res_dir in self.resource_dirs:
                hook_cmd += f' --resources-dir "{res_dir}"'
            for p in self.guardrails.allowed_paths:
                hook_cmd += f' --allow-path "{p}"'
            for d in self.guardrails.denied_commands:
                hook_cmd += f' --deny-cmd "{d}"'
            if self.deny_runs:
                hook_cmd += " --deny-runs"

            hooks.append(
                HookDefinition(
                    name="factory-guardrails",
                    on="permission",
                    run=hook_cmd,
                )
            )
        hooks.extend(acpterm_cfg.hooks)

        self._agent = ACPAgent(
            project_root=self.workspace,
            agent_binary=self.agent_config.name,
            session_name=self.session_name,
            auto_approve=True,
            verbose=self.verbose,
            read_only=self.read_only,
            silent=self.silent,
            transcript_recorder=self.tracker.recorder,
            permission_hooks=hooks,
        )

        orig_spawn = acpterm_agent_mod.spawn_stdio_transport

        def _spawn_in_workspace(
            command: str,
            *args: str,
            env: Mapping[str, str] | None = None,
            cwd: str | Path | None = None,
            stderr: int | None = -1,
            limit: int | None = None,
            shutdown_timeout: float = 2.0,
        ) -> AbstractAsyncContextManager[
            tuple[
                asyncio.StreamReader, asyncio.StreamWriter, asyncio.subprocess.Process
            ]
        ]:
            from ..runner.checks import build_workspace_env

            _ = env
            return orig_spawn(
                command,
                *args,
                env=build_workspace_env(
                    self.workspace,
                    restrict_gh_writes=True,
                    resources_dir=self.resources_dir,
                    extra_env=self.extra_env,
                    strip_env=self.strip_env,
                    keep_env=self.keep_env,
                ),
                cwd=str(self.workspace),
                stderr=stderr,
                limit=limit,
                shutdown_timeout=shutdown_timeout,
            )

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

    @staticmethod
    def _format_resource_entry(path: Path, *, kind: str) -> str:
        """Format a skill or instruction path with optional YAML frontmatter summary."""
        meta = parse_frontmatter_summary(path)
        parts: list[str] = []
        if kind == "skill" and meta.get("name"):
            parts.append(f"**{meta['name']}**")
        if kind == "instruction" and meta.get("applyTo"):
            parts.append(f"applies to `{meta['applyTo']}`")
        if meta.get("description"):
            parts.append(meta["description"])
        if parts:
            return f"- `{path}` — {' | '.join(parts)}"
        return f"- `{path}`"

    @classmethod
    def _format_skill_block(
        cls,
        heading: str,
        intro: str,
        skills: list[Path],
        *,
        script_label: str = "Validation Script",
    ) -> str:
        """Render a Markdown skill section with frontmatter summaries, templates, and scripts."""
        lines = [heading, intro]
        for sp in skills:
            lines.append(cls._format_resource_entry(sp, kind="skill"))
            templates_dir = sp.parent / "templates"
            if templates_dir.is_dir():
                for tmpl in sorted(templates_dir.glob("*.md")):
                    lines.append(f"  - Template: `{tmpl}`")
            scripts_dir = sp.parent / "scripts"
            if scripts_dir.is_dir():
                for script in sorted(scripts_dir.glob("*.py")):
                    lines.append(f"  - {script_label}: `{script}`")
        return "\n".join(lines)

    def _decorate_first_turn_prompt(
        self,
        prompt: str,
        attached_skills: list[Path],
        attached_instructions: list[Path] | None = None,
        discovered_skills: list[Path] | None = None,
    ) -> str:
        """Prepend persona, domain instructions, and active/workspace skill references on turn 1."""
        sections: list[str] = []
        if self.persona_text:
            sections.append(f"# Agent Persona & Role\n{self.persona_text}")
        if attached_instructions:
            instr_lines = [
                "# Domain & Policy Instructions",
                "Read and follow the applicable domain/policy instructions:",
            ]
            for ip in attached_instructions:
                instr_lines.append(self._format_resource_entry(ip, kind="instruction"))
            sections.append("\n".join(instr_lines))
        if attached_skills:
            sections.append(
                self._format_skill_block(
                    "# Active Skills",
                    "Read and follow the instructions, templates, and validation scripts in these attached skill packages:",
                    attached_skills,
                )
            )
        if discovered_skills:
            sections.append(
                self._format_skill_block(
                    "# Available Workspace Skills",
                    "Read and use any skill below only when relevant to the task (based on its description):",
                    discovered_skills,
                    script_label="Helper Script",
                )
            )
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
            for ip in self.instruction_paths:
                if ip not in turn_resources:
                    turn_resources.append(ip)
            for sp in self.explicit_skill_paths:
                if sp not in turn_resources:
                    turn_resources.append(sp)
            if self.persona_text or self.skill_paths or self.instruction_paths:
                effective_prompt = self._decorate_first_turn_prompt(
                    prompt,
                    self.explicit_skill_paths,
                    self.instruction_paths,
                    self.discovered_skill_paths,
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

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()
