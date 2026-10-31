"""Unit tests for acp_factory.config and per-stage persona/skill resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from acp_factory.acp.client import (
    FactoryACPClient,
    resolve_persona_text,
    resolve_skill_paths,
)
from acp_factory.config import CheckConfig, FactoryConfig


def test_parse_yaml_config_from_goal_spec(tmp_path: Path) -> None:
    yaml_content = """\
version: "1"
agent:
  command: ["kiro-cli", "acp"]
  timeout: 600

setup:
  - "uv sync"

task: "Implement pagination for GET /items in api/routes.py and add integration tests"

checks:
  - name: "Lint & Format"
    cmd: "ruff check ."
  - name: "Unit & Integration Tests"
    cmd: "pytest tests/"
  - name: "Spec Review"
    prompt: "Verify that GET /items supports limit and offset query parameters."

retry:
  max_attempts: 3
  keep_worktree_on_failure: true
"""
    cfg_file = tmp_path / "factory.yaml"
    cfg_file.write_text(yaml_content, encoding="utf-8")

    cfg = FactoryConfig.discover(tmp_path)
    assert cfg.version == "1"
    assert cfg.agent.command == ["kiro-cli", "acp"]
    assert cfg.agent.name == "kiro-cli"
    assert cfg.agent.timeout == 600
    assert cfg.setup == ["uv sync"]
    assert "Implement pagination" in cfg.task
    assert len(cfg.checks) == 3
    assert cfg.checks[0].name == "Lint & Format"
    assert cfg.checks[0].cmd == "ruff check ."
    assert cfg.checks[2].prompt is not None
    assert cfg.retry.max_attempts == 3
    assert cfg.retry.keep_worktree_on_failure is True


def test_parse_toml_config_and_shorthands(tmp_path: Path) -> None:
    toml_content = """\
version = "1"
agent = "opencode"
task = "Fix bug in parser"
checks = ["ruff check .", "pytest"]
"""
    cfg_file = tmp_path / "factory.toml"
    cfg_file.write_text(toml_content, encoding="utf-8")

    cfg = FactoryConfig.from_file(cfg_file)
    assert cfg.agent.name == "opencode"
    assert cfg.agent.command is None
    assert len(cfg.checks) == 2
    assert cfg.checks[0].cmd == "ruff check ."
    assert cfg.checks[1].cmd == "pytest"


def test_check_config_validation_errors() -> None:
    with pytest.raises(ValueError, match="must specify either 'cmd'"):
        CheckConfig(name="Invalid Empty")

    with pytest.raises(ValueError, match="cannot specify both 'cmd' and 'prompt'"):
        CheckConfig(name="Invalid Both", cmd="pytest", prompt="Review code")


def test_cli_overrides() -> None:
    base = FactoryConfig(task="Initial task")
    updated = base.with_overrides(
        task="Overridden task",
        agent_command="copilot acp",
        model="claude-3-5-sonnet",
        read_only=True,
        persona="You are a careful engineer.",
        skills=["python-tdd"],
        checks=["pytest -q"],
        review_prompts=["Ensure docstrings exist"],
        setup_cmds=["uv sync"],
        max_attempts=5,
        worktree=True,
    )
    assert updated.task == "Overridden task"
    assert updated.agent.command == ["copilot", "acp"]
    assert updated.agent.name == "copilot"
    assert updated.agent.model == "claude-3-5-sonnet"
    assert updated.agent.read_only is True
    assert updated.agent.persona == "You are a careful engineer."
    assert updated.agent.skills == ["python-tdd"]
    assert updated.setup == ["uv sync"]
    assert len(updated.checks) == 2
    assert updated.checks[0].cmd == "pytest -q"
    assert updated.checks[1].prompt == "Ensure docstrings exist"
    assert updated.retry.max_attempts == 5
    assert updated.worktree is True


def test_multi_stage_config_and_persona_skill_resolution(tmp_path: Path) -> None:
    factory_dir = tmp_path / ".factory"
    (factory_dir / "personas").mkdir(parents=True)
    (factory_dir / "prompts").mkdir(parents=True)
    (tmp_path / "apm_modules" / "python-tdd").mkdir(parents=True)

    (factory_dir / "personas" / "implementer.md").write_text(
        "You are a TDD Python specialist.", encoding="utf-8"
    )
    (factory_dir / "prompts" / "BUILD.md").write_text(
        "Implement the requested feature with unit tests.", encoding="utf-8"
    )
    (factory_dir / "prompts" / "REVIEW.md").write_text(
        "Review diff carefully against spec.", encoding="utf-8"
    )
    skill_path = tmp_path / "apm_modules" / "python-tdd" / "SKILL.md"
    skill_path.write_text(
        "# Python TDD Skill\nAlways write tests first.", encoding="utf-8"
    )

    config_toml = factory_dir / "config.toml"
    config_toml.write_text(
        """\
version = "1"

[agent]
name = "opencode"
timeout = 600

[stages.build]
agent = "opencode"
model = "deepseek/deepseek-v4-pro"
mode = "code"
read_only = false
pre = ["apm install --frozen", "uv sync"]
persona = "personas/implementer.md"
skills = ["python-tdd"]
prompt = "prompts/BUILD.md"
checks = ["lint", "test", "review"]

[checks.lint]
command = "uv run ruff check ."

[checks.test]
command = "uv run pytest"

[checks.review]
agent = "kiro"
prompt = "prompts/REVIEW.md"
""",
        encoding="utf-8",
    )

    cfg = FactoryConfig.discover(tmp_path)
    resolved = cfg.with_overrides(
        stage="build",
        workspace=tmp_path,
        task="Issue #42: Add rate limiter",
    )

    assert resolved.active_stage == "build"
    assert resolved.agent.name == "opencode"
    assert resolved.agent.model == "deepseek/deepseek-v4-pro"
    assert resolved.agent.mode == "code"
    assert resolved.agent.read_only is False
    assert resolved.setup == ["apm install --frozen", "uv sync"]
    assert "Implement the requested feature" in resolved.task
    assert "Issue #42: Add rate limiter" in resolved.task
    assert len(resolved.checks) == 3
    assert resolved.checks[0].cmd == "uv run ruff check ."
    assert resolved.checks[2].agent == "kiro"
    assert "Review diff carefully" in (resolved.checks[2].prompt or "")

    # Verify FactoryACPClient resolves persona text and APM skill paths
    client = FactoryACPClient(
        workspace=tmp_path,
        agent_config=resolved.agent,
        config_dir=resolved.config_dir,
    )
    assert client.persona_text == "You are a TDD Python specialist."
    assert client.skill_paths == [skill_path.resolve()]

    decorated = client._decorate_first_turn_prompt("Do the task", client.skill_paths)
    assert "# Agent Persona & Role" in decorated
    assert "You are a TDD Python specialist." in decorated
    assert "# Active Skills" in decorated
    assert str(skill_path.resolve()) in decorated


def test_resolve_persona_and_missing_skill_errors(tmp_path: Path) -> None:
    assert (
        resolve_persona_text("Inline persona instructions", workspace=tmp_path)
        == "Inline persona instructions"
    )

    with pytest.raises(
        FileNotFoundError, match="Prompt/persona Markdown file not found"
    ):
        resolve_persona_text("nonexistent_persona.md", workspace=tmp_path)

    with pytest.raises(FileNotFoundError, match="Skill 'missing-skill' not found"):
        resolve_skill_paths(["missing-skill"], workspace=tmp_path)


def test_builtin_skills_and_nested_apm_resolution(tmp_path: Path) -> None:
    builtin_names = [
        "issue-triage",
        "spec-writer",
        "tdd-builder",
        "code-reviewer",
        "security-auditor",
    ]
    resolved_builtins = resolve_skill_paths(builtin_names, workspace=tmp_path)
    assert len(resolved_builtins) == 5
    for path in resolved_builtins:
        assert path.name == "SKILL.md"
        assert path.is_file()

    # Verify .agents/skills and nested apm_modules/<org>/<repo>/skills/<skill>/SKILL.md resolution
    agents_skill = tmp_path / ".agents" / "skills" / "mlops-iac" / "SKILL.md"
    agents_skill.parent.mkdir(parents=True)
    agents_skill.write_text("# MLOps IaC Skill", encoding="utf-8")

    nested_apm_skill = (
        tmp_path
        / "apm_modules"
        / "org"
        / "coding-ai-resources"
        / "skills"
        / "ml-endpoints"
        / "SKILL.md"
    )
    nested_apm_skill.parent.mkdir(parents=True)
    nested_apm_skill.write_text("# ML Endpoints Skill", encoding="utf-8")

    custom_resolved = resolve_skill_paths(
        ["mlops-iac", "ml-endpoints"], workspace=tmp_path
    )
    assert custom_resolved == [agents_skill.resolve(), nested_apm_skill.resolve()]


def test_scaffolded_multi_stage_config_resolves_all_stages_and_skills(
    tmp_path: Path,
) -> None:
    from typer.testing import CliRunner

    from acp_factory.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--multi-stage", "--path", str(tmp_path)])
    assert result.exit_code == 0

    cfg = FactoryConfig.discover(tmp_path)
    for stage_name, expected_skills in (
        ("triage", ["issue-triage"]),
        ("spec", ["spec-writer"]),
        ("build", ["tdd-builder"]),
        ("review", ["code-reviewer", "security-auditor"]),
    ):
        stage_cfg = cfg.with_overrides(
            stage=stage_name, workspace=tmp_path, task="Test issue #1"
        )
        assert stage_cfg.agent.skills == expected_skills
        client = FactoryACPClient(
            workspace=tmp_path,
            agent_config=stage_cfg.agent,
            config_dir=stage_cfg.config_dir,
        )
        assert len(client.skill_paths) == len(expected_skills)
        assert client.persona_text is not None

    assert "security" in cfg.named_checks
    assert cfg.named_checks["security"].skills == ["security-auditor"]
