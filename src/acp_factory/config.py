"""Configuration parser for acp-factory (YAML/TOML, multi-stage .factory/config.toml + CLI overrides)."""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, PrivateAttr, model_validator

DEFAULT_CONFIG_FILES = (
    ".factory/config.toml",
    ".factory/config.yaml",
    ".factory/config.yml",
    "factory.toml",
    "factory.yaml",
    "factory.yml",
)


class AgentConfig(BaseModel):
    """ACP worker agent configuration (global or stage-specific)."""

    name: str = Field(
        default="opencode", description="acpterm agent alias or binary name"
    )
    command: list[str] | None = Field(
        default=None,
        description="Explicit command list to spawn the ACP server (e.g. ['kiro-cli', 'acp'])",
    )
    model: str | None = Field(default=None, description="Optional LLM model override")
    mode: str | None = Field(
        default=None, description="Optional agent mode override (e.g. 'plan', 'code')"
    )
    read_only: bool = Field(
        default=False,
        description="Run the agent in read-only mode (disables file writes)",
    )
    persona: str | None = Field(
        default=None,
        description="Path to a persona Markdown file (or inline role persona text)",
    )
    skills: list[str] = Field(
        default_factory=list,
        description="Paths or APM skill names (SKILL.md) attached to the agent session",
    )
    timeout: int = Field(default=600, ge=1, description="Per-turn timeout in seconds")

    @model_validator(mode="before")
    @classmethod
    def _normalize_command(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"name": data}
        if isinstance(data, dict):
            cmd = data.get("command")
            if isinstance(cmd, str):
                data = dict(data)
                data["command"] = shlex.split(cmd)
            if data.get("command") and "name" not in data:
                data = dict(data)
                data["name"] = Path(data["command"][0]).name
        return data


class CheckConfig(BaseModel):
    """A single validation check (either a shell command or an agent review prompt)."""

    name: str = Field(description="Human-readable name for the check")
    cmd: str | None = Field(
        default=None, description="Deterministic shell command to execute"
    )
    prompt: str | None = Field(
        default=None,
        description="Review prompt (inline text or path to a .md file) evaluated by the ACP agent",
    )
    agent: str | None = Field(
        default=None,
        description="Optional agent override for review prompt checks (enables cross-agent review)",
    )
    model: str | None = Field(
        default=None,
        description="Optional model override for review prompt checks",
    )
    persona: str | None = Field(
        default=None,
        description="Optional persona Markdown path for review prompt checks",
    )
    skills: list[str] = Field(
        default_factory=list,
        description="Optional skill paths (SKILL.md) attached during review prompt checks",
    )
    timeout: int = Field(
        default=300, ge=1, description="Timeout in seconds for this check"
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_shorthand(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"name": data, "cmd": data}
        if isinstance(data, dict) and "command" in data and "cmd" not in data:
            data = dict(data)
            data["cmd"] = data.pop("command")
        return data

    @model_validator(mode="after")
    def _validate_check_type(self) -> CheckConfig:
        if not self.cmd and not self.prompt:
            raise ValueError(
                f"Check '{self.name}' must specify either 'cmd' ('command') or 'prompt'."
            )
        if self.cmd and self.prompt:
            raise ValueError(
                f"Check '{self.name}' cannot specify both 'cmd' and 'prompt'."
            )
        return self


class StageConfig(BaseModel):
    """Configuration for a named workflow stage (e.g. triage, spec, build, review, done)."""

    agent: str | None = Field(
        default=None, description="Stage-specific acpterm agent alias"
    )
    command: list[str] | None = Field(
        default=None, description="Stage-specific explicit agent command"
    )
    model: str | None = Field(
        default=None, description="Stage-specific LLM model override"
    )
    mode: str | None = Field(
        default=None, description="Stage-specific operating mode (e.g. 'plan', 'code')"
    )
    read_only: bool | None = Field(
        default=None, description="Enforce read-only mode during this stage"
    )
    persona: str | None = Field(
        default=None,
        description="Path to stage persona Markdown file (e.g. '.factory/personas/implementer.md')",
    )
    skills: list[str] = Field(
        default_factory=list,
        description="List of SKILL.md paths or APM skill identifiers for this stage",
    )
    prompt: str | None = Field(
        default=None,
        description="Path to stage Markdown prompt file (e.g. 'BUILD.md') or inline instructions",
    )
    pre: list[str] = Field(
        default_factory=list,
        description="Pre-stage setup commands (e.g. ['apm install --frozen', 'uv sync'])",
    )
    checks: list[str | CheckConfig] = Field(
        default_factory=list,
        description="Check names (referencing [checks.<name>]) or inline CheckConfig definitions",
    )
    timeout: int | None = Field(
        default=None, ge=1, description="Stage-specific per-turn timeout in seconds"
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_stage(cls, data: Any) -> Any:
        if isinstance(data, dict):
            cmd = data.get("command")
            if isinstance(cmd, str):
                data = dict(data)
                data["command"] = shlex.split(cmd)
        return data


class RetryConfig(BaseModel):
    """Retry and feedback loop configuration."""

    max_attempts: int = Field(default=3, ge=1, description="Maximum agent attempts")
    keep_worktree_on_failure: bool = Field(
        default=True, description="Preserve temporary git worktree when run fails"
    )
    keep_worktree_on_success: bool = Field(
        default=False, description="Preserve temporary git worktree when run succeeds"
    )
    max_output_lines: int = Field(
        default=150,
        ge=10,
        description="Maximum lines of check output to include in retry feedback prompts",
    )


class FactoryConfig(BaseModel):
    """Top-level configuration for an acp-factory run."""

    version: str = Field(default="1")
    agent: AgentConfig = Field(default_factory=AgentConfig)
    setup: list[str] = Field(default_factory=list)
    task: str = Field(default="")
    checks: list[CheckConfig] = Field(default_factory=list)
    named_checks: dict[str, CheckConfig] = Field(default_factory=dict)
    stages: dict[str, StageConfig] = Field(default_factory=dict)
    active_stage: str | None = Field(default=None)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    worktree: bool = Field(default=False)
    dry_run: bool = Field(
        default=False,
        description="When true, never execute mutating gh/git push commands; output files and human commands instead.",
    )

    _config_dir: Path | None = PrivateAttr(default=None)

    @property
    def config_dir(self) -> Path | None:
        return self._config_dir

    @model_validator(mode="before")
    @classmethod
    def _parse_checks_mapping(cls, data: Any) -> Any:
        """Allow `checks` in TOML/YAML to be either a list of CheckConfigs or a dict of named checks."""
        if not isinstance(data, dict):
            return data
        raw_checks = data.get("checks")
        if isinstance(raw_checks, dict):
            data = dict(data)
            named: dict[str, Any] = {}
            check_list: list[Any] = []
            for chk_name, chk_val in raw_checks.items():
                if isinstance(chk_val, str):
                    entry = {"name": chk_name, "cmd": chk_val}
                elif isinstance(chk_val, dict):
                    entry = {"name": chk_name, **chk_val}
                else:
                    entry = chk_val
                named[chk_name] = entry
                check_list.append(entry)
            data["named_checks"] = named
            # Only populate top-level checks if no stages are defined
            data["checks"] = check_list if not data.get("stages") else []
        return data

    @classmethod
    def from_file(cls, path: Path) -> FactoryConfig:
        """Load configuration from a YAML or TOML file."""
        resolved_path = path.resolve()
        if not resolved_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {resolved_path}")

        raw_text = resolved_path.read_text(encoding="utf-8")
        suffix = resolved_path.suffix.lower()
        if suffix == ".toml":
            data = tomllib.loads(raw_text)
        elif suffix in (".yaml", ".yml"):
            data = yaml.safe_load(raw_text) or {}
        else:
            raise ValueError(
                f"Unsupported config file extension '{suffix}'. Use .yaml, .yml, or .toml."
            )

        if not isinstance(data, dict):
            raise ValueError(
                f"Configuration file {resolved_path} must contain a mapping/object."
            )
        cfg = cls.model_validate(data)
        cfg._config_dir = resolved_path.parent
        return cfg

    @classmethod
    def discover(
        cls, workspace: Path, config_path: Path | None = None
    ) -> FactoryConfig:
        """Load from an explicit config path or auto-discover in the workspace."""
        if config_path is not None:
            resolved = (
                config_path if config_path.is_absolute() else (workspace / config_path)
            )
            return cls.from_file(resolved)

        for rel_path in DEFAULT_CONFIG_FILES:
            candidate = workspace / rel_path
            if candidate.exists():
                return cls.from_file(candidate)

        cfg = cls()
        cfg._config_dir = workspace.resolve()
        return cfg

    def resolve_stage(
        self, stage_name: str, workspace: Path | None = None
    ) -> FactoryConfig:
        """Apply a named stage's agent, persona, skills, prompt, pre-setup, and checks."""
        if stage_name not in self.stages:
            available = ", ".join(sorted(self.stages.keys())) or "none"
            raise ValueError(
                f"Unknown stage '{stage_name}'. Available stages: {available}"
            )

        stage = self.stages[stage_name]
        resolved = self.model_copy(deep=True)
        resolved._config_dir = self._config_dir
        resolved.active_stage = stage_name

        # 1. Merge Agent customization
        if stage.agent is not None:
            resolved.agent.name = stage.agent
            resolved.agent.command = None
        if stage.command is not None:
            resolved.agent.command = list(stage.command)
            if stage.agent is None and stage.command:
                resolved.agent.name = Path(stage.command[0]).name
        if stage.model is not None:
            resolved.agent.model = stage.model
        if stage.mode is not None:
            resolved.agent.mode = stage.mode
        if stage.read_only is not None:
            resolved.agent.read_only = stage.read_only
        if stage.persona is not None:
            resolved.agent.persona = stage.persona
        if stage.skills:
            resolved.agent.skills = list(stage.skills)
        if stage.timeout is not None:
            resolved.agent.timeout = stage.timeout

        # 2. Merge pre-setup commands
        if stage.pre:
            resolved.setup = list(stage.pre)

        # 3. Resolve stage prompt file (if specified) + combine with task
        if stage.prompt:
            prompt_text = _load_markdown_or_inline(
                stage.prompt,
                config_dir=self._config_dir,
                workspace=workspace,
            )
            if resolved.task.strip():
                resolved.task = (
                    f"{prompt_text.strip()}\n\n## Task Context\n{resolved.task.strip()}"
                )
            else:
                resolved.task = prompt_text.strip()

        # 4. Resolve stage checks (referencing named_checks or inline CheckConfig)
        resolved_checks: list[CheckConfig] = []
        for item in stage.checks:
            if isinstance(item, CheckConfig):
                chk = item.model_copy(deep=True)
            elif item in self.named_checks:
                chk = self.named_checks[item].model_copy(deep=True)
            else:
                # Treat unknown string as a shell command shorthand
                chk = CheckConfig(name=item, cmd=item)

            if chk.prompt:
                chk.prompt = _load_markdown_or_inline(
                    chk.prompt,
                    config_dir=self._config_dir,
                    workspace=workspace,
                )
            resolved_checks.append(chk)

        resolved.checks = resolved_checks
        return resolved

    def with_overrides(
        self,
        *,
        stage: str | None = None,
        workspace: Path | None = None,
        task: str | None = None,
        agent_name: str | None = None,
        agent_command: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        read_only: bool | None = None,
        persona: str | None = None,
        skills: list[str] | None = None,
        timeout: int | None = None,
        checks: list[str] | None = None,
        review_prompts: list[str] | None = None,
        setup_cmds: list[str] | None = None,
        max_attempts: int | None = None,
        worktree: bool | None = None,
        keep_worktree: bool | None = None,
        dry_run: bool | None = None,
    ) -> FactoryConfig:
        """Return a copy of the config with stage selection and CLI overrides applied."""
        updated = self.model_copy(deep=True)
        updated._config_dir = self._config_dir

        if task is not None:
            updated.task = task

        if stage is not None:
            updated = updated.resolve_stage(stage, workspace=workspace)

        if agent_name is not None:
            updated.agent.name = agent_name
            updated.agent.command = None
        if agent_command is not None:
            cmd_parts = shlex.split(agent_command)
            updated.agent.command = cmd_parts
            if agent_name is None and cmd_parts:
                updated.agent.name = Path(cmd_parts[0]).name
        if model is not None:
            updated.agent.model = model
        if mode is not None:
            updated.agent.mode = mode
        if read_only is not None:
            updated.agent.read_only = read_only
        if persona is not None:
            updated.agent.persona = persona
        if skills:
            updated.agent.skills = list(skills)
        if timeout is not None:
            updated.agent.timeout = timeout
        if setup_cmds:
            updated.setup = list(setup_cmds)
        if checks or review_prompts:
            merged_checks: list[CheckConfig] = []
            for idx, chk_item in enumerate(checks or [], start=1):
                # Support comma-separated check names (e.g. --check test,review)
                parts = [p.strip() for p in chk_item.split(",") if p.strip()]
                if len(parts) > 1 and all(p in updated.named_checks for p in parts):
                    for p in parts:
                        chk = updated.named_checks[p].model_copy(deep=True)
                        if chk.prompt:
                            chk.prompt = _load_markdown_or_inline(
                                chk.prompt,
                                config_dir=updated._config_dir,
                                workspace=workspace,
                            )
                        merged_checks.append(chk)
                elif chk_item in updated.named_checks:
                    chk = updated.named_checks[chk_item].model_copy(deep=True)
                    if chk.prompt:
                        chk.prompt = _load_markdown_or_inline(
                            chk.prompt,
                            config_dir=updated._config_dir,
                            workspace=workspace,
                        )
                    merged_checks.append(chk)
                else:
                    merged_checks.append(
                        CheckConfig(name=f"Check #{idx}: {chk_item}", cmd=chk_item)
                    )
            for idx, prompt in enumerate(review_prompts or [], start=1):
                merged_checks.append(CheckConfig(name=f"Review #{idx}", prompt=prompt))
            updated.checks = merged_checks
        if max_attempts is not None:
            updated.retry.max_attempts = max_attempts
        if worktree is not None:
            updated.worktree = worktree
        if keep_worktree is not None:
            updated.retry.keep_worktree_on_failure = keep_worktree
            updated.retry.keep_worktree_on_success = keep_worktree
        if dry_run is not None:
            updated.dry_run = dry_run

        return updated


def _load_markdown_or_inline(
    value: str,
    *,
    config_dir: Path | None = None,
    workspace: Path | None = None,
) -> str:
    """If `value` points to a Markdown/text file on disk, read it; otherwise return inline text."""
    stripped = value.strip()
    if "\n" in stripped:
        return value

    search_roots: list[Path] = []
    if config_dir is not None:
        search_roots.append(config_dir)
    if workspace is not None:
        search_roots.append(workspace)
    search_roots.append(Path.cwd())

    candidate_path = Path(stripped)
    if candidate_path.is_absolute():
        if candidate_path.is_file():
            return candidate_path.read_text(encoding="utf-8")
        if stripped.endswith(".md"):
            raise FileNotFoundError(f"Prompt/persona file not found: {candidate_path}")
        return value

    for root in search_roots:
        resolved = (root / candidate_path).resolve()
        if resolved.is_file():
            return resolved.read_text(encoding="utf-8")

    if stripped.endswith(".md"):
        raise FileNotFoundError(
            f"Prompt/persona Markdown file not found: '{stripped}' "
            f"(searched in {[str(r) for r in search_roots]})"
        )
    return value
