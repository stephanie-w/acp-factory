"""Unit tests for acp_factory.config and per-stage persona/skill resolution."""

from __future__ import annotations

import tomllib
from pathlib import Path

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
        if stage_name != "retro":
            assert "Template:" in decorated
            assert "Validation Script:" in decorated

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

    # Valid values (including a zero cost cap) are accepted.
    valid = BudgetConfig(max_tokens_per_run=100_000, max_cost_usd=2.0, max_turns=3)
    assert valid.max_tokens_per_run == 100_000
    assert valid.max_cost_usd == 2.0
    assert valid.max_turns == 3
    assert BudgetConfig(max_cost_usd=0.0).max_cost_usd == 0.0

    # Zero/negative tokens or turns are rejected.
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
