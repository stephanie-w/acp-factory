"""Configuration parser for acp-factory (YAML/TOML, multi-stage .factory/config.toml + CLI overrides)."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, Field, PrivateAttr, model_validator

from .resources import (
    load_markdown_or_inline as _load_markdown_or_inline,
)
from .resources import (
    resolve_resource_dirs as resolve_resource_dirs,
)

DEFAULT_CONFIG_FILES = (
    ".factory/config.toml",
    ".factory/config.yaml",
    ".factory/config.yml",
    "factory.toml",
    "factory.yaml",
    "factory.yml",
)

USER_CONFIG_FILES = (
    Path("~/.acp-factory/config.toml"),
    Path("~/.config/acp-factory/config.toml"),
)

USER_FLEET_FILES = (
    Path("~/.acp-factory/fleet.toml"),
    Path("~/.config/acp-factory/fleet.toml"),
)


def _validate_thought_level(value: object) -> str | None:
    """Normalize a configured ACP ``thought_level`` value.

    ACP option values are agent-defined opaque strings (for example ``"low"`` or
    ``"budget_10k"``), so only the shape is validated here. Surrounding
    whitespace is stripped while internal casing is preserved; the value is
    checked against the agent's advertised options at session setup time.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("thought_level must be a non-empty string")
    return value.strip()


ThoughtLevel = Annotated[str | None, BeforeValidator(_validate_thought_level)]


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
    thought_level: ThoughtLevel = Field(
        default=None,
        description=(
            "Optional ACP reasoning/thinking level applied via session config options "
            "(values are agent-defined, e.g. 'low', 'medium', 'high')"
        ),
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
    instructions: list[str] = Field(
        default_factory=list,
        description="Explicit instruction Markdown paths attached to the agent session",
    )
    discover_apm_resources: bool | None = Field(
        default=None,
        description="Override workspace/APM auto-discovery of skills and instructions",
    )
    timeout: int = Field(default=600, ge=1, description="Per-turn timeout in seconds")

    @model_validator(mode="before")
    @classmethod
    def _normalize_command(
        cls, data: str | dict[str, Any] | AgentConfig
    ) -> dict[str, Any] | AgentConfig:
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
    thought_level: ThoughtLevel = Field(
        default=None,
        description=(
            "Optional ACP reasoning/thinking level for review prompt checks; "
            "inherits the active stage agent level when unset"
        ),
    )
    persona: str | None = Field(
        default=None,
        description="Optional persona Markdown path for review prompt checks",
    )
    skills: list[str] = Field(
        default_factory=list,
        description="Optional skill paths (SKILL.md) attached during review prompt checks",
    )
    instructions: list[str] = Field(
        default_factory=list,
        description="Optional instruction Markdown paths attached during review prompt checks",
    )
    discover_apm_resources: bool | None = Field(
        default=None,
        description="Override workspace/APM auto-discovery for this prompt check",
    )
    timeout: int = Field(
        default=300, ge=1, description="Timeout in seconds for this check"
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_shorthand(
        cls, data: str | dict[str, Any] | CheckConfig
    ) -> dict[str, Any] | CheckConfig:
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
    thought_level: ThoughtLevel = Field(
        default=None,
        description=(
            "Stage-specific ACP reasoning/thinking level; overrides [agent].thought_level"
        ),
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
    instructions: list[str] = Field(
        default_factory=list,
        description="Explicit instruction Markdown paths for this stage",
    )
    discover_apm_resources: bool | None = Field(
        default=None,
        description="Override workspace/APM auto-discovery for this stage",
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
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Stage-specific environment variables merged into checks and agent subprocesses",
    )
    strip_env: list[str] = Field(
        default_factory=list,
        description="Stage-specific additional environment variable names to strip from workspace subprocesses",
    )
    keep_env: list[str] = Field(
        default_factory=list,
        description="Stage-specific environment variable names to preserve even if they match sensitive patterns",
    )
    thought_max_chars: int | None = Field(
        default=None,
        ge=0,
        strict=True,
        description=(
            "Maximum agent thought characters in markdown/condensed transcripts. "
            "0 omits thoughts; unset preserves full markdown/history and the default summary preview."
        ),
    )
    worktree: bool | None = Field(
        default=None,
        description="Run this stage inside the persistent `.worktrees/issue-<id>` worktree.",
    )
    require_clean_worktree: bool | None = Field(
        default=None,
        description="Abort and bounce back when the issue worktree has uncommitted changes.",
    )
    delta_review: bool | None = Field(
        default=None,
        description="Snapshot HEAD before review and inject Round 2+ delta review briefs.",
    )
    inject_prior_notes_from: str | None = Field(
        default=None,
        description="Attach latest implementation/refactor notes produced by the named prior stage.",
    )
    revision_brief: bool | None = Field(
        default=None,
        description="Inject an incremental revision brief when updating an existing factory block.",
    )
    orchestrator_only: bool | None = Field(
        default=None,
        description="Execute the stage's transition action directly without spawning an ACP agent.",
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_stage(
        cls, data: dict[str, Any] | StageConfig
    ) -> dict[str, Any] | StageConfig:
        if isinstance(data, dict):
            cmd = data.get("command")
            if isinstance(cmd, str):
                data = dict(data)
                data["command"] = shlex.split(cmd)
        return data


@dataclass(frozen=True, slots=True)
class StageCapabilities:
    """Resolved execution capabilities for a workflow stage."""

    worktree: bool = False
    require_clean_worktree: bool = False
    delta_review: bool = False
    inject_prior_notes_from: str | None = None
    revision_brief: bool = False
    orchestrator_only: bool = False


_BUILTIN_STAGE_CAPABILITIES: dict[str, StageCapabilities] = {
    "spec": StageCapabilities(revision_brief=True),
    "build": StageCapabilities(worktree=True),
    "review": StageCapabilities(
        worktree=True,
        require_clean_worktree=True,
        delta_review=True,
        inject_prior_notes_from="build",
    ),
    "done": StageCapabilities(orchestrator_only=True),
    "refactor-plan": StageCapabilities(revision_brief=True),
    "refactor-apply": StageCapabilities(worktree=True),
    "refactor-review": StageCapabilities(
        worktree=True,
        require_clean_worktree=True,
        delta_review=True,
        inject_prior_notes_from="refactor-apply",
    ),
}


TransitionAction = Literal[
    "none",
    "update_issue_block",
    "publish_pr",
    "cleanup_worktree",
]
BlockMode = Literal[
    "preserve",
    "replace",
    "append_section",
    "findings",
    "clear_findings",
]


class TransitionConfig(BaseModel):
    """Declarative outcome rule mapping a stage status token to the next label and side-effect."""

    label: str = Field(
        description="Target GitHub label to apply when this transition fires."
    )
    action: TransitionAction = Field(
        default="none",
        description="Built-in orchestrator side-effect executed on transition.",
    )
    block_mode: BlockMode = Field(
        default="preserve",
        description="How to update the `<!-- factory:start/end -->` Issue Body block.",
    )
    section_title: str | None = Field(
        default=None,
        description=(
            "Optional heading prefix used with `block_mode = 'append_section'` "
            "(supports `{token}` formatting)."
        ),
    )
    replace_existing: bool = Field(
        default=False,
        description=(
            "When true with `block_mode = 'append_section'`, replace the block with "
            "the headed section instead of appending after the existing block."
        ),
    )
    max_rounds: int | None = Field(
        default=None,
        ge=1,
        description="Optional round circuit-breaker cap (used with `block_mode = 'findings'`).",
    )
    on_max_rounds_label: str = Field(
        default="factory:needs-human",
        description="Fallback label when `max_rounds` is reached.",
    )
    summary: str = Field(
        default="",
        description="Optional human-readable transition summary template (supports `{token}`).",
    )
    status: str | None = Field(
        default=None,
        description="Optional normalized status name for StageVerdict.status (defaults to lowercased token).",
    )
    record_review_report: bool = Field(
        default=False,
        description="When true, populate StageVerdict.review_report with the cleaned stage output.",
    )

    @model_validator(mode="after")
    def _validate_labels(self) -> TransitionConfig:
        if not self.label.strip():
            raise ValueError("TransitionConfig.label must be a non-empty string.")
        self.label = self.label.strip()
        if not self.on_max_rounds_label.strip():
            raise ValueError(
                "TransitionConfig.on_max_rounds_label must be a non-empty string."
            )
        self.on_max_rounds_label = self.on_max_rounds_label.strip()
        return self


class FlowConfig(BaseModel):
    """Declarative multi-stage workflow state machine over reusable stages."""

    entry: str | None = Field(
        default=None,
        description=(
            "Entry stage name for this flow. Required for custom flows; optional "
            "when partially overriding a built-in preset (inherits preset entry)."
        ),
    )
    transitions: dict[str, dict[str, TransitionConfig]] = Field(
        default_factory=dict,
        description="Per-stage mapping of uppercase status tokens (and optional 'DEFAULT') to transitions.",
    )
    open_questions_stages: list[str] = Field(
        default_factory=list,
        description=(
            "Stages where an `## Open Questions` heading escalates to `NEEDS_HUMAN` "
            "unless an explicit ready token (`SPEC_READY` or `READY`) is emitted."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_transition_tokens(
        cls, data: dict[str, Any] | FlowConfig
    ) -> dict[str, Any] | FlowConfig:
        if not isinstance(data, dict):
            return data
        raw_transitions = data.get("transitions")
        if isinstance(raw_transitions, dict):
            normalized: dict[str, Any] = {}
            for stage_name, token_map in raw_transitions.items():
                if isinstance(token_map, dict):
                    normalized[stage_name] = {
                        str(tok).strip().upper(): cfg for tok, cfg in token_map.items()
                    }
                else:
                    normalized[stage_name] = token_map
            data = dict(data)
            data["transitions"] = normalized
        return data

    @model_validator(mode="after")
    def _validate_entry(self) -> FlowConfig:
        if self.entry is not None:
            if not self.entry.strip():
                raise ValueError("FlowConfig.entry must be a non-empty stage name.")
            self.entry = self.entry.strip()
        return self


def _merge_flow_override(base: FlowConfig, override: FlowConfig) -> FlowConfig:
    """Deep-merge a partial `[flows.<builtin>]` override on top of a built-in preset."""
    merged_transitions: dict[str, dict[str, TransitionConfig]] = {
        stage: dict(tokens) for stage, tokens in base.transitions.items()
    }
    for stage, token_map in override.transitions.items():
        if stage in merged_transitions:
            merged_transitions[stage].update(token_map)
        else:
            merged_transitions[stage] = dict(token_map)
    return FlowConfig(
        entry=override.entry if override.entry is not None else base.entry,
        transitions=merged_transitions,
        open_questions_stages=(
            list(override.open_questions_stages)
            if "open_questions_stages" in override.model_fields_set
            else list(base.open_questions_stages)
        ),
    )


def _build_default_pr_flow() -> FlowConfig:
    """Return the built-in 5-stage PR workflow matching the legacy hardcoded state machine 1:1."""
    triage_escalate = TransitionConfig(
        label="factory:needs-human",
        action="update_issue_block",
        block_mode="append_section",
        section_title="## Triage Report (`{token}`)",
        replace_existing=True,
        summary="Triage requires human attention ({token})",
    )
    spec_escalate = TransitionConfig(
        label="factory:needs-human",
        action="update_issue_block",
        block_mode="replace",
        status="needs_human",
        summary="Spec has open questions -> waiting for human answers in comments",
    )
    review_accept = TransitionConfig(
        label="factory:pr-ready",
        action="publish_pr",
        block_mode="clear_findings",
        status="accepted",
        summary="Review accepted -> PR ready for Human Gate 2",
        record_review_report=True,
    )
    review_escalate = TransitionConfig(
        label="factory:needs-human",
        action="update_issue_block",
        block_mode="append_section",
        section_title="## Review Escalation",
        replace_existing=False,
        status="needs_human",
        summary="Review requires human decision",
        record_review_report=True,
    )
    review_revise = TransitionConfig(
        label="factory:build",
        action="update_issue_block",
        block_mode="findings",
        max_rounds=3,
        on_max_rounds_label="factory:needs-human",
        status="revision_needed",
        summary="Review requested revisions -> returning to factory:build",
        record_review_report=True,
    )
    return FlowConfig(
        entry="triage",
        open_questions_stages=["spec"],
        transitions={
            "triage": {
                "DUPLICATE": triage_escalate,
                "TOO_BIG": triage_escalate,
                "NEEDS_HUMAN": triage_escalate,
                "NEEDS_INPUT": triage_escalate,
                "STOP": triage_escalate,
                "TRIVIAL": TransitionConfig(
                    label="factory:spec-ready",
                    action="update_issue_block",
                    block_mode="append_section",
                    section_title="## Triage Report (`TRIVIAL`)",
                    replace_existing=True,
                    status="trivial",
                    summary=(
                        "Triage fast-tracked (TRIVIAL) -> waiting for Human Gate 1 "
                        "approval (factory:spec-ready)"
                    ),
                ),
                "DEFAULT": TransitionConfig(
                    label="factory:spec",
                    action="update_issue_block",
                    block_mode="append_section",
                    section_title="## Triage Report (`OK`)",
                    replace_existing=True,
                    status="ok",
                    summary="Triage passed -> ready for spec",
                ),
            },
            "spec": {
                "NEEDS_HUMAN": spec_escalate,
                "NEEDS_INPUT": spec_escalate,
                "OPEN_QUESTIONS": spec_escalate,
                "DEFAULT": TransitionConfig(
                    label="factory:spec-ready",
                    action="update_issue_block",
                    block_mode="replace",
                    status="spec_ready",
                    summary="Spec complete -> waiting for Human Gate 1 approval (factory:spec-ready)",
                ),
            },
            "build": {
                "DEFAULT": TransitionConfig(
                    label="factory:review",
                    action="none",
                    block_mode="preserve",
                    status="built",
                    summary="",
                ),
            },
            "review": {
                "ACCEPTED": review_accept,
                "PASS": review_accept,
                "HUMAN_DECISION_NEEDED": review_escalate,
                "NEEDS_HUMAN": review_escalate,
                "STOP": review_escalate,
                "DEFAULT": review_revise,
            },
            "done": {
                "DEFAULT": TransitionConfig(
                    label="factory:completed",
                    action="cleanup_worktree",
                    block_mode="preserve",
                    status="completed",
                    summary="",
                ),
            },
        },
    )


def _build_default_refactor_flow() -> FlowConfig:
    """Return the built-in 4-stage refactoring workflow (`flow:refactor`)."""
    plan_ready = TransitionConfig(
        label="factory:plan-ready",
        action="update_issue_block",
        block_mode="replace",
        status="plan_ready",
        summary="Refactor Contract complete -> waiting for Human Gate 1 approval (factory:plan-ready)",
    )
    plan_escalate = TransitionConfig(
        label="factory:needs-human",
        action="update_issue_block",
        block_mode="replace",
        status="needs_human",
        summary="Refactor plan requires human attention ({token})",
    )
    review_accept = TransitionConfig(
        label="factory:pr-ready",
        action="publish_pr",
        block_mode="clear_findings",
        status="accepted",
        summary="Refactor review accepted -> PR ready for Human Gate 2",
        record_review_report=True,
    )
    review_escalate = TransitionConfig(
        label="factory:needs-human",
        action="update_issue_block",
        block_mode="append_section",
        section_title="## Review Escalation",
        replace_existing=False,
        status="needs_human",
        summary="Refactor review requires human decision",
        record_review_report=True,
    )
    review_revise = TransitionConfig(
        label="factory:refactor-apply",
        action="update_issue_block",
        block_mode="findings",
        max_rounds=3,
        on_max_rounds_label="factory:needs-human",
        status="revision_needed",
        summary="Refactor review requested revisions -> returning to factory:refactor-apply",
        record_review_report=True,
    )
    return FlowConfig(
        entry="refactor-plan",
        open_questions_stages=["refactor-plan"],
        transitions={
            "refactor-plan": {
                "READY": plan_ready,
                "SPLIT_NEEDED": plan_escalate,
                "NEEDS_HUMAN": plan_escalate,
                "NEEDS_INPUT": plan_escalate,
                "DEFAULT": plan_ready,
            },
            "refactor-apply": {
                "FAIL": TransitionConfig(
                    label="factory:needs-human",
                    action="none",
                    block_mode="preserve",
                    status="failed",
                    summary="Refactor apply failed -> waiting for human intervention",
                ),
                "DEFAULT": TransitionConfig(
                    label="factory:refactor-review",
                    action="none",
                    block_mode="preserve",
                    status="applied",
                    summary="",
                ),
            },
            "refactor-review": {
                "ACCEPTED": review_accept,
                "PASS": review_accept,
                "HUMAN_DECISION_NEEDED": review_escalate,
                "NEEDS_HUMAN": review_escalate,
                "STOP": review_escalate,
                "DEFAULT": review_revise,
            },
            "done": {
                "DEFAULT": TransitionConfig(
                    label="factory:completed",
                    action="cleanup_worktree",
                    block_mode="preserve",
                    status="completed",
                    summary="",
                ),
            },
        },
    )


DEFAULT_PR_FLOW: FlowConfig = _build_default_pr_flow()
DEFAULT_REFACTOR_FLOW: FlowConfig = _build_default_refactor_flow()
BUILTIN_FLOWS: dict[str, FlowConfig] = {
    "pr": DEFAULT_PR_FLOW,
    "refactor": DEFAULT_REFACTOR_FLOW,
}


class BudgetConfig(BaseModel):
    """Execution budget guardrails to limit tokens, financial cost, and turns."""

    max_tokens_per_run: int | None = Field(
        default=None,
        ge=1,
        description="Cumulative token limit across all turns in a run",
    )
    max_cost_usd: float | None = Field(
        default=None,
        ge=0.0,
        allow_inf_nan=False,
        description="Cumulative cost limit in USD across all turns in a run",
    )
    max_turns: int | None = Field(
        default=None,
        ge=1,
        description="Maximum number of agent turns allowed in a run",
    )


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


class GuardrailsConfig(BaseModel):
    """Guardrails policy configuration to contain agent file access and block dangerous commands."""

    enabled: bool = Field(
        default=True,
        description="Whether workspace path containment and command guardrails are enforced",
    )
    denied_commands: list[str] = Field(
        default_factory=list,
        description="Additional custom command substrings or regex patterns to deny",
    )
    allowed_paths: list[str] = Field(
        default_factory=list,
        description="Additional external host directory paths permitted for agent access",
    )
    blocked_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Workspace-relative or absolute paths to block from direct access and file-dump commands"
        ),
    )


class FleetConfig(BaseModel):
    """Fleet orchestration configuration (multi-repo runs)."""

    repositories: dict[str, str] = Field(
        default_factory=dict,
        description="Map of 'owner/repo' to an explicit local checkout path (absolute or ~-relative).",
    )
    max_active_branches_per_repo: int = Field(
        default=1,
        ge=1,
        description=(
            "Reserved for future per-repo concurrency. The fleet runner currently "
            "advances at most one build/review/pr-ready branch per repository regardless "
            "of this value."
        ),
    )
    max_review_rounds: int | None = Field(
        default=None,
        ge=1,
        description="Review round circuit-breaker cap (factory:needs-human). When unset, inherits the top-level max_review_rounds.",
    )

    @classmethod
    def from_file(cls, path: Path) -> FleetConfig:
        """Load a fleet configuration from a TOML file.

        Supports both an explicit ``[fleet]`` table and flat root-level keys
        (``repositories``, ``max_active_branches_per_repo``, ``max_review_rounds``)
        so operators do not have to remember which nesting style to use.
        """
        resolved_path = path.expanduser().resolve()
        if resolved_path.suffix.lower() != ".toml":
            raise ValueError(
                f"Unsupported fleet config extension '{resolved_path.suffix}'. Use .toml."
            )
        if not resolved_path.is_file():
            raise FileNotFoundError(
                f"Fleet configuration file not found: {resolved_path}"
            )

        data: Any = tomllib.loads(resolved_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(
                f"Fleet configuration file {resolved_path} must contain a mapping/object."
            )
        fleet_data = data.get("fleet", data)
        if not isinstance(fleet_data, dict):
            raise ValueError(
                f"Fleet configuration file {resolved_path} must contain a 'fleet' table "
                "or root-level fleet keys."
            )
        return cls.model_validate(fleet_data)


def discover_fleet_config(fleet_config_path: Path | None = None) -> FleetConfig | None:
    """Load an explicit fleet file or discover the operator-level default.

    When ``fleet_config_path`` is supplied it must exist (raising ``FileNotFoundError``).
    Otherwise ``USER_FLEET_FILES`` is checked in order and the first existing file wins;
    ``None`` is returned when no operator-level fleet file is present.
    """
    if fleet_config_path is not None:
        return FleetConfig.from_file(fleet_config_path)
    for candidate in USER_FLEET_FILES:
        expanded = candidate.expanduser()
        if expanded.is_file():
            return FleetConfig.from_file(expanded)
    return None


def _normalize_runs_dir(raw: str | Path, workspace: Path) -> Path:
    """Expand and anchor an explicit runs directory at the main repository root."""
    candidate = Path(str(raw).strip()).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    # Function-local import: acp_factory.runner re-exports loop.py, which imports
    # this module, so a top-level import would create a circular dependency.
    from .runner.worktree import resolve_main_repo_root

    return (resolve_main_repo_root(workspace) / candidate).resolve()


def _default_runs_dir(workspace: Path) -> Path:
    """Return the repo-scoped XDG data directory used when no runs_dir is configured."""
    # Function-local import to avoid the config -> runner -> loop -> config cycle.
    from .runner.worktree import resolve_main_repo_root

    xdg_raw = os.environ.get("XDG_DATA_HOME", "").strip()
    xdg_home = Path(xdg_raw).expanduser() if xdg_raw else None
    data_home = (
        xdg_home
        if xdg_home is not None and xdg_home.is_absolute()
        else Path.home() / ".local" / "share"
    )

    main_root = resolve_main_repo_root(workspace)
    slug = re.sub(r"[^a-z0-9._-]+", "-", main_root.name.lower()).strip("-._") or "repo"
    digest = hashlib.sha256(str(main_root).encode("utf-8")).hexdigest()[:12]
    return (data_home / "acp-factory" / "runs" / f"{slug}-{digest}").resolve()


def resolve_runs_dir(
    workspace: Path,
    *,
    runs_dir: str | Path | None = None,
) -> Path:
    """Resolve the directory that stores run artifacts for ``workspace``.

    Precedence (highest first): the ``FACTORY_RUNS_DIR`` environment variable,
    the explicit ``runs_dir`` argument, then a repo-scoped directory under the
    XDG data home. Blank values are treated as unset. Explicit relative paths
    anchor at the main repository root so linked worktrees share one bucket.
    This function performs no filesystem writes.
    """
    env_runs_dir = os.environ.get("FACTORY_RUNS_DIR", "").strip()
    if env_runs_dir:
        return _normalize_runs_dir(env_runs_dir, workspace)
    if runs_dir is not None and str(runs_dir).strip():
        return _normalize_runs_dir(runs_dir, workspace)
    return _default_runs_dir(workspace)


class FactoryConfig(BaseModel):
    """Top-level configuration for an acp-factory run."""

    version: str = Field(default="1")
    resources_dir: str | None = Field(
        default=None,
        description=(
            "Optional path to external AI resources catalog (agents/, prompts/, skills/). "
            "Can also be provided via the FACTORY_RESOURCES_DIR environment variable."
        ),
    )
    runs_dir: str | None = Field(
        default=None,
        description=(
            "Optional directory for run artifacts. Relative paths anchor at the main "
            "repository root. FACTORY_RUNS_DIR takes precedence; when unset, a repo-scoped "
            "directory under the XDG data home is used."
        ),
    )
    resource_dirs: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered list of external AI resource catalog directories (orchestration, org policies, domain catalogs). "
            "Layered after resources_dir and before package-bundled defaults."
        ),
    )
    instructions: list[str] = Field(
        default_factory=list,
        description="Top-level instruction Markdown paths attached to all agent sessions",
    )
    discover_apm_resources: bool = Field(
        default=True,
        description="Auto-discover workspace/APM skills and instructions (.agents/skills/, .apm/, apm_modules/, .github/instructions/)",
    )
    agent: AgentConfig = Field(default_factory=AgentConfig)
    setup: list[str] = Field(default_factory=list)
    task: str = Field(default="")
    checks: list[CheckConfig] = Field(default_factory=list)
    named_checks: dict[str, CheckConfig] = Field(default_factory=dict)
    stages: dict[str, StageConfig] = Field(default_factory=dict)
    flows: dict[str, FlowConfig] = Field(
        default_factory=dict,
        description="Optional [flows.<name>] declarative workflow definitions overriding or extending built-in flows.",
    )
    active_stage: str | None = Field(default=None)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    fleet: FleetConfig | None = Field(
        default=None,
        description="Optional [fleet] multi-repo/organization orchestration section.",
    )
    max_review_rounds: int = Field(
        default=3,
        ge=1,
        description="Top-level review round circuit-breaker cap (used when [fleet] is absent).",
    )
    worktree: bool = Field(default=False)
    dry_run: bool = Field(
        default=False,
        description="When true, never execute mutating gh/git push commands; output files and human commands instead.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Custom environment variables injected into checks, setup, and ACP agent subprocesses.",
    )
    strip_env: list[str] = Field(
        default_factory=list,
        description="Additional environment variable names to strip from workspace subprocesses.",
    )
    keep_env: list[str] = Field(
        default_factory=list,
        description=(
            "Environment variable names to preserve even if they match sensitive "
            "credential patterns or DEFAULT_HOST_ENV_STRIP."
        ),
    )
    thought_max_chars: int | None = Field(
        default=None,
        ge=0,
        strict=True,
        description=(
            "Default maximum agent thought characters in markdown/condensed transcripts. "
            "Stage-level values override this."
        ),
    )

    _config_dir: Path | None = PrivateAttr(default=None)

    @property
    def config_dir(self) -> Path | None:
        return self._config_dir

    @property
    def resolved_resources_dir(self) -> Path | None:
        """Return the primary resolved external resources catalog directory, if configured or discovered."""
        dirs = self.resolved_resource_dirs
        return dirs[0] if dirs else None

    @property
    def resolved_resource_dirs(self) -> list[Path]:
        """Return all resolved external AI resource catalog directories in precedence order."""
        return resolve_resource_dirs(
            resources_dir=self.resources_dir,
            resource_dirs=self.resource_dirs,
            config_dir=self._config_dir,
        )

    def resolve_runs_dir(self, workspace: Path) -> Path:
        """Return the resolved run-artifact directory for ``workspace``.

        Delegates to the module-level :func:`resolve_runs_dir`, honoring the
        ``FACTORY_RUNS_DIR`` environment variable before ``self.runs_dir``.
        """
        return resolve_runs_dir(workspace, runs_dir=self.runs_dir)

    @property
    def effective_max_review_rounds(self) -> int:
        """Return the review round cap, preferring `[fleet].max_review_rounds` when set."""
        if self.fleet is not None and self.fleet.max_review_rounds is not None:
            return self.fleet.max_review_rounds
        return self.max_review_rounds

    @model_validator(mode="after")
    def _validate_custom_flow_entries(self) -> FactoryConfig:
        """Require explicit `entry` on non-builtin custom flows."""
        for flow_name, flow_cfg in self.flows.items():
            if flow_name not in BUILTIN_FLOWS and flow_cfg.entry is None:
                raise ValueError(
                    f"Custom flow '{flow_name}' must specify a non-empty 'entry' stage."
                )
        return self

    def effective_flows(self) -> dict[str, FlowConfig]:
        """Return all available flows, merging built-in presets with partial or full overrides."""
        merged = dict(BUILTIN_FLOWS)
        for flow_name, flow_cfg in self.flows.items():
            if flow_name in merged:
                merged[flow_name] = _merge_flow_override(merged[flow_name], flow_cfg)
            else:
                merged[flow_name] = flow_cfg
        return merged

    def get_flow(self, flow_name: str = "pr") -> FlowConfig:
        """Resolve a named flow from configured flows or built-in presets."""
        key = flow_name.strip()
        merged = self.effective_flows()
        if key not in merged:
            available = ", ".join(sorted(merged.keys()))
            raise ValueError(
                f"Unknown flow '{flow_name}'. Available flows: {available}"
            )
        return merged[key]

    def stage_capabilities(self, stage_name: str | None) -> StageCapabilities:
        """Return effective capabilities for `stage_name`, combining StageConfig and legacy defaults."""
        if not stage_name:
            return StageCapabilities()
        defaults = _BUILTIN_STAGE_CAPABILITIES.get(stage_name, StageCapabilities())
        stage_cfg = self.stages.get(stage_name)
        if stage_cfg is None:
            return defaults
        return StageCapabilities(
            worktree=(
                stage_cfg.worktree
                if stage_cfg.worktree is not None
                else defaults.worktree
            ),
            require_clean_worktree=(
                stage_cfg.require_clean_worktree
                if stage_cfg.require_clean_worktree is not None
                else defaults.require_clean_worktree
            ),
            delta_review=(
                stage_cfg.delta_review
                if stage_cfg.delta_review is not None
                else defaults.delta_review
            ),
            inject_prior_notes_from=(
                stage_cfg.inject_prior_notes_from
                if stage_cfg.inject_prior_notes_from is not None
                else defaults.inject_prior_notes_from
            ),
            revision_brief=(
                stage_cfg.revision_brief
                if stage_cfg.revision_brief is not None
                else defaults.revision_brief
            ),
            orchestrator_only=(
                stage_cfg.orchestrator_only
                if stage_cfg.orchestrator_only is not None
                else defaults.orchestrator_only
            ),
        )

    def is_branch_contending_stage(self, stage_name: str | None) -> bool:
        """Return True when `stage_name` runs in a branch worktree and contends for a branch slot."""
        return self.stage_capabilities(stage_name).worktree

    def publish_pr_labels(self) -> set[str]:
        """Return all gate labels produced by `publish_pr` transitions across configured flows."""
        labels = {"factory:pr-ready"}
        for flow_cfg in self.effective_flows().values():
            for token_map in flow_cfg.transitions.values():
                for transition in token_map.values():
                    if transition.action == "publish_pr":
                        labels.add(transition.label)
        return labels

    def actionable_stage_labels(self) -> dict[str, str]:
        """Return `{label: stage}` for every actionable stage across all effective flows."""
        mapping: dict[str, str] = {}
        for flow_cfg in self.effective_flows().values():
            if flow_cfg.entry:
                mapping[f"factory:{flow_cfg.entry}"] = flow_cfg.entry
            for stage_name in flow_cfg.transitions:
                mapping[f"factory:{stage_name}"] = stage_name
        mapping["factory:done"] = "done"
        return mapping

    def resolve_issue_flow(
        self, labels: list[str], *, stage: str | None = None
    ) -> tuple[str, FlowConfig]:
        """Resolve `(flow_name, FlowConfig)` from `flow:<name>` labels or unique stage ownership.

        Rules:
        1. If an explicit `flow:<name>` label is present, use it (failing fast if unknown).
        2. Otherwise, if `stage` uniquely belongs to a single configured flow, infer that flow.
        3. Otherwise default to `pr`.
        4. If `stage` is provided and does not belong to the resolved flow (other than `done`),
           raise `ValueError`.
        """
        explicit_flow: str | None = None
        for lbl in labels:
            if lbl.startswith("flow:") and lbl.removeprefix("flow:").strip():
                explicit_flow = lbl.removeprefix("flow:").strip()
                break

        merged = self.effective_flows()
        if explicit_flow is not None:
            flow_name = explicit_flow
            flow_cfg = self.get_flow(flow_name)
        else:
            flow_name = "pr"
            if stage and stage != "done":
                owners = [
                    name
                    for name, cfg in merged.items()
                    if stage in cfg.transitions or cfg.entry == stage
                ]
                if len(owners) == 1:
                    flow_name = owners[0]
            flow_cfg = merged[flow_name]

        if (
            stage
            and stage != "done"
            and stage not in flow_cfg.transitions
            and stage != flow_cfg.entry
        ):
            valid_stages = ", ".join(sorted(flow_cfg.transitions.keys())) or "none"
            raise ValueError(
                f"Stage '{stage}' does not belong to flow '{flow_name}' "
                f"(valid stages: {valid_stages}). Add a matching 'flow:<name>' label or update config."
            )
        return flow_name, flow_cfg

    def with_fleet_overlay(
        self, fleet_config_path: Path | None = None
    ) -> FactoryConfig:
        """Return a copy with a fleet configuration overlaid.

        An explicit ``fleet_config_path`` always wins. Otherwise an operator-level
        default is discovered only when this config has no ``[fleet]`` section, so
        a repo-local fleet definition is never silently replaced.
        """
        updated = self.model_copy(deep=True)
        updated._config_dir = self._config_dir
        if fleet_config_path is not None:
            updated.fleet = FleetConfig.from_file(fleet_config_path)
        elif self.fleet is None:
            discovered = discover_fleet_config()
            if discovered is not None:
                updated.fleet = discovered
        return updated

    @model_validator(mode="before")
    @classmethod
    def _parse_checks_mapping(
        cls, data: dict[str, Any] | FactoryConfig
    ) -> dict[str, Any] | FactoryConfig:
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
        cls,
        workspace: Path,
        config_path: Path | None = None,
        *,
        allow_user_defaults: bool = True,
    ) -> FactoryConfig:
        """Load from an explicit config path or auto-discover in the workspace.

        Discovery precedence (highest first): an explicit `config_path`, a
        repo-local config file (``DEFAULT_CONFIG_FILES``), a user-level config
        file (``USER_CONFIG_FILES``), then built-in defaults. `allow_user_defaults`
        opts out of the user-level fallback so fleet repos never inherit the
        operator's personal settings.
        """
        if config_path is not None:
            resolved = (
                config_path if config_path.is_absolute() else (workspace / config_path)
            )
            return cls.from_file(resolved)

        for rel_path in DEFAULT_CONFIG_FILES:
            candidate = workspace / rel_path
            if candidate.exists():
                return cls.from_file(candidate)

        if allow_user_defaults:
            for user_path in USER_CONFIG_FILES:
                candidate = user_path.expanduser()
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
        if stage.thought_level is not None:
            resolved.agent.thought_level = stage.thought_level
        if stage.read_only is not None:
            resolved.agent.read_only = stage.read_only
        if stage.persona is not None:
            resolved.agent.persona = stage.persona
        if stage.skills:
            resolved.agent.skills = list(stage.skills)
        merged_instructions = list(
            dict.fromkeys(
                [
                    *resolved.instructions,
                    *resolved.agent.instructions,
                    *stage.instructions,
                ]
            )
        )
        if merged_instructions:
            resolved.agent.instructions = merged_instructions
        if stage.discover_apm_resources is not None:
            resolved.discover_apm_resources = stage.discover_apm_resources
            resolved.agent.discover_apm_resources = stage.discover_apm_resources
        elif resolved.agent.discover_apm_resources is None:
            resolved.agent.discover_apm_resources = resolved.discover_apm_resources
        if stage.timeout is not None:
            resolved.agent.timeout = stage.timeout
        if stage.thought_max_chars is not None:
            resolved.thought_max_chars = stage.thought_max_chars

        # 2. Merge pre-setup commands
        if stage.pre:
            resolved.setup = list(stage.pre)

        # 3. Resolve stage prompt file (if specified) + combine with task
        res_dirs = self.resolved_resource_dirs
        if stage.prompt:
            prompt_text = _load_markdown_or_inline(
                stage.prompt,
                config_dir=self._config_dir,
                workspace=workspace,
                resource_dirs=res_dirs,
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
                    resource_dirs=res_dirs,
                )
            resolved_checks.append(chk)

        if stage.env:
            merged_env = dict(resolved.env)
            merged_env.update(stage.env)
            resolved.env = merged_env
        if stage.strip_env:
            resolved.strip_env = list(
                dict.fromkeys(resolved.strip_env + stage.strip_env)
            )
        if stage.keep_env:
            resolved.keep_env = list(dict.fromkeys(resolved.keep_env + stage.keep_env))

        resolved.checks = resolved_checks
        return resolved

    def resolve_repo_stage(
        self,
        stage_name: str,
        repo_checkout: Path,
        *,
        workspace: Path | None = None,
        task: str | None = None,
    ) -> FactoryConfig:
        """Resolve a stage for a fleet target repository, overlaying that repo's own
        discovered config file (`.factory/config.toml`, `factory.yaml`, etc.) onto the
        central stage configuration.

        Central governance (personas, prompts, skills, agent) is inherited from this
        fleet config; a target repository's `pre` setup commands and `[checks.*]`
        override the fleet defaults. Target repos require zero `.factory/` scaffolding
        to participate.

        `task` supplies the issue context; when a stage prompt is present it is wrapped
        under it (mirroring `resolve_stage`), rather than overwriting the prompt.
        """
        base = self.model_copy(deep=True)
        base._config_dir = self._config_dir
        if task is not None:
            base.task = task

        if stage_name in self.stages:
            resolved = base.resolve_stage(stage_name, workspace=workspace)
        else:
            # No central stage definition: fall back to built-in defaults (read-only for
            # triage/spec/review), mirroring run_issue_stage's non-fleet fallback.
            resolved = base
            resolved.agent.read_only = stage_name in ("triage", "spec", "review")
            resolved.active_stage = stage_name

        repo_cfg = FactoryConfig.discover(repo_checkout, allow_user_defaults=False)
        repo_stage = repo_cfg.stages.get(stage_name)
        if repo_stage is None:
            return resolved

        # Overlay repo-local pre setup, checks, and optional domain skills/instructions;
        # central stage governance is otherwise inherited.
        if repo_stage.pre:
            resolved.setup = list(repo_stage.pre)

        if repo_stage.skills:
            resolved.agent.skills = list(
                dict.fromkeys([*resolved.agent.skills, *repo_stage.skills])
            )
        if repo_stage.instructions or repo_cfg.instructions:
            resolved.agent.instructions = list(
                dict.fromkeys(
                    [
                        *resolved.agent.instructions,
                        *repo_cfg.instructions,
                        *repo_stage.instructions,
                    ]
                )
            )
        if repo_stage.discover_apm_resources is not None:
            resolved.discover_apm_resources = repo_stage.discover_apm_resources
            resolved.agent.discover_apm_resources = repo_stage.discover_apm_resources

        if repo_stage.checks:
            resolved_checks: list[CheckConfig] = []
            merged_res_dirs = list(
                dict.fromkeys(
                    [*repo_cfg.resolved_resource_dirs, *self.resolved_resource_dirs]
                )
            )
            for item in repo_stage.checks:
                if isinstance(item, CheckConfig):
                    chk = item.model_copy(deep=True)
                elif item in repo_cfg.named_checks:
                    chk = repo_cfg.named_checks[item].model_copy(deep=True)
                else:
                    chk = CheckConfig(name=item, cmd=item)
                if chk.prompt:
                    chk.prompt = _load_markdown_or_inline(
                        chk.prompt,
                        config_dir=repo_cfg.config_dir,
                        workspace=repo_checkout,
                        resource_dirs=merged_res_dirs,
                    )
                resolved_checks.append(chk)
            resolved.checks = resolved_checks

        if repo_stage.env:
            merged_env = dict(resolved.env)
            merged_env.update(repo_stage.env)
            resolved.env = merged_env
        if repo_stage.strip_env:
            resolved.strip_env = list(
                dict.fromkeys(resolved.strip_env + repo_stage.strip_env)
            )
        if repo_stage.keep_env:
            resolved.keep_env = list(
                dict.fromkeys(resolved.keep_env + repo_stage.keep_env)
            )

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
        thought_level: str | None = None,
        read_only: bool | None = None,
        persona: str | None = None,
        skills: list[str] | None = None,
        timeout: int | None = None,
        checks: list[str] | None = None,
        review_prompts: list[str] | None = None,
        setup_cmds: list[str] | None = None,
        max_attempts: int | None = None,
        max_tokens: int | None = None,
        max_cost: float | None = None,
        max_turns: int | None = None,
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
        if thought_level is not None:
            updated.agent.thought_level = _validate_thought_level(thought_level)
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
            res_dirs = updated.resolved_resource_dirs
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
                                resource_dirs=res_dirs,
                            )
                        merged_checks.append(chk)
                elif chk_item in updated.named_checks:
                    chk = updated.named_checks[chk_item].model_copy(deep=True)
                    if chk.prompt:
                        chk.prompt = _load_markdown_or_inline(
                            chk.prompt,
                            config_dir=updated._config_dir,
                            workspace=workspace,
                            resource_dirs=res_dirs,
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
        if max_tokens is not None or max_cost is not None or max_turns is not None:
            # Rebuild the sub-model so CLI overrides re-run validation (e.g. reject
            # a zero/negative cap that would silently disable enforcement).
            updated.budget = BudgetConfig(
                max_tokens_per_run=(
                    max_tokens
                    if max_tokens is not None
                    else updated.budget.max_tokens_per_run
                ),
                max_cost_usd=(
                    max_cost if max_cost is not None else updated.budget.max_cost_usd
                ),
                max_turns=(
                    max_turns if max_turns is not None else updated.budget.max_turns
                ),
            )
        if worktree is not None:
            updated.worktree = worktree
        if keep_worktree is not None:
            updated.retry.keep_worktree_on_failure = keep_worktree
            updated.retry.keep_worktree_on_success = keep_worktree
        if dry_run is not None:
            updated.dry_run = dry_run

        return updated
