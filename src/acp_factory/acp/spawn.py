"""Single process-spawn seam between acp-factory and acpterm.

``acpterm.acp_agent`` resolves the spawn command from its own config, passes the
host environment straight through, and records the session handshake in its
on-disk cache. Factory needs the command, environment, and working directory to
come from factory configuration instead. The pinned acpterm revision exposes no
injection point for any of those, so this adapter keeps the one unavoidable
workaround localized here rather than scattered across client and tests.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Generator, Mapping
from contextlib import AbstractAsyncContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import acpterm.acp_agent as acpterm_agent_mod
from acpterm import agent_cache as acpterm_agent_cache
from acpterm.acp_agent import ACPAgent
from acpterm.config import Config as AcptermConfig

from ..config import AgentConfig


@dataclass(frozen=True)
class SpawnSpec:
    """Resolved inputs for one agent process spawn."""

    command: list[str]
    workspace: Path
    resources_dir: Path | None = None
    extra_env: dict[str, str] | None = None
    strip_env: list[str] | set[str] | tuple[str, ...] | None = None
    keep_env: list[str] | set[str] | tuple[str, ...] | None = None
    load_existing: bool = False
    model_override: str | None = None
    mode_override: str | None = None
    timeout: int = 600


@dataclass(frozen=True)
class SpawnHandshake:
    """Session negotiation data observed while the agent process started."""

    observed: bool = False
    config_options: Any = None


class ProcessSpawner(Protocol):
    """Adapter responsible for resolving, configuring, and spawning ACP agents."""

    def resolve_command(self, agent_cfg: AgentConfig) -> list[str]:
        """Resolve the executable command for ``agent_cfg``."""
        ...

    def create_agent(
        self,
        *,
        workspace: Path,
        agent_binary: str,
        session_name: str,
        auto_approve: bool,
        verbose: bool,
        read_only: bool,
        silent: bool,
        transcript_recorder: Any,
        permission_hooks: list[Any],
    ) -> ACPAgent:
        """Construct the agent object that will own the spawned process."""
        ...

    def build_env(self, spec: SpawnSpec) -> dict[str, str]:
        """Build the workspace-scoped environment for ``spec``."""
        ...

    async def start(self, agent: ACPAgent, spec: SpawnSpec) -> SpawnHandshake:
        """Spawn ``agent`` using ``spec`` and return its handshake data."""
        ...


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


def _validate_spec(spec: SpawnSpec) -> None:
    """Reject spawn specs that cannot launch a process."""
    if not spec.command:
        raise ValueError("SpawnSpec.command must contain at least one token.")
    if any(not isinstance(part, str) or not part for part in spec.command):
        raise ValueError("SpawnSpec.command must not contain empty tokens.")


@contextmanager
def _installed_spawn_globals(
    *,
    resolve_command: Any,
    spawn: Any,
    store: Any,
) -> Generator[None]:
    """Temporarily install the factory spawn overrides, then restore acpterm's own.

    Every original is captured before any assignment so a failing start can
    never leave partially rewired module state behind.
    """
    overrides = [
        (acpterm_agent_mod, "resolve_agent_command", resolve_command),
        (acpterm_agent_mod, "spawn_stdio_transport", spawn),
        (acpterm_agent_cache, "store", store),
    ]
    originals = [
        (module, name, getattr(module, name)) for module, name, _value in overrides
    ]
    try:
        for module, name, value in overrides:
            setattr(module, name, value)
        yield
    finally:
        for module, name, value in originals:
            setattr(module, name, value)


class AcptermProcessSpawner:
    """Production adapter over the pinned acpterm ``ACPAgent`` implementation."""

    def resolve_command(self, agent_cfg: AgentConfig) -> list[str]:
        """Resolve the command list for ``agent_cfg`` through factory/acpterm config."""
        return resolve_factory_agent_command(agent_cfg)

    def create_agent(
        self,
        *,
        workspace: Path,
        agent_binary: str,
        session_name: str,
        auto_approve: bool,
        verbose: bool,
        read_only: bool,
        silent: bool,
        transcript_recorder: Any,
        permission_hooks: list[Any],
    ) -> ACPAgent:
        """Construct an ``ACPAgent`` configured from factory settings."""
        return ACPAgent(
            project_root=workspace,
            agent_binary=agent_binary,
            session_name=session_name,
            auto_approve=auto_approve,
            verbose=verbose,
            read_only=read_only,
            silent=silent,
            transcript_recorder=transcript_recorder,
            permission_hooks=permission_hooks,
        )

    def build_env(self, spec: SpawnSpec) -> dict[str, str]:
        """Build the workspace environment, including the read-only ``gh`` shim."""
        from ..runner.checks import build_workspace_env

        return build_workspace_env(
            spec.workspace,
            restrict_gh_writes=True,
            resources_dir=spec.resources_dir,
            extra_env=spec.extra_env,
            strip_env=spec.strip_env,
            keep_env=spec.keep_env,
        )

    async def start(self, agent: ACPAgent, spec: SpawnSpec) -> SpawnHandshake:
        """Spawn ``agent`` inside the workspace encoded by ``spec``."""
        _validate_spec(spec)

        handshake: dict[str, Any] = {"observed": False, "config_options": None}
        original_spawn = acpterm_agent_mod.spawn_stdio_transport
        original_store = acpterm_agent_cache.store

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
            """Ignore acpterm's host env/cwd and spawn with factory's workspace values."""
            _ = env, cwd
            return original_spawn(
                command,
                *args,
                env=self.build_env(spec),
                cwd=str(spec.workspace),
                stderr=stderr,
                limit=limit,
                shutdown_timeout=shutdown_timeout,
            )

        def _capture_store(
            agent_name: str,
            config_options: Any = None,
            modes: Any = None,
            commands: Any = None,
        ) -> None:
            """Record the live handshake before acpterm caches and discards it."""
            handshake["observed"] = True
            handshake["config_options"] = config_options
            original_store(
                agent_name,
                config_options=config_options,
                modes=modes,
                commands=commands,
            )

        with _installed_spawn_globals(
            resolve_command=lambda _name: list(spec.command),
            spawn=_spawn_in_workspace,
            store=_capture_store,
        ):
            await asyncio.wait_for(
                agent.start(
                    load_existing=spec.load_existing,
                    model_override=spec.model_override,
                    mode_override=spec.mode_override,
                ),
                timeout=spec.timeout,
            )

        return SpawnHandshake(
            observed=bool(handshake["observed"]),
            config_options=handshake["config_options"],
        )
