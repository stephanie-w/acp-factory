"""Tests for the single acpterm process-spawn seam."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import acpterm.acp_agent as acpterm_agent_mod
import pytest
from acpterm import agent_cache as acpterm_agent_cache

from acp_factory.acp.spawn import AcptermProcessSpawner, SpawnSpec
from acp_factory.config import AgentConfig


class _FakeAcptermConfig:
    """Stand-in for acpterm's on-disk agent config loader."""

    agents = {
        "opencode": ["opencode", "acp"],
        "kiro": ["kiro-cli", "acp"],
    }

    def get_agent_command(self, name: str) -> list[str]:
        return list(self.agents[name])


@pytest.mark.parametrize(
    ("agent_cfg", "expected"),
    [
        (AgentConfig(name="opencode"), ["opencode", "acp"]),
        (AgentConfig(command=["opencode"]), ["opencode", "acp"]),
        (AgentConfig(command=["kiro"]), ["kiro-cli", "acp"]),
        (
            AgentConfig(command=["definitely-not-a-real-agent", "--flag"]),
            ["definitely-not-a-real-agent", "--flag"],
        ),
        (AgentConfig(command=[]), ["opencode", "acp"]),
    ],
    ids=["config-name", "config-alias", "single-alias", "explicit", "empty-list"],
)
def test_resolve_command_prefers_explicit_then_acpterm_config(
    agent_cfg: AgentConfig,
    expected: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "acp_factory.acp.spawn.AcptermConfig.load",
        lambda: _FakeAcptermConfig(),
    )

    assert AcptermProcessSpawner().resolve_command(agent_cfg) == expected


@pytest.mark.parametrize(
    "command",
    [[], [""], ["fake-agent", ""]],
    ids=["empty", "blank-token", "blank-trailing-token"],
)
async def test_start_rejects_unusable_commands(
    tmp_path: Path, command: list[str]
) -> None:
    spawner = AcptermProcessSpawner()
    spec = SpawnSpec(command=command, workspace=tmp_path, timeout=1)

    with pytest.raises(ValueError, match="command"):
        await spawner.start(cast(Any, object()), spec)


def test_build_env_applies_workspace_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTORY_PROBE_TOKEN", "host-secret")
    monkeypatch.setenv("FACTORY_PROBE_STRIP", "leak")
    monkeypatch.setenv("PYTHONPATH", "/host/python")

    spec = SpawnSpec(
        command=["fake-agent"],
        workspace=tmp_path,
        extra_env={"FACTORY_PROBE_EXTRA": "1"},
        keep_env=["FACTORY_PROBE_TOKEN", "FACTORY_PROBE_STRIP"],
        strip_env=["FACTORY_PROBE_STRIP"],
    )

    env = AcptermProcessSpawner().build_env(spec)

    # An allowlisted credential name survives, but an explicit strip always wins.
    assert env["FACTORY_PROBE_TOKEN"] == "host-secret"
    assert "FACTORY_PROBE_STRIP" not in env
    assert env["FACTORY_PROBE_EXTRA"] == "1"
    assert env["NO_COLOR"] == "1"
    assert "PYTHONPATH" not in env


async def test_start_installs_seam_and_restores_acpterm_globals(
    tmp_path: Path,
) -> None:
    originals = (
        acpterm_agent_mod.resolve_agent_command,
        acpterm_agent_mod.spawn_stdio_transport,
        acpterm_agent_cache.store,
    )
    observed: dict[str, Any] = {}
    option = SimpleNamespace(
        id="thought_level",
        name="Thought level",
        type="select",
        current_value="high",
        options=[],
    )

    class ProbeAgent:
        async def start(self, **kwargs: Any) -> None:
            observed["kwargs"] = kwargs
            observed["command"] = acpterm_agent_mod.resolve_agent_command("ignored")
            observed["spawn"] = acpterm_agent_mod.spawn_stdio_transport
            observed["store"] = acpterm_agent_cache.store
            acpterm_agent_cache.store("ignored", config_options=cast(Any, [option]))

    spec = SpawnSpec(
        command=["fake-agent", "--acp"],
        workspace=tmp_path,
        load_existing=True,
        model_override="review-model",
        mode_override="plan",
        timeout=5,
    )

    handshake = await AcptermProcessSpawner().start(cast(Any, ProbeAgent()), spec)

    assert observed["kwargs"] == {
        "load_existing": True,
        "model_override": "review-model",
        "mode_override": "plan",
    }
    assert observed["command"] == ["fake-agent", "--acp"]
    assert observed["spawn"] is not originals[1]
    assert observed["store"] is not originals[2]
    assert handshake.observed is True
    assert handshake.config_options == [option]
    assert (
        acpterm_agent_mod.resolve_agent_command,
        acpterm_agent_mod.spawn_stdio_transport,
        acpterm_agent_cache.store,
    ) == originals


async def test_start_restores_acpterm_globals_after_failure(tmp_path: Path) -> None:
    originals = (
        acpterm_agent_mod.resolve_agent_command,
        acpterm_agent_mod.spawn_stdio_transport,
        acpterm_agent_cache.store,
    )

    class FailingAgent:
        async def start(self, **_kwargs: Any) -> None:
            raise RuntimeError("spawn failed")

    spec = SpawnSpec(command=["fake-agent"], workspace=tmp_path, timeout=5)

    with pytest.raises(RuntimeError, match="spawn failed"):
        await AcptermProcessSpawner().start(cast(Any, FailingAgent()), spec)

    assert (
        acpterm_agent_mod.resolve_agent_command,
        acpterm_agent_mod.spawn_stdio_transport,
        acpterm_agent_cache.store,
    ) == originals


async def test_spawn_override_uses_workspace_env_and_cwd(tmp_path: Path) -> None:
    output: list[str] = []

    class ProbeAgent:
        async def start(self, **_kwargs: Any) -> None:
            context = acpterm_agent_mod.spawn_stdio_transport(
                sys.executable,
                "-c",
                "import os; print(os.getcwd()); print(os.environ.get('FACTORY_PROBE', ''))",
            )
            async with context as (reader, _writer, _process):
                data = await asyncio.wait_for(reader.read(), timeout=10)
                output.append(data.decode())

    spec = SpawnSpec(
        command=["unused"],
        workspace=tmp_path,
        extra_env={"FACTORY_PROBE": "workspace-value"},
        timeout=10,
    )

    await AcptermProcessSpawner().start(cast(Any, ProbeAgent()), spec)

    lines = output[0].splitlines()
    assert lines[0] == str(tmp_path.resolve())
    assert lines[1] == "workspace-value"
