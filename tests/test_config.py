"""Unit tests for acp_factory.config and per-stage persona/skill resolution."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from acp_factory.acp.client import (
    FactoryACPClient,
    resolve_persona_text,
    resolve_skill_paths,
)
from acp_factory.config import BudgetConfig, CheckConfig, FactoryConfig, StageConfig


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


def test_scaffolded_stage_prose_comes_from_bundled_resources(
    tmp_path: Path,
) -> None:
    """Scaffolded personas and prompts are the bundled catalog content, not inline copies."""
    from typer.testing import CliRunner

    from acp_factory import cli
    from acp_factory.resources import load_bundled_text

    runner = CliRunner()
    result = runner.invoke(cli.app, ["init", "--multi-stage", "--path", str(tmp_path)])
    assert result.exit_code == 0

    expected_files = {
        ".factory/personas/triage.md": "agents/triage.agent.md",
        ".factory/personas/architect.md": "agents/architect.agent.md",
        ".factory/personas/implementer.md": "agents/implementer.agent.md",
        ".factory/personas/reviewer.md": "agents/reviewer.agent.md",
        ".factory/personas/security.md": "agents/security.agent.md",
        ".factory/prompts/TRIAGE.md": "prompts/triage.md",
        ".factory/prompts/SPEC.md": "prompts/spec.md",
        ".factory/prompts/BUILD.md": "prompts/build.md",
        ".factory/prompts/REVIEW.md": "prompts/review.md",
        ".factory/prompts/SECURITY.md": "prompts/security.md",
        ".factory/prompts/RETRO.md": "prompts/retro.md",
    }
    for scaffolded, bundled_source in expected_files.items():
        content = (tmp_path / scaffolded).read_text(encoding="utf-8")
        assert content == load_bundled_text(bundled_source)
        assert content.endswith("\n")
        assert not content.endswith("\n\n")

    assert not hasattr(cli, "DEFAULT_PERSONAS")
    assert not hasattr(cli, "DEFAULT_STAGE_PROMPTS")


def test_scaffolded_config_files_come_from_bundled_templates(tmp_path: Path) -> None:
    """Starter configs are bundled template files, not copies embedded in cli.py."""
    from typer.testing import CliRunner

    from acp_factory import cli
    from acp_factory.resources import load_bundled_text

    runner = CliRunner()

    single_file = tmp_path / "factory.yaml"
    result = runner.invoke(cli.app, ["init", "--path", str(single_file)])
    assert result.exit_code == 0
    assert single_file.read_text(encoding="utf-8") == load_bundled_text(
        "templates/factory.yaml"
    )

    result = runner.invoke(cli.app, ["init", "--multi-stage", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / ".factory" / "config.toml").read_text(
        encoding="utf-8"
    ) == load_bundled_text("templates/config.toml")
    assert (tmp_path / "apm.yml").read_text(encoding="utf-8") == load_bundled_text(
        "templates/apm.yml"
    )

    # Pin the exact bytes emitted by `init`. The equality checks above compare
    # against the same bundled file, so only a golden hash catches unintentional
    # template content drift that would silently change `init` output.
    expected_sha256 = {
        "factory.yaml": "e8aac907dbc7b310bb9bad1562021e439bda21712c5b266686328a07da576c3f",
        ".factory/config.toml": "9c2fe87cdbac994d5523c9c6b84a4f8f954210408a7c086361c8cc9761bc539e",
        "apm.yml": "95140312db2d7109299f321c36662e74b322085d784d549b60d251b673b6f1ce",
    }
    for relative_path, expected_digest in expected_sha256.items():
        scaffolded = (tmp_path / relative_path).read_bytes()
        assert hashlib.sha256(scaffolded).hexdigest() == expected_digest

    assert not hasattr(cli, "STARTER_CONFIG_YAML")
    assert not hasattr(cli, "MULTI_STAGE_CONFIG_TOML")
    assert not hasattr(cli, "STARTER_APM_YML")


@pytest.mark.parametrize(
    ("template_name", "extra_args", "scaffolded_file"),
    [
        ("config.toml", ["--multi-stage"], ".factory/config.toml"),
        ("apm.yml", ["--multi-stage"], "apm.yml"),
        ("factory.yaml", [], "factory.yaml"),
    ],
)
def test_scaffolded_init_writes_nothing_when_template_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    template_name: str,
    extra_args: list[str],
    scaffolded_file: str,
) -> None:
    """A missing bundled template aborts before any scaffolded file is written."""
    from typer.testing import CliRunner

    from acp_factory import cli

    templates = dict(cli._SCAFFOLD_TEMPLATE_FILES)
    templates[template_name] = "templates/does-not-exist"
    monkeypatch.setattr(cli, "_SCAFFOLD_TEMPLATE_FILES", templates)

    result = CliRunner().invoke(cli.app, ["init", *extra_args, "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "Failed to load bundled scaffold resources" in result.output
    assert not (tmp_path / scaffolded_file).exists()
    assert not (tmp_path / "apm.yml").exists()


def test_scaffolded_init_writes_nothing_when_bundle_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing bundled resource aborts before any scaffolded file is written."""
    from typer.testing import CliRunner

    from acp_factory import cli

    monkeypatch.setattr(
        cli,
        "_SCAFFOLD_PERSONAS",
        {"triage.md": "agents/does-not-exist.agent.md"},
    )

    result = CliRunner().invoke(
        cli.app, ["init", "--multi-stage", "--path", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "Failed to load bundled scaffold resources" in result.output
    assert not (tmp_path / ".factory").exists()
    assert not (tmp_path / "apm.yml").exists()


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
        ("retro", ["retro"]),
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
        decorated = client._decorate_first_turn_prompt("Run task", client.skill_paths)
        assert "# Active Skills" in decorated
        # Non-retro skills inject their templates and validation scripts by path.
        for sp in client.skill_paths:
            templates_dir = sp.parent / "templates"
            scripts_dir = sp.parent / "scripts"
            for tmpl in (
                sorted(templates_dir.glob("*.md")) if templates_dir.is_dir() else ()
            ):
                assert str(tmpl) in decorated
            for script in (
                sorted(scripts_dir.glob("*.py")) if scripts_dir.is_dir() else ()
            ):
                assert str(script) in decorated

    assert "security" in cfg.named_checks
    assert cfg.named_checks["security"].skills == ["security-auditor"]


@pytest.mark.parametrize(
    (
        "has_repo_cfg",
        "home_cfg",
        "xdg_cfg",
        "use_explicit",
        "expected_task",
        "expected_dir_kind",
    ),
    [
        (
            False,
            'version = "1"\ntask = "personal-default-task"\n\n[agent]\nname = "opencode"\n',
            None,
            False,
            "personal-default-task",
            "home",
        ),
        (
            False,
            None,
            'version = "1"\ntask = "xdg-task"\n',
            False,
            "xdg-task",
            "xdg",
        ),
        (
            True,
            'version = "1"\ntask = "user-task"\n',
            None,
            False,
            "repo-task",
            "repo",
        ),
        (
            True,
            'version = "1"\ntask = "user-task"\n',
            None,
            True,
            "explicit-task",
            "explicit",
        ),
        (False, None, None, False, "", "workspace"),
    ],
    ids=[
        "home_fallback",
        "xdg_fallback",
        "repo_beats_user",
        "explicit_beats_all",
        "no_config_defaults",
    ],
)
def test_config_discover_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_repo_cfg: bool,
    home_cfg: str | None,
    xdg_cfg: str | None,
    use_explicit: bool,
    expected_task: str,
    expected_dir_kind: str,
) -> None:
    """FactoryConfig.discover resolves explicit > repo-local > ~/.acp-factory > XDG > defaults."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    if has_repo_cfg:
        (workspace / ".factory").mkdir()
        (workspace / ".factory" / "config.toml").write_text(
            'version = "1"\ntask = "repo-task"\n', encoding="utf-8"
        )

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    if home_cfg is not None:
        acp_dir = home / ".acp-factory"
        acp_dir.mkdir(parents=True)
        (acp_dir / "config.toml").write_text(home_cfg, encoding="utf-8")
    if xdg_cfg is not None:
        xdg_dir = home / ".config" / "acp-factory"
        xdg_dir.mkdir(parents=True)
        (xdg_dir / "config.toml").write_text(xdg_cfg, encoding="utf-8")

    explicit = None
    if use_explicit:
        explicit = tmp_path / "explicit.toml"
        explicit.write_text('version = "1"\ntask = "explicit-task"\n', encoding="utf-8")

    cfg = FactoryConfig.discover(workspace, config_path=explicit)
    assert cfg.task == expected_task
    assert cfg.agent.name == "opencode"
    expected_dir = {
        "home": home / ".acp-factory",
        "xdg": home / ".config" / "acp-factory",
        "repo": workspace / ".factory",
        "explicit": tmp_path,
        "workspace": workspace,
    }[expected_dir_kind]
    assert cfg.config_dir == expected_dir.resolve()


def test_user_level_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: persona, prompt, and skill paths resolve relative to the config file."""
    workspace = tmp_path / "bare"
    workspace.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    acp_dir = home / ".acp-factory"
    (acp_dir / "personas").mkdir(parents=True)
    (acp_dir / "skills" / "personal-skill").mkdir(parents=True)
    (acp_dir / "personas" / "architect.md").write_text(
        "Personal architect persona.", encoding="utf-8"
    )
    skill_file = acp_dir / "skills" / "personal-skill" / "SKILL.md"
    skill_file.write_text("# Personal Skill\n", encoding="utf-8")

    (acp_dir / "config.toml").write_text(
        'version = "1"\n'
        "[agent]\n"
        'name = "opencode"\n'
        'persona = "personas/architect.md"\n'
        'skills = ["personal-skill"]\n',
        encoding="utf-8",
    )

    cfg = FactoryConfig.discover(workspace)
    client = FactoryACPClient(
        workspace=workspace,
        agent_config=cfg.agent,
        config_dir=cfg.config_dir,
    )
    assert client.persona_text == "Personal architect persona."
    assert client.skill_paths == [skill_file.resolve()]


def test_malformed_user_config_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-8: an existing but malformed user-level config raises instead of falling through."""
    workspace = tmp_path / "bare"
    workspace.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    acp_dir = home / ".acp-factory"
    acp_dir.mkdir(parents=True)
    (acp_dir / "config.toml").write_text("[unclosed\n", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        FactoryConfig.discover(workspace)


def test_external_resources_dir_resolves_agents_prompts_and_skills(
    tmp_path: Path,
) -> None:
    ext_res = tmp_path / "external-resources"
    (ext_res / "agents").mkdir(parents=True)
    (ext_res / "prompts").mkdir(parents=True)
    (ext_res / "skills" / "html2md").mkdir(parents=True)

    (ext_res / "agents" / "triage.agent.md").write_text(
        "External triage agent persona.", encoding="utf-8"
    )
    (ext_res / "prompts" / "triage.md").write_text(
        "External triage stage prompt.", encoding="utf-8"
    )
    html2md_skill = ext_res / "skills" / "html2md" / "SKILL.md"
    html2md_skill.write_text("# html2md skill", encoding="utf-8")

    ws = tmp_path / "workspace"
    ws.mkdir()
    cfg_file = ws / "factory.toml"
    cfg_file.write_text(
        f"""\
version = "1"
resources_dir = "{ext_res}"

[stages.triage]
persona = "agents/triage.agent.md"
skills = ["html2md"]
prompt = "prompts/triage.md"
""",
        encoding="utf-8",
    )

    cfg = FactoryConfig.from_file(cfg_file)
    assert cfg.resolved_resources_dir == ext_res.resolve()
    stage_cfg = cfg.resolve_stage("triage", workspace=ws)
    assert "External triage stage prompt." in stage_cfg.task

    client = FactoryACPClient(
        workspace=ws,
        agent_config=stage_cfg.agent,
        config_dir=stage_cfg.config_dir,
        resources_dir=stage_cfg.resolved_resources_dir,
    )
    assert client.persona_text == "External triage agent persona."
    assert client.skill_paths == [html2md_skill.resolve()]


def test_budget_config_validation(tmp_path: Path) -> None:
    """AC-1: [budget] parses limits and rejects negative/zero (or non-finite) values."""
    # Defaults leave all caps disabled.
    default = BudgetConfig()
    assert default.max_tokens_per_run is None
    assert default.max_cost_usd is None
    assert default.max_turns is None

    # A zero cost cap is accepted (cost caps may be disabled via zero), while
    # zero/negative tokens or turns are rejected.
    assert BudgetConfig(max_cost_usd=0.0).max_cost_usd == 0.0

    for bad_tokens in (0, -1):
        with pytest.raises(ValidationError):
            BudgetConfig(max_tokens_per_run=bad_tokens)
    for bad_turns in (0, -1):
        with pytest.raises(ValidationError):
            BudgetConfig(max_turns=bad_turns)

    # Negative or non-finite cost is rejected.
    with pytest.raises(ValidationError):
        BudgetConfig(max_cost_usd=-0.01)
    with pytest.raises(ValidationError):
        BudgetConfig(max_cost_usd=float("inf"))
    with pytest.raises(ValidationError):
        BudgetConfig(max_cost_usd=float("nan"))

    # FactoryConfig exposes a `budget` section, parsed from TOML and YAML.
    toml_file = tmp_path / "factory.toml"
    toml_file.write_text(
        'version = "1"\n[budget]\nmax_tokens_per_run = 100000\nmax_cost_usd = 2.0\nmax_turns = 3\n',
        encoding="utf-8",
    )
    cfg = FactoryConfig.from_file(toml_file)
    assert cfg.budget.max_tokens_per_run == 100_000
    assert cfg.budget.max_cost_usd == 2.0
    assert cfg.budget.max_turns == 3

    yaml_file = tmp_path / "factory.yaml"
    yaml_file.write_text(
        "version: '1'\nbudget:\n  max_tokens_per_run: 50000\n  max_cost_usd: 1.25\n  max_turns: 2\n",
        encoding="utf-8",
    )
    cfg = FactoryConfig.from_file(yaml_file)
    assert cfg.budget.max_tokens_per_run == 50_000
    assert cfg.budget.max_cost_usd == 1.25
    assert cfg.budget.max_turns == 2

    # An invalid config file fails validation instead of silently disabling a cap.
    invalid = tmp_path / "invalid.toml"
    invalid.write_text('version = "1"\n[budget]\nmax_turns = 0\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        FactoryConfig.from_file(invalid)


def test_budget_cli_overrides() -> None:
    """AC-2: CLI budget flags override `[budget]` values in `with_overrides`."""
    base = FactoryConfig(
        budget=BudgetConfig(max_tokens_per_run=1000, max_cost_usd=1.0, max_turns=3)
    )
    updated = base.with_overrides(max_tokens=2000, max_cost=2.5, max_turns=5)
    assert updated.budget.max_tokens_per_run == 2000
    assert updated.budget.max_cost_usd == 2.5
    assert updated.budget.max_turns == 5

    # Partial overrides leave the other caps untouched.
    partial = base.with_overrides(max_tokens=3000)
    assert partial.budget.max_tokens_per_run == 3000
    assert partial.budget.max_cost_usd == 1.0
    assert partial.budget.max_turns == 3

    # Overrides re-run validation rather than silently disabling a cap.
    with pytest.raises(ValidationError):
        base.with_overrides(max_turns=0)
    with pytest.raises(ValidationError):
        base.with_overrides(max_cost=-1.0)


def test_config_env_and_strip_env_parsing_and_stage_resolution(tmp_path: Path) -> None:
    cfg_file = tmp_path / "factory.toml"
    cfg_file.write_text(
        """\
version = "1"
strip_env = ["GLOBAL_LEAK_VAR"]

[env]
GLOBAL_FLAG = "1"
COMMON_KEY = "global_val"

[stages.build]
strip_env = ["STAGE_LEAK_VAR"]

[stages.build.env]
STAGE_FLAG = "true"
COMMON_KEY = "stage_override"
""",
        encoding="utf-8",
    )

    cfg = FactoryConfig.from_file(cfg_file)
    assert cfg.strip_env == ["GLOBAL_LEAK_VAR"]
    assert cfg.env == {"GLOBAL_FLAG": "1", "COMMON_KEY": "global_val"}

    resolved_build = cfg.resolve_stage("build", workspace=tmp_path)
    assert resolved_build.strip_env == ["GLOBAL_LEAK_VAR", "STAGE_LEAK_VAR"]
    assert resolved_build.env == {
        "GLOBAL_FLAG": "1",
        "STAGE_FLAG": "true",
        "COMMON_KEY": "stage_override",
    }


def test_config_keep_env_parsing_and_resolution(tmp_path: Path) -> None:
    cfg_file = tmp_path / "factory.toml"
    cfg_file.write_text(
        """\
version = "1"
keep_env = ["GLOBAL_API_KEY"]

[stages.build]
keep_env = ["STAGE_TOKEN", "GLOBAL_API_KEY"]
""",
        encoding="utf-8",
    )

    cfg = FactoryConfig.from_file(cfg_file)
    assert cfg.keep_env == ["GLOBAL_API_KEY"]
    assert FactoryConfig().keep_env == []

    resolved_build = cfg.resolve_stage("build", workspace=tmp_path)
    assert resolved_build.keep_env == ["GLOBAL_API_KEY", "STAGE_TOKEN"]

    repo = tmp_path / "repo"
    (repo / ".factory").mkdir(parents=True)
    (repo / ".factory" / "config.toml").write_text(
        """\
version = "1"

[stages.build]
keep_env = ["REPO_API_KEY", "GLOBAL_API_KEY"]
""",
        encoding="utf-8",
    )
    resolved_repo = cfg.resolve_repo_stage("build", repo, workspace=tmp_path)
    assert resolved_repo.keep_env == [
        "GLOBAL_API_KEY",
        "STAGE_TOKEN",
        "REPO_API_KEY",
    ]


def test_thought_max_chars(tmp_path: Path) -> None:
    """AC-1: stage overrides win, otherwise the top-level default is inherited."""
    cfg_file = tmp_path / "factory.toml"
    cfg_file.write_text(
        """\
version = "1"
thought_max_chars = 250

[stages.build]
thought_max_chars = 0

[stages.review]
thought_max_chars = 100

[stages.spec]
model = "anthropic/claude"
""",
        encoding="utf-8",
    )

    cfg = FactoryConfig.from_file(cfg_file)
    assert cfg.thought_max_chars == 250
    assert cfg.resolve_stage("build").thought_max_chars == 0
    assert cfg.resolve_stage("review").thought_max_chars == 100
    assert cfg.resolve_stage("spec").thought_max_chars == 250

    assert FactoryConfig().thought_max_chars is None
    assert StageConfig().thought_max_chars is None

    build_cfg = cfg.resolve_stage("build")
    client = FactoryACPClient(
        workspace=tmp_path,
        agent_config=build_cfg.agent,
        config_dir=build_cfg.config_dir,
        thought_max_chars=build_cfg.thought_max_chars,
    )
    assert client.tracker.thought_max_chars == 0


def test_thought_max_chars_negative_rejected() -> None:
    """AC-2: negative values and non-integer inputs are rejected explicitly."""
    with pytest.raises(ValidationError):
        StageConfig(thought_max_chars=-1)
    with pytest.raises(ValidationError):
        FactoryConfig(thought_max_chars=-1)
    with pytest.raises(ValidationError):
        FactoryConfig(stages={"build": {"thought_max_chars": -5}})
    with pytest.raises(ValidationError):
        StageConfig.model_validate({"thought_max_chars": 1.5})
    with pytest.raises(ValidationError):
        StageConfig.model_validate({"thought_max_chars": "100"})


def test_thought_level_defaults_to_none() -> None:
    """AC-1: thought_level is optional and unset by default everywhere."""
    from acp_factory.config import AgentConfig

    assert AgentConfig().thought_level is None
    assert StageConfig().thought_level is None
    assert CheckConfig(name="review", prompt="Check it").thought_level is None
    assert FactoryConfig().agent.thought_level is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("medium", "medium"),
        ("  low  ", "low"),
        ("\tHigh\n", "High"),
        ("budget_10k", "budget_10k"),
    ],
)
def test_thought_level_strips_whitespace_and_preserves_case(
    raw: str, expected: str
) -> None:
    """AC-1: surrounding whitespace is stripped, internal casing is preserved."""
    from acp_factory.config import AgentConfig

    assert AgentConfig(thought_level=raw).thought_level == expected
    assert StageConfig(thought_level=raw).thought_level == expected
    assert (
        CheckConfig(name="review", prompt="Check it", thought_level=raw).thought_level
        == expected
    )


@pytest.mark.parametrize(
    "bad_value",
    ["", "   ", 1, 0, 1.5, True, False, ["low"], {"level": "low"}],
)
def test_thought_level_rejects_invalid_values(bad_value: Any) -> None:
    """AC-1: blank and non-string thought levels are rejected explicitly."""
    from acp_factory.config import AgentConfig

    with pytest.raises(ValidationError, match="thought_level"):
        AgentConfig(thought_level=bad_value)
    with pytest.raises(ValidationError, match="thought_level"):
        StageConfig(thought_level=bad_value)
    with pytest.raises(ValidationError, match="thought_level"):
        CheckConfig(name="review", prompt="Check it", thought_level=bad_value)
    with pytest.raises(ValidationError, match="thought_level"):
        FactoryConfig(stages={"build": {"thought_level": bad_value}})


def test_thought_level_stage_precedence_and_overrides(tmp_path: Path) -> None:
    """AC-2: stage overrides win, unset stages inherit, CLI overrides take precedence."""
    cfg_file = tmp_path / "factory.toml"
    cfg_file.write_text(
        """\
version = "1"

[agent]
name = "opencode"
thought_level = "medium"

[stages.build]
thought_level = "  low  "

[stages.review]
model = "anthropic/claude"
""",
        encoding="utf-8",
    )

    cfg = FactoryConfig.from_file(cfg_file)
    assert cfg.agent.thought_level == "medium"
    assert cfg.resolve_stage("build").agent.thought_level == "low"
    assert cfg.resolve_stage("review").agent.thought_level == "medium"

    assert cfg.with_overrides(thought_level="high").agent.thought_level == "high"
    assert cfg.with_overrides(stage="build").agent.thought_level == "low"
    assert (
        cfg.with_overrides(stage="build", thought_level="High").agent.thought_level
        == "High"
    )
    with pytest.raises(ValueError, match="thought_level"):
        cfg.with_overrides(thought_level="   ")
    with pytest.raises(ValueError, match="thought_level"):
        cfg.with_overrides(thought_level="")


def test_cli_run_exposes_and_forwards_thought_level() -> None:
    """AC-2: `run --thought-level` is documented and threaded into the config."""
    from unittest.mock import AsyncMock, patch

    from typer.testing import CliRunner

    from acp_factory.cli import app
    from acp_factory.runner.orchestrator import FactoryRunOutcome

    help_res = CliRunner().invoke(app, ["run", "--help"])
    assert help_res.exit_code == 0
    # Rich splits the double dash across style spans; strip ANSI before asserting.
    plain_help = re.sub(r"\x1b\[[0-9;]*m", "", help_res.output)
    assert "--thought-level" in plain_help

    cfg = FactoryConfig(task="task")
    with (
        patch("acp_factory.config.FactoryConfig.discover", return_value=cfg),
        patch(
            "acp_factory.cli.run_factory", new_callable=AsyncMock
        ) as mock_run_factory,
    ):
        mock_run_factory.return_value = FactoryRunOutcome(
            succeeded=True, exit_code=0, attempts_used=1, total_duration_seconds=0.0
        )
        res = CliRunner().invoke(
            app, ["run", "--offline", "--thought-level", " high ", "some task"]
        )

    assert res.exit_code == 0
    called_cfg = mock_run_factory.call_args.args[0]
    assert called_cfg.agent.thought_level == "high"


def test_prompt_resolution_prefers_local_factory_and_case_insensitive(
    tmp_path: Path,
) -> None:
    from acp_factory.config import _load_markdown_or_inline

    factory_dir = tmp_path / ".factory"
    prompts_dir = factory_dir / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "retro.md").write_text("Local Retro Prompt", encoding="utf-8")

    # 1. Exact match via config_dir
    res = _load_markdown_or_inline(
        "prompts/retro.md", config_dir=factory_dir, workspace=tmp_path
    )
    assert res == "Local Retro Prompt"

    # 2. Case-insensitive match (prompts/RETRO.md -> prompts/retro.md)
    res_upper = _load_markdown_or_inline(
        "prompts/RETRO.md", config_dir=factory_dir, workspace=tmp_path
    )
    assert res_upper == "Local Retro Prompt"

    # 3. Direct short name (retro.md -> prompts/retro.md)
    res_short = _load_markdown_or_inline(
        "retro.md", config_dir=factory_dir, workspace=tmp_path
    )
    assert res_short == "Local Retro Prompt"


# ---------------------------------------------------------------------------
# External, repo-scoped runs_dir resolution (issue #74)
# ---------------------------------------------------------------------------


def _isolate_runs_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Clear runs-related env vars and pin HOME to a temporary directory."""
    monkeypatch.delenv("FACTORY_RUNS_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "tracked.txt").write_text("content\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "init")
    return path.resolve()


def _make_worktree(repo: Path, worktree: Path) -> Path:
    _git(repo, "worktree", "add", "-q", "-b", "test-branch", str(worktree))
    return worktree.resolve()


@pytest.mark.parametrize("suffix", [".toml", ".yaml"])
def test_runs_dir_config_parsing(tmp_path: Path, suffix: str) -> None:
    """AC-1: runs_dir parses from TOML/YAML and defaults to None."""
    config_file = tmp_path / f"config{suffix}"
    body = 'version: "1"\nruns_dir: ".factory/runs"\n'
    if suffix == ".toml":
        body = 'version = "1"\nruns_dir = ".factory/runs"\n'
    config_file.write_text(body, encoding="utf-8")

    assert FactoryConfig.from_file(config_file).runs_dir == ".factory/runs"
    assert FactoryConfig().runs_dir is None


@pytest.mark.parametrize("bad_value", [1, 1.5, True, ["a"], {"a": 1}])
def test_runs_dir_rejects_non_string_values(bad_value: Any) -> None:
    """AC-1: non-string, non-None runs_dir values are rejected explicitly."""
    with pytest.raises(ValidationError):
        FactoryConfig(runs_dir=bad_value)


def test_runs_dir_precedence_env_over_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: FACTORY_RUNS_DIR wins over FactoryConfig.runs_dir."""
    from acp_factory.config import resolve_runs_dir

    _isolate_runs_env(monkeypatch, tmp_path)
    env_dir = tmp_path / "env-runs"
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(env_dir))
    config = FactoryConfig(runs_dir=str(tmp_path / "config-runs"))

    assert resolve_runs_dir(tmp_path, runs_dir=config.runs_dir) == env_dir.resolve()
    assert config.resolve_runs_dir(tmp_path) == env_dir.resolve()


@pytest.mark.parametrize("blank_env", [None, "", "   "])
def test_runs_dir_blank_env_falls_back_to_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blank_env: str | None,
) -> None:
    """AC-1: an unset/blank FACTORY_RUNS_DIR falls through to the config value."""
    from acp_factory.config import resolve_runs_dir

    _isolate_runs_env(monkeypatch, tmp_path)
    if blank_env is not None:
        monkeypatch.setenv("FACTORY_RUNS_DIR", blank_env)
    config_dir = tmp_path / "config-runs"

    result = resolve_runs_dir(tmp_path, runs_dir=str(config_dir))

    assert result == config_dir.resolve()


@pytest.mark.parametrize("blank_config", [None, "", "   "])
def test_runs_dir_blank_config_falls_back_to_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blank_config: str | None,
) -> None:
    """AC-1: a None/blank config value falls through to the XDG default."""
    from acp_factory.config import resolve_runs_dir

    home = _isolate_runs_env(monkeypatch, tmp_path)

    result = resolve_runs_dir(tmp_path, runs_dir=blank_config)

    assert result.parent == (home / ".local" / "share" / "acp-factory" / "runs")
    assert not result.exists()


def test_runs_dir_xdg_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2: absolute XDG_DATA_HOME anchors the external default path."""
    from acp_factory.config import resolve_runs_dir

    _isolate_runs_env(monkeypatch, tmp_path)
    xdg_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = resolve_runs_dir(workspace)

    assert result.parent == (xdg_home / "acp-factory" / "runs")
    assert re.fullmatch(r"workspace-[0-9a-f]{12}", result.name)
    assert not result.exists()
    assert not result.is_relative_to(workspace.resolve())


@pytest.mark.parametrize("xdg_value", [None, "", "   ", "relative/data"])
def test_runs_dir_xdg_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    xdg_value: str | None,
) -> None:
    """AC-2: unset/blank/relative XDG_DATA_HOME falls back to ~/.local/share."""
    from acp_factory.config import resolve_runs_dir

    home = _isolate_runs_env(monkeypatch, tmp_path)
    if xdg_value is not None:
        monkeypatch.setenv("XDG_DATA_HOME", xdg_value)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = resolve_runs_dir(workspace)

    assert result.parent == (home / ".local" / "share" / "acp-factory" / "runs")
    assert re.fullmatch(r"workspace-[0-9a-f]{12}", result.name)
    assert not result.exists()


def test_runs_dir_xdg_isolates_same_basename_repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: two distinct repos sharing a basename resolve to different buckets."""
    from acp_factory.config import resolve_runs_dir

    _isolate_runs_env(monkeypatch, tmp_path)
    first = tmp_path / "one" / "project"
    second = tmp_path / "two" / "project"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    first_runs = resolve_runs_dir(first)
    second_runs = resolve_runs_dir(second)

    assert first_runs != second_runs
    assert first_runs.name.startswith("project-")
    assert second_runs.name.startswith("project-")


@pytest.mark.parametrize(
    ("basename", "expected_slug"),
    [("My Repo!!", "my-repo"), ("!!!", "repo"), ("UPPER_case.name", "upper_case.name")],
)
def test_runs_dir_xdg_slug_sanitization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    basename: str,
    expected_slug: str,
) -> None:
    """AC-2: repo slugs are sanitized, with an explicit `repo` fallback."""
    from acp_factory.config import resolve_runs_dir

    _isolate_runs_env(monkeypatch, tmp_path)
    workspace = tmp_path / basename
    workspace.mkdir()

    result = resolve_runs_dir(workspace)

    assert result.name.startswith(f"{expected_slug}-")
    assert re.fullmatch(rf"{re.escape(expected_slug)}-[0-9a-f]{{12}}", result.name)


def test_runs_dir_xdg_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: repeated resolution of the same repo is deterministic."""
    from acp_factory.config import resolve_runs_dir

    _isolate_runs_env(monkeypatch, tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()

    assert resolve_runs_dir(workspace) == resolve_runs_dir(workspace)


@pytest.mark.parametrize("relative", [".factory/runs", "../shared-runs", "runs"])
def test_runs_dir_relative_worktree_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    """AC-3: relative runs_dir anchors at the main repo from every location."""
    from acp_factory.config import resolve_runs_dir

    _isolate_runs_env(monkeypatch, tmp_path)
    repo = _make_repo(tmp_path / "repo")
    worktree = _make_worktree(repo, tmp_path / "repo" / ".worktrees" / "issue-74")
    subdir = repo / "src"
    subdir.mkdir()

    expected = (repo / relative).resolve()
    assert resolve_runs_dir(repo, runs_dir=relative) == expected
    assert resolve_runs_dir(subdir, runs_dir=relative) == expected
    assert resolve_runs_dir(worktree, runs_dir=relative) == expected


@pytest.mark.parametrize("env_value", ["{abs}", "relative-runs"])
def test_runs_dir_env_worktree_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_value: str
) -> None:
    """AC-3: FACTORY_RUNS_DIR resolves identically from main repo and worktree."""
    from acp_factory.config import FactoryConfig, resolve_runs_dir

    _isolate_runs_env(monkeypatch, tmp_path)
    repo = _make_repo(tmp_path / "repo")
    worktree = _make_worktree(repo, tmp_path / "repo" / ".worktrees" / "issue-74")
    raw = str(tmp_path / "absolute-runs") if env_value == "{abs}" else env_value
    monkeypatch.setenv("FACTORY_RUNS_DIR", raw)
    expected = (
        (tmp_path / "absolute-runs").resolve()
        if env_value == "{abs}"
        else (repo / raw).resolve()
    )

    assert resolve_runs_dir(repo) == expected
    assert resolve_runs_dir(worktree) == expected
    assert FactoryConfig().resolve_runs_dir(worktree) == expected


def test_runs_dir_worktree_xdg_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: the default XDG bucket is shared by main repo, subdir, and worktree."""
    from acp_factory.config import resolve_runs_dir

    home = _isolate_runs_env(monkeypatch, tmp_path)
    repo = _make_repo(tmp_path / "repo")
    worktree = _make_worktree(repo, tmp_path / "repo" / ".worktrees" / "issue-74")
    subdir = repo / "src"
    subdir.mkdir()

    expected = resolve_runs_dir(repo)
    assert expected.parent == (home / ".local" / "share" / "acp-factory" / "runs")
    assert re.fullmatch(r"repo-[0-9a-f]{12}", expected.name)
    assert resolve_runs_dir(subdir) == expected
    assert resolve_runs_dir(worktree) == expected


def test_runs_dir_config_relative_path_from_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a repo-local config's relative runs_dir anchors at the main repo."""
    _isolate_runs_env(monkeypatch, tmp_path)
    repo = _make_repo(tmp_path / "repo")
    worktree = _make_worktree(repo, tmp_path / "repo" / ".worktrees" / "issue-74")
    (repo / ".factory").mkdir()
    (repo / ".factory" / "config.toml").write_text(
        'version = "1"\nruns_dir = ".factory/runs"\n', encoding="utf-8"
    )

    main_config = FactoryConfig.from_file(repo / ".factory" / "config.toml")
    (worktree / ".factory").mkdir()
    (worktree / ".factory" / "config.toml").write_text(
        'version = "1"\nruns_dir = ".factory/runs"\n', encoding="utf-8"
    )
    worktree_config = FactoryConfig.from_file(worktree / ".factory" / "config.toml")

    assert main_config.resolve_runs_dir(repo) == (repo / ".factory" / "runs").resolve()
    assert (
        worktree_config.resolve_runs_dir(worktree)
        == (repo / ".factory" / "runs").resolve()
    )


def test_config_import_does_not_trigger_runner_cycle() -> None:
    """AC-4: importing acp_factory.config works in a fresh interpreter."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from acp_factory.config import FactoryConfig, resolve_runs_dir; "
            "print(resolve_runs_dir(Path('.')))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
