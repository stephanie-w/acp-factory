"""Unit tests for Phase 2: RunMetadata, FlowConfig/TransitionConfig, and data-driven verdict.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from acp_factory.config import (
    DEFAULT_PR_FLOW,
    FactoryConfig,
    FlowConfig,
    TransitionConfig,
)
from acp_factory.runner import RunArtifactStore, RunMetadata
from acp_factory.verdict import parse_stage_verdict


def test_run_metadata_persists_optional_flow_field(tmp_path: Path) -> None:
    """`flow` is written to run.json when provided and omitted when None."""
    cfg = FactoryConfig(task="Check metadata")
    store = RunArtifactStore(tmp_path, cfg)

    without_flow = store.persist(
        "run-no-flow",
        succeeded=True,
        attempts_used=1,
        total_duration=0.1,
        setup_logs=[],
        turn_transcripts=[],
        check_history=[],
        failure_reason=None,
        metadata=RunMetadata(issue_number=7, branch="factory/issue-7"),
    )
    data_without = json.loads((without_flow / "run.json").read_text(encoding="utf-8"))
    assert "flow" not in data_without
    assert data_without["issue_number"] == 7

    with_flow = store.persist(
        "run-with-flow",
        succeeded=True,
        attempts_used=1,
        total_duration=0.1,
        setup_logs=[],
        turn_transcripts=[],
        check_history=[],
        failure_reason=None,
        metadata=RunMetadata(flow="refactor", issue_number=7, branch="factory/issue-7"),
    )
    data_with = json.loads((with_flow / "run.json").read_text(encoding="utf-8"))
    assert data_with["flow"] == "refactor"


def test_factory_config_parses_custom_flows_from_toml_and_preserves_builtin_pr(
    tmp_path: Path,
) -> None:
    """Custom `[flows.<name>]` parses from TOML while built-in `pr` remains available."""
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        """\
[flows.refactor]
entry = "refactor-plan"

[flows.refactor.transitions.refactor-plan]
ready = { label = "factory:plan-ready", action = "update_issue_block", block_mode = "replace" }
SPLIT_NEEDED = { label = "factory:needs-human", action = "update_issue_block", block_mode = "replace" }

[flows.refactor.transitions.refactor-review]
ACCEPTED = { label = "factory:pr-ready", action = "publish_pr", block_mode = "clear_findings", record_review_report = true }
REVISION_NEEDED = { label = "factory:refactor-apply", action = "update_issue_block", block_mode = "findings", max_rounds = 2, on_max_rounds_label = "factory:needs-human", record_review_report = true }
""",
        encoding="utf-8",
    )
    cfg = FactoryConfig.from_file(toml_path)
    assert cfg.get_flow("pr") == DEFAULT_PR_FLOW
    refactor_flow = cfg.get_flow("refactor")
    assert refactor_flow.entry == "refactor-plan"
    # Lowercase TOML keys normalize to uppercase
    assert "READY" in refactor_flow.transitions["refactor-plan"]
    assert (
        refactor_flow.transitions["refactor-review"]["REVISION_NEEDED"].max_rounds == 2
    )

    with pytest.raises(ValueError, match="Unknown flow 'does-not-exist'"):
        cfg.get_flow("does-not-exist")


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"label": "   "},
        {"label": "factory:ok", "on_max_rounds_label": "  "},
        {"label": "factory:ok", "action": "delete_repo"},
        {"label": "factory:ok", "block_mode": "overwrite_all"},
        {"label": "factory:ok", "max_rounds": 0},
    ],
)
def test_transition_config_rejects_invalid_values(
    bad_kwargs: dict[str, object],
) -> None:
    """Invalid labels, actions, block_modes, and non-positive max_rounds fail validation."""
    with pytest.raises(ValidationError):
        TransitionConfig.model_validate(bad_kwargs)


def test_flow_config_rejects_blank_entry() -> None:
    """FlowConfig rejects an empty or whitespace-only entry stage, and custom flows require entry."""
    with pytest.raises(ValidationError):
        FlowConfig(entry="   ")

    with pytest.raises(ValidationError, match="must specify a non-empty 'entry'"):
        FactoryConfig.model_validate({"flows": {"custom": {"transitions": {}}}})


def test_partial_override_merges_with_builtin_pr_flow(tmp_path: Path) -> None:
    """Overriding a single transition in `[flows.pr]` inherits entry and all other transitions."""
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        """\
[flows.pr.transitions.spec]
DEFAULT = { label = "factory:build", action = "update_issue_block", block_mode = "replace", status = "spec_ready" }
""",
        encoding="utf-8",
    )
    cfg = FactoryConfig.from_file(toml_path)
    pr_flow = cfg.get_flow("pr")

    assert pr_flow.entry == "triage"
    assert pr_flow.open_questions_stages == ["spec"]
    # Overridden transition takes effect
    v_spec = parse_stage_verdict("spec", "# Spec\n\nSTATUS: SPEC_READY", flow=pr_flow)
    assert v_spec.target_label == "factory:build"
    # Unchanged transitions on the same stage and other stages are inherited
    v_spec_q = parse_stage_verdict(
        "spec",
        "# Spec\n\n## Open Questions\n1. A or B?\n\nSTATUS: NEEDS_HUMAN",
        flow=pr_flow,
    )
    assert v_spec_q.target_label == "factory:needs-human"
    v_triage = parse_stage_verdict("triage", "Scoped.\nSTATUS: OK", flow=pr_flow)
    assert v_triage.target_label == "factory:spec"


def test_parse_stage_verdict_drives_custom_refactor_flow() -> None:
    """`parse_stage_verdict` executes all block_modes and circuit breakers from a custom FlowConfig."""
    refactor_flow = FlowConfig(
        entry="refactor-plan",
        transitions={
            "refactor-plan": {
                "READY": TransitionConfig(
                    label="factory:plan-ready",
                    action="update_issue_block",
                    block_mode="replace",
                ),
                "SPLIT_NEEDED": TransitionConfig(
                    label="factory:needs-human",
                    action="update_issue_block",
                    block_mode="append_section",
                    section_title="## Split Proposal",
                    replace_existing=True,
                ),
            },
            "refactor-apply": {
                "DEFAULT": TransitionConfig(
                    label="factory:refactor-review",
                    action="none",
                    block_mode="preserve",
                    status="applied",
                ),
            },
            "refactor-review": {
                "ACCEPTED": TransitionConfig(
                    label="factory:pr-ready",
                    action="publish_pr",
                    block_mode="clear_findings",
                    record_review_report=True,
                ),
                "DEFAULT": TransitionConfig(
                    label="factory:refactor-apply",
                    action="update_issue_block",
                    block_mode="findings",
                    max_rounds=2,
                    on_max_rounds_label="factory:refactor-escalated",
                    record_review_report=True,
                ),
            },
        },
    )

    # 1. replace block_mode
    v_plan = parse_stage_verdict(
        "refactor-plan",
        "# Refactor Contract\n\n## Invariant\n- Keep API.\n\nSTATUS: READY",
        flow=refactor_flow,
    )
    assert v_plan.target_label == "factory:plan-ready"
    assert v_plan.action == "update_issue_block"
    assert v_plan.publish_pr is False
    assert "# Refactor Contract" in v_plan.updated_factory_block

    # 2. append_section (replace_existing=True)
    v_split = parse_stage_verdict(
        "refactor-plan",
        "### Steps\n1. Seam A\n2. Seam B\n\nSTATUS: SPLIT_NEEDED",
        flow=refactor_flow,
    )
    assert v_split.target_label == "factory:needs-human"
    assert v_split.updated_factory_block.startswith("## Split Proposal\n\n### Steps")

    # 3. preserve block_mode
    v_apply = parse_stage_verdict(
        "refactor-apply",
        "### Refactor Summary\n- Done.\n\nSTATUS: PASS",
        current_factory_block=v_plan.updated_factory_block,
        flow=refactor_flow,
    )
    assert v_apply.target_label == "factory:refactor-review"
    assert v_apply.status == "applied"
    assert v_apply.updated_factory_block == v_plan.updated_factory_block

    # 4. findings block_mode round 1 -> bounce back to refactor-apply
    v_rev1 = parse_stage_verdict(
        "refactor-review",
        "### Findings\n1. `[major]` circular import\n\nSTATUS: REVISION_NEEDED",
        current_factory_block=v_plan.updated_factory_block,
        reviewed_sha="aaa1111",
        flow=refactor_flow,
    )
    assert v_rev1.target_label == "factory:refactor-apply"
    assert "factory:findings:start" in v_rev1.updated_factory_block
    assert "stage=refactor-apply" in v_rev1.updated_factory_block
    assert "review_round=1" in v_rev1.updated_factory_block

    # 5. findings block_mode round 2 -> trips custom max_rounds=2 -> custom on_max_rounds_label
    v_rev2 = parse_stage_verdict(
        "refactor-review",
        "### Findings\n1. `[major]` still circular\n\nSTATUS: REVISION_NEEDED",
        current_factory_block=v_rev1.updated_factory_block,
        reviewed_sha="bbb2222",
        flow=refactor_flow,
    )
    assert v_rev2.target_label == "factory:refactor-escalated"
    assert v_rev2.status == "needs_human"
    assert "2-round review limit" in v_rev2.updated_factory_block

    # 6. clear_findings + publish_pr
    v_ok = parse_stage_verdict(
        "refactor-review",
        "### Risk Classification: LOW\n\n### Findings\n- None.\n\nSTATUS: ACCEPTED",
        current_factory_block=v_rev1.updated_factory_block,
        reviewed_sha="ccc3333",
        flow=refactor_flow,
    )
    assert v_ok.target_label == "factory:pr-ready"
    assert v_ok.action == "publish_pr"
    assert v_ok.publish_pr is True
    assert "factory:findings:start" not in v_ok.updated_factory_block
    assert "last_reviewed_sha=ccc3333" in v_ok.updated_factory_block
