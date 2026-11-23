"""Tests for bundled resources, layered resource_dirs, APM auto-discovery, and unified injection."""

from __future__ import annotations

import contextlib
import os
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from acp_factory.acp.client import FactoryACPClient
from acp_factory.config import AgentConfig, CheckConfig, FactoryConfig
from acp_factory.resources import (
    get_bundled_resources_dir,
    parse_frontmatter_summary,
    resolve_resource_dirs,
    resolve_resources,
)
from acp_factory.runner.checks import build_workspace_env, run_single_check
from acp_factory.runner.guardrails import is_path_contained


def test_bundled_orchestration_resources_exist_and_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core orchestration personas, prompts, and skills resolve from package bundle without external catalog."""
    monkeypatch.delenv("FACTORY_RESOURCES_DIR", raising=False)
    monkeypatch.delenv("FACTORY_RESOURCES_DIRS", raising=False)

    bundled = get_bundled_resources_dir()
    assert bundled.is_dir()

    for agent_name in (
        "triage.agent.md",
        "architect.agent.md",
        "implementer.agent.md",
        "reviewer.agent.md",
        "security.agent.md",
    ):
        assert (bundled / "agents" / agent_name).is_file()

    for prompt_name in (
        "triage.md",
        "spec.md",
        "build.md",
        "review.md",
        "security.md",
        "retro.md",
    ):
        assert (bundled / "prompts" / prompt_name).is_file()

    core_skills = [
        "issue-triage",
        "spec-writer",
        "tdd-builder",
        "code-reviewer",
        "security-auditor",
        "retro",
    ]
    resolved = resolve_resources(
        persona="agents/triage.agent.md",
        skills=core_skills,
        workspace=tmp_path,
        resource_dirs=[],
        discover_apm=False,
    )
    assert resolved.persona_text is not None
    assert len(resolved.skills) == len(core_skills)
    assert all(sp.is_relative_to(bundled) for sp in resolved.skills)


def test_layered_resource_dirs_precedence(tmp_path: Path) -> None:
    """config_dir > workspace > resource_dirs[0] > resource_dirs[1] > bundled."""
    ws = tmp_path / "ws"
    cfg_dir = ws / ".factory"
    layer_a = tmp_path / "layer_a"
    layer_b = tmp_path / "layer_b"
    for d in (
        ws,
        cfg_dir,
        layer_a / "agents",
        layer_b / "agents",
        layer_a / "skills" / "policy-auditor",
        layer_b / "skills" / "policy-auditor",
    ):
        d.mkdir(parents=True)

    (layer_a / "agents" / "auditor.agent.md").write_text("Layer A", encoding="utf-8")
    (layer_b / "agents" / "auditor.agent.md").write_text("Layer B", encoding="utf-8")
    (layer_a / "skills" / "policy-auditor" / "SKILL.md").write_text(
        "---\nname: policy-auditor\ndescription: Org policy A\n---\n# Policy A",
        encoding="utf-8",
    )
    (layer_b / "skills" / "policy-auditor" / "SKILL.md").write_text(
        "---\nname: policy-auditor\ndescription: Org policy B\n---\n# Policy B",
        encoding="utf-8",
    )

    toml_file = cfg_dir / "config.toml"
    toml_file.write_text(
        f'resource_dirs = ["{layer_a}", "{layer_b}"]\n',
        encoding="utf-8",
    )
    cfg = FactoryConfig.from_file(toml_file)
    assert cfg.resolved_resource_dirs == [layer_a.resolve(), layer_b.resolve()]

    resolved = resolve_resources(
        persona="agents/auditor.agent.md",
        skills=["policy-auditor"],
        workspace=ws,
        config_dir=cfg_dir,
        resource_dirs=cfg.resolved_resource_dirs,
        discover_apm=False,
    )
    assert resolved.persona_text == "Layer A"
    assert resolved.skills == [
        (layer_a / "skills" / "policy-auditor" / "SKILL.md").resolve()
    ]


def test_apm_workspace_auto_discovery_and_opt_out(tmp_path: Path) -> None:
    """APM-installed skills and instructions are auto-discovered unless discover_apm=False."""
    ws = tmp_path / "repo"
    (ws / ".agents" / "skills" / "domain-ml").mkdir(parents=True)
    (ws / ".agents" / "skills" / "domain-ml" / "SKILL.md").write_text(
        "---\nname: domain-ml\ndescription: ML serving standards\n---\n# Body",
        encoding="utf-8",
    )
    (ws / "apm_modules" / "org" / "repo" / "skills" / "html2md").mkdir(parents=True)
    (
        ws / "apm_modules" / "org" / "repo" / "skills" / "html2md" / "SKILL.md"
    ).write_text(
        "---\nname: html2md\ndescription: Convert HTML to Markdown\n---\n# Body",
        encoding="utf-8",
    )
    (ws / ".github" / "instructions").mkdir(parents=True)
    (ws / ".github" / "instructions" / "python.instructions.md").write_text(
        "---\napplyTo: '**/*.py'\ndescription: Python style rules\n---\n# Rules",
        encoding="utf-8",
    )

    discovered = resolve_resources(
        skills=["tdd-builder"],
        workspace=ws,
        resource_dirs=[],
        discover_apm=True,
    )
    skill_names = [p.parent.name for p in discovered.skills]
    assert skill_names == ["tdd-builder", "domain-ml", "html2md"]
    assert [p.name for p in discovered.instructions] == ["python.instructions.md"]

    isolated = resolve_resources(
        skills=["tdd-builder"],
        workspace=ws,
        resource_dirs=[],
        discover_apm=False,
    )
    assert [p.parent.name for p in isolated.skills] == ["tdd-builder"]
    assert isolated.instructions == []


def test_client_lazy_loads_frontmatter_summaries_and_resources(tmp_path: Path) -> None:
    """FactoryACPClient injects paths + YAML frontmatter summaries without inlining full bodies."""
    ws = tmp_path / "repo"
    (ws / ".agents" / "skills" / "domain-sec").mkdir(parents=True)
    skill_file = ws / ".agents" / "skills" / "domain-sec" / "SKILL.md"
    skill_file.write_text(
        "---\nname: domain-sec\ndescription: Org security rules\n---\nSECRET_BODY_DO_NOT_INLINE",
        encoding="utf-8",
    )
    (ws / ".github" / "instructions").mkdir(parents=True)
    instr_file = ws / ".github" / "instructions" / "api.instructions.md"
    instr_file.write_text(
        "---\napplyTo: 'src/api/**'\ndescription: API conventions\n---\nINSTR_BODY_DO_NOT_INLINE",
        encoding="utf-8",
    )

    meta = parse_frontmatter_summary(skill_file)
    assert meta == {"name": "domain-sec", "description": "Org security rules"}

    client = FactoryACPClient(
        workspace=ws,
        agent_config=AgentConfig(skills=["tdd-builder"]),
        resource_dirs=[],
        discover_apm_resources=True,
    )
    decorated = client._decorate_first_turn_prompt(
        "Implement feature",
        client.explicit_skill_paths,
        client.instruction_paths,
        client.discovered_skill_paths,
    )
    assert "# Domain & Policy Instructions" in decorated
    assert "# Active Skills" in decorated
    assert "# Available Workspace Skills" in decorated
    # Frontmatter-derived values are injected (without coupling to entry format).
    assert "src/api/**" in decorated
    assert "API conventions" in decorated
    assert "domain-sec" in decorated
    assert "Org security rules" in decorated
    assert "SECRET_BODY_DO_NOT_INLINE" not in decorated
    assert "INSTR_BODY_DO_NOT_INLINE" not in decorated


async def test_send_prompt_turn_resources_omits_discovered_skills_and_templates(
    tmp_path: Path,
) -> None:
    """Turn 1 attaches instructions + explicit skills only; discovered skills and templates stay on-demand."""
    ws = tmp_path / "repo"
    discovered_skill = ws / ".agents" / "skills" / "domain-extra" / "SKILL.md"
    discovered_skill.parent.mkdir(parents=True)
    discovered_skill.write_text(
        "---\nname: domain-extra\ndescription: Extra domain rules\n---\n# Body",
        encoding="utf-8",
    )
    discovered_template = discovered_skill.parent / "templates" / "discovered.md"
    discovered_template.parent.mkdir(parents=True)
    discovered_template.write_text("# Discovered template", encoding="utf-8")

    instruction = ws / ".github" / "instructions" / "api.instructions.md"
    instruction.parent.mkdir(parents=True)
    instruction.write_text(
        "---\napplyTo: 'src/api/**'\ndescription: API conventions\n---\n# Rules",
        encoding="utf-8",
    )

    caller_resource = ws / "notes.md"
    caller_resource.write_text("# Caller notes", encoding="utf-8")

    client = FactoryACPClient(
        workspace=ws,
        agent_config=AgentConfig(skills=["tdd-builder"]),
        resource_dirs=[],
        discover_apm_resources=True,
    )
    explicit_skill = client.explicit_skill_paths[0]
    explicit_template = next(
        (explicit_skill.parent / "templates").glob("*.md")
    ).resolve()
    explicit_script = next((explicit_skill.parent / "scripts").glob("*.py")).resolve()
    assert discovered_skill in client.discovered_skill_paths

    calls: list[tuple[str, list[Path]]] = []

    async def _fake_send(prompt: str, **kwargs: object) -> str:
        passed = kwargs.get("resources")
        calls.append((prompt, list(passed) if isinstance(passed, list) else []))
        return "end_turn"

    mock_agent = MagicMock()
    mock_agent.session_id = "sess-resources"
    mock_agent.start = AsyncMock()
    mock_agent.stop = AsyncMock()
    mock_agent.send_prompt = AsyncMock(side_effect=_fake_send)

    with (
        patch("acp_factory.acp.client.ACPAgent", return_value=mock_agent),
        patch(
            "acp_factory.acp.client.resolve_factory_agent_command",
            return_value=["python3", "-c", "pass"],
        ),
    ):
        await client.start()
        await client.send_prompt("first turn", resources=[caller_resource])
        await client.send_prompt("second turn")

    first_prompt, first_resources = calls[0]
    _, second_resources = calls[1]

    assert caller_resource in first_resources
    assert explicit_skill in first_resources
    assert set(client.instruction_paths) <= set(first_resources)
    assert discovered_skill not in first_resources
    assert not [
        p for p in first_resources if p.parent.name == "templates" and p.suffix == ".md"
    ]

    assert "# Active Skills" in first_prompt
    assert "# Available Workspace Skills" in first_prompt
    assert str(explicit_template) in first_prompt
    assert str(explicit_script) in first_prompt
    assert str(discovered_template.resolve()) in first_prompt

    assert second_resources == []


async def test_prompt_check_policy_auditor_receives_apm_domain_resources(
    tmp_path: Path,
) -> None:
    """Dedicated prompt checks (e.g. policy-auditor) inject both check skills and APM domain resources."""
    ws = tmp_path / "repo"
    (ws / ".agents" / "skills" / "domain-iac").mkdir(parents=True)
    domain_skill = (ws / ".agents" / "skills" / "domain-iac" / "SKILL.md").resolve()
    domain_skill.write_text(
        "---\nname: domain-iac\ndescription: Terraform policy\n---\n# IaC",
        encoding="utf-8",
    )
    (ws / ".github" / "instructions").mkdir(parents=True)
    domain_instr = (ws / ".github" / "instructions" / "iac.instructions.md").resolve()
    domain_instr.write_text(
        "---\napplyTo: '**/*.tf'\ndescription: IaC rules\n---\n# Rules",
        encoding="utf-8",
    )

    check = CheckConfig(
        name="policy-auditor",
        prompt="Verify domain standards.",
        agent="opencode",
        persona="agents/reviewer.agent.md",
        skills=["code-reviewer"],
    )

    mock_agent = MagicMock()
    mock_agent.session_id = "sess-audit"
    mock_agent.start = AsyncMock()
    mock_agent.stop = AsyncMock()
    mock_agent.send_prompt = AsyncMock(return_value="end_turn")

    captured_prompts: list[str] = []

    async def _fake_send(prompt: str, **_kwargs: object) -> str:
        captured_prompts.append(prompt)
        return "end_turn"

    mock_agent.send_prompt.side_effect = _fake_send

    with (
        patch("acp_factory.acp.client.ACPAgent", return_value=mock_agent),
        patch(
            "acp_factory.acp.client.resolve_factory_agent_command",
            return_value=["python3", "-c", "pass"],
        ),
        patch(
            "acp_factory.acp.client.TurnEventTracker.end_turn",
            return_value=MagicMock(response_text="All good.\nVERDICT: PASS"),
        ),
    ):
        res = await run_single_check(check, ws)

    assert res.passed is True
    assert len(captured_prompts) == 1
    sent = captured_prompts[0]
    assert "domain-iac" in sent
    assert "iac.instructions.md" in sent
    assert "code-reviewer" in sent


def test_fleet_resolve_repo_stage_appends_repo_skills_and_instructions(
    tmp_path: Path,
) -> None:
    """Fleet resolve_repo_stage preserves central orchestration skills while appending repo skills/instructions."""
    central = FactoryConfig.model_validate(
        {
            "stages": {
                "build": {
                    "skills": ["tdd-builder"],
                    "prompt": "prompts/build.md",
                }
            }
        }
    )
    repo = tmp_path / "target-repo"
    (repo / ".factory").mkdir(parents=True)
    (repo / ".factory" / "config.toml").write_text(
        """
instructions = ["docs/arch.md"]

[stages.build]
skills = ["domain-ml"]
instructions = [".github/instructions/ml.instructions.md"]
""",
        encoding="utf-8",
    )
    resolved = central.resolve_repo_stage("build", repo)
    assert resolved.agent.skills == ["tdd-builder", "domain-ml"]
    assert resolved.agent.instructions == [
        "docs/arch.md",
        ".github/instructions/ml.instructions.md",
    ]


def test_guardrails_allows_multiple_resource_dirs_and_bundled_readonly(
    tmp_path: Path,
) -> None:
    """Guardrails allow read-only access to all resource_dirs and bundled resources, blocking writes."""
    ws = tmp_path / "ws"
    ws.mkdir()
    res_a = tmp_path / "res_a"
    res_b = tmp_path / "res_b"
    (res_a / "skills").mkdir(parents=True)
    (res_b / "instructions").mkdir(parents=True)
    file_a = res_a / "skills" / "a.md"
    file_b = res_b / "instructions" / "b.md"
    file_a.write_text("A", encoding="utf-8")
    file_b.write_text("B", encoding="utf-8")

    bundled_skill = get_bundled_resources_dir() / "skills" / "issue-triage" / "SKILL.md"

    for target in (file_a, file_b, bundled_skill):
        ok_read, _ = is_path_contained(
            target, ws, is_read=True, resource_dirs=[res_a, res_b]
        )
        assert ok_read is True
        ok_write, _ = is_path_contained(
            target, ws, is_read=False, resource_dirs=[res_a, res_b]
        )
        assert ok_write is False


def test_build_workspace_env_uses_bundled_skills_dir_when_no_external_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_workspace_env falls back to bundled resources/skills when no external dir is configured."""
    monkeypatch.delenv("FACTORY_RESOURCES_DIR", raising=False)
    monkeypatch.delenv("FACTORY_RESOURCES_DIRS", raising=False)
    monkeypatch.setenv("FACTORY_SKILLS_DIR", "/stale/parent/skills")
    monkeypatch.setenv("FACTORY_BUNDLED_SKILLS_DIR", "/stale/parent/bundled-skills")
    env = build_workspace_env(tmp_path)
    expected_skills = str((get_bundled_resources_dir() / "skills").resolve())
    assert env["FACTORY_SKILLS_DIR"] == expected_skills
    assert env["FACTORY_BUNDLED_SKILLS_DIR"] == expected_skills
    assert len(resolve_resource_dirs(include_bundled=True)) >= 1


def test_apm_lock_deployed_files_target_agnostic_and_deduplicated(
    tmp_path: Path,
) -> None:
    """apm.lock.yaml deployed_files are discovered regardless of target dir and deduplicated vs apm_modules."""
    ws = tmp_path / "repo"
    (ws / ".claude" / "skills" / "domain-skill").mkdir(parents=True)
    deployed_skill = (ws / ".claude" / "skills" / "domain-skill" / "SKILL.md").resolve()
    deployed_skill.write_text("# Deployed Claude Skill", encoding="utf-8")

    (ws / ".cursor" / "instructions").mkdir(parents=True)
    deployed_instr = (
        ws / ".cursor" / "instructions" / "style.instructions.md"
    ).resolve()
    deployed_instr.write_text("# Deployed Cursor Instruction", encoding="utf-8")

    # Duplicate raw copies in apm_modules/ should be ignored in favor of deployed_files
    (ws / "apm_modules" / "org" / "pkg" / "skills" / "domain-skill").mkdir(parents=True)
    (
        ws / "apm_modules" / "org" / "pkg" / "skills" / "domain-skill" / "SKILL.md"
    ).write_text("# Raw Skill", encoding="utf-8")
    (ws / "apm_modules" / "org" / "pkg" / "instructions").mkdir(parents=True)
    (
        ws / "apm_modules" / "org" / "pkg" / "instructions" / "style.instructions.md"
    ).write_text("# Raw Instruction", encoding="utf-8")

    # Arbitrary workspace paths outside known APM directories must be ignored
    (ws / "src" / "evil").mkdir(parents=True)
    (ws / "src" / "evil" / "SKILL.md").write_text("# Evil", encoding="utf-8")

    (ws / "apm.lock.yaml").write_text(
        """lockfile_version: '1'
dependencies:
  - repo_url: org/pkg
    deployed_files:
      - .claude/skills/domain-skill/SKILL.md
      - .cursor/instructions/style.instructions.md
      - src/evil/SKILL.md
""",
        encoding="utf-8",
    )

    resolved = resolve_resources(
        workspace=ws,
        resource_dirs=[],
        discover_apm=True,
    )
    assert resolved.skills == [deployed_skill]
    assert resolved.instructions == [deployed_instr]


def test_guardrails_main_parses_pathsep_factory_resources_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guardrails hook parses os.pathsep-delimited FACTORY_RESOURCES_DIRS identically to resolver."""
    from acp_factory.runner.guardrails import main as guardrails_main

    ws = tmp_path / "ws"
    ws.mkdir()
    res_a = tmp_path / "res_a"
    res_b = tmp_path / "res_b"
    res_a.mkdir()
    res_b.mkdir()
    target_b = res_b / "policy.md"
    target_b.write_text("Policy", encoding="utf-8")

    monkeypatch.setenv("ACPTERM_PERMISSION_KIND", "read")
    monkeypatch.setenv("ACPTERM_PERMISSION_PATH", str(target_b))
    monkeypatch.setenv("ACPTERM_CWD", str(ws))
    monkeypatch.setenv("FACTORY_RESOURCES_DIRS", f"{res_a}{os.pathsep}{res_b}")

    with pytest.raises(SystemExit) as exc_info:
        guardrails_main([])
    assert exc_info.value.code == 0


def _thought_level_option(
    *,
    option_id: str = "thought_level",
    category: str | None = "thought_level",
    values: tuple[str, ...] = ("low", "medium", "high"),
    grouped: bool = False,
) -> SimpleNamespace:
    """Build a stand-in for an ACP SessionConfigOptionSelect (flat or grouped)."""
    if grouped:
        items = [
            SimpleNamespace(
                group="effort",
                name="Effort",
                options=[SimpleNamespace(value=value, name=value) for value in values],
            )
        ]
    else:
        items = [SimpleNamespace(value=value, name=value) for value in values]
    return SimpleNamespace(
        id=option_id,
        name="Thought level",
        type="select",
        category=category,
        options=items,
    )


async def _start_client_with_handshake(
    tmp_path: Path,
    thought_level: str | None,
    *,
    options: list[Any] | None,
    cached_options: list[object] | None = None,
    conn: SimpleNamespace | None = None,
    agent_setter: object | None = None,
    console_buf: StringIO | None = None,
    observe_handshake: bool = True,
) -> tuple[FactoryACPClient, MagicMock, SimpleNamespace, MagicMock, MagicMock]:
    """Start a client whose mocked agent reports ``options`` in its session handshake."""
    from acpterm import agent_cache as acpterm_agent_cache

    client = FactoryACPClient(
        workspace=tmp_path,
        agent_config=AgentConfig(name="opencode", thought_level=thought_level),
        resource_dirs=[],
        discover_apm_resources=False,
        silent=console_buf is None,
    )
    fake_conn = conn or SimpleNamespace(set_config_option=AsyncMock())
    agent = MagicMock()
    agent.session_id = "sess-1"
    agent._conn = fake_conn
    agent.stop = AsyncMock()
    if agent_setter is not None:
        agent.set_config_option = agent_setter

    async def _fake_start(**_kwargs: object) -> None:
        # Mirror acpterm's post-handshake cache write so the client can capture it.
        if observe_handshake:
            acpterm_agent_cache.store("opencode", config_options=options)

    agent.start = AsyncMock(side_effect=_fake_start)

    cached_mock = MagicMock(return_value=cached_options)
    store_mock = MagicMock()
    patches = [
        patch("acp_factory.acp.client.ACPAgent", return_value=agent),
        patch(
            "acp_factory.acp.client.resolve_factory_agent_command",
            return_value=["python3", "-c", "pass"],
        ),
        patch("acpterm.agent_cache.store", new=store_mock),
        patch(
            "acp_factory.acp.client.acpterm_agent_cache.get_config_options",
            cached_mock,
        ),
    ]
    if console_buf is not None:
        from rich.console import Console

        patches.append(
            patch(
                "acp_factory.acp.client.console",
                Console(file=console_buf, width=200, highlight=False),
            )
        )
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await client.start()

    return client, agent, fake_conn, cached_mock, store_mock


def _console_text(buf: StringIO) -> str:
    """Normalize Rich's terminal-width-dependent line wrapping before assertions."""
    return " ".join(buf.getvalue().split())


async def test_thought_level_applied_from_live_handshake(tmp_path: Path) -> None:
    """AC-3: the live handshake's category="thought_level" option is preferred by id."""
    buf = StringIO()
    category_option = _thought_level_option(option_id="reasoning")
    id_only_option = _thought_level_option(option_id="thought_level", category=None)

    client, _, conn, _, _ = await _start_client_with_handshake(
        tmp_path, "high", options=[category_option, id_only_option], console_buf=buf
    )

    conn.set_config_option.assert_awaited_once_with(
        config_id="reasoning", session_id="sess-1", value="high"
    )
    assert client.applied_thought_level == "high"
    assert "[thought_level]" in _console_text(buf)
    assert "high" in _console_text(buf)


@pytest.mark.parametrize("grouped", [False, True], ids=["flat", "grouped"])
async def test_thought_level_matches_option_id_and_grouped_values(
    tmp_path: Path, grouped: bool
) -> None:
    """AC-3: id matches work for flat and grouped select option lists."""
    option = _thought_level_option(
        option_id="thought_level",
        category=None,
        values=("low", "medium"),
        grouped=grouped,
    )

    client, _, conn, _, _ = await _start_client_with_handshake(
        tmp_path, "medium", options=[option]
    )

    conn.set_config_option.assert_awaited_once_with(
        config_id="thought_level", session_id="sess-1", value="medium"
    )
    assert client.applied_thought_level == "medium"


async def test_thought_level_boolean_option_coercion(tmp_path: Path) -> None:
    """AC-3: boolean thought_level options receive a real boolean value."""
    option = SimpleNamespace(
        id="thinking", name="Thinking", type="boolean", category="thought_level"
    )

    client, _, conn, _, _ = await _start_client_with_handshake(
        tmp_path, "ON", options=[option]
    )

    conn.set_config_option.assert_awaited_once_with(
        config_id="thinking", session_id="sess-1", value=True
    )
    assert client.applied_thought_level == "True"


async def test_thought_level_prefers_agent_coroutine_setter(tmp_path: Path) -> None:
    """AC-3: a future ACPAgent.set_config_option coroutine is used when available."""
    setter = AsyncMock()
    option = _thought_level_option()

    await _start_client_with_handshake(
        tmp_path, "low", options=[option], agent_setter=setter
    )

    setter.assert_awaited_once_with(config_id="thought_level", value="low")


async def test_thought_level_unset_is_a_noop(tmp_path: Path) -> None:
    """AC-3: no thought_level means no cache lookups and no RPC calls."""
    option = _thought_level_option()

    client, _, conn, cached_mock, _ = await _start_client_with_handshake(
        tmp_path, None, options=[option], cached_options=[option]
    )

    conn.set_config_option.assert_not_called()
    cached_mock.assert_not_called()
    assert client.applied_thought_level is None


async def test_thought_level_unsupported_agent_warns_and_continues(
    tmp_path: Path,
) -> None:
    """AC-4: an agent without the option produces a warning instead of a crash."""
    buf = StringIO()
    model_option = SimpleNamespace(
        id="model", name="Model", type="select", category="model", options=[]
    )

    client, _, conn, _, _ = await _start_client_with_handshake(
        tmp_path, "high", options=[model_option], console_buf=buf
    )

    conn.set_config_option.assert_not_called()
    assert "does not advertise a thought_level config option" in _console_text(buf)
    assert client.applied_thought_level is None


async def test_thought_level_ignores_stale_cache_when_live_options_empty(
    tmp_path: Path,
) -> None:
    """AC-4: an empty live handshake never falls back to a stale on-disk cache."""
    buf = StringIO()
    cached_option = _thought_level_option()

    client, _, conn, cached_mock, _ = await _start_client_with_handshake(
        tmp_path,
        "high",
        options=None,
        cached_options=[cached_option],
        console_buf=buf,
    )

    cached_mock.assert_not_called()
    conn.set_config_option.assert_not_called()
    assert "does not advertise a thought_level config option" in _console_text(buf)
    assert client.applied_thought_level is None


@pytest.mark.parametrize(
    ("level", "expected_detail"),
    [
        ("hight", "Did you mean 'high'?"),
        ("maximum", "Available values: low, high."),
    ],
)
async def test_thought_level_invalid_value_warns_without_rpc(
    tmp_path: Path, level: str, expected_detail: str
) -> None:
    """AC-4: values outside the advertised options warn and skip the RPC call."""
    buf = StringIO()
    option = _thought_level_option(values=("low", "high"))

    client, _, conn, _, _ = await _start_client_with_handshake(
        tmp_path, level, options=[option], console_buf=buf
    )

    conn.set_config_option.assert_not_called()
    output = _console_text(buf)
    assert expected_detail in output
    assert client.applied_thought_level is None


async def test_thought_level_invalid_boolean_warns_without_rpc(tmp_path: Path) -> None:
    """AC-4: unrecognized boolean strings warn instead of coercing silently."""
    buf = StringIO()
    option = SimpleNamespace(
        id="thinking", name="Thinking", type="boolean", category="thought_level"
    )

    await _start_client_with_handshake(
        tmp_path, "sometimes", options=[option], console_buf=buf
    )

    assert "not a valid boolean" in _console_text(buf)


@pytest.mark.parametrize("error", [RuntimeError("boom"), TimeoutError("slow")])
async def test_thought_level_rpc_errors_are_non_fatal(
    tmp_path: Path, error: Exception
) -> None:
    """AC-4: RPC errors (including timeouts) are warned about and swallowed."""
    buf = StringIO()
    conn = SimpleNamespace(set_config_option=AsyncMock(side_effect=error))
    option = _thought_level_option()

    client, _, _, _, _ = await _start_client_with_handshake(
        tmp_path, "high", options=[option], conn=conn, console_buf=buf
    )

    assert "Failed to set thought_level" in _console_text(buf)
    assert client.applied_thought_level is None
    assert client.session_id == "sess-1"


async def test_thought_level_unsupported_connection_warns(tmp_path: Path) -> None:
    """AC-4: agents without a config-option RPC surface degrade gracefully."""
    buf = StringIO()
    option = _thought_level_option()

    await _start_client_with_handshake(
        tmp_path,
        "high",
        options=[option],
        conn=SimpleNamespace(),
        console_buf=buf,
    )

    assert "does not support set_config_option" in _console_text(buf)


async def test_thought_level_falls_back_to_cached_options(tmp_path: Path) -> None:
    """AC-3: without a live observation, acpterm's cached options are used."""
    option = _thought_level_option(values=("low", "high"))

    client, _, conn, cached_mock, _ = await _start_client_with_handshake(
        tmp_path,
        "low",
        options=None,
        cached_options=[option],
        observe_handshake=False,
    )

    cached_mock.assert_called_once_with("opencode")
    conn.set_config_option.assert_awaited_once_with(
        config_id="thought_level", session_id="sess-1", value="low"
    )
    assert client.applied_thought_level == "low"
