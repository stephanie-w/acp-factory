#!/usr/bin/env python3
"""Create or update GitHub labels required by configured acp-factory flows.

Usage:
    uv run --frozen python scripts/sync_factory_labels.py
    uv run --frozen python scripts/sync_factory_labels.py --flow refactor --dry-run
    uv run --frozen python scripts/sync_factory_labels.py --repo owner/repo
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
from pathlib import Path

from acp_factory.config import FactoryConfig, FlowConfig
from acp_factory.github import LABEL_COLOR_MAP, run_gh

LABEL_DESCRIPTIONS: dict[str, str] = {
    "factory:triage": "acp-factory stage: issue triage and sizing",
    "factory:spec": "acp-factory stage: technical specification authoring",
    "factory:spec-ready": "acp-factory Human Gate 1: specification awaiting approval",
    "factory:build": "acp-factory stage: implementation and verification in worktree",
    "factory:review": "acp-factory stage: skeptical code and security review",
    "factory:pr-ready": "acp-factory Human Gate 2: reviewed PR awaiting manual merge",
    "factory:done": "acp-factory stage: worktree cleanup",
    "factory:completed": "acp-factory terminal: worktree cleaned up and issue completed",
    "factory:needs-human": "acp-factory escalation: human decision or input required",
    "flow:refactor": "acp-factory flow selector: behavior-preserving refactoring flow",
    "factory:refactor-plan": "acp-factory stage: Refactor Contract authoring",
    "factory:plan-ready": "acp-factory Human Gate 1: Refactor Contract awaiting approval",
    "factory:refactor-apply": "acp-factory stage: behavior-preserving refactor in worktree",
    "factory:refactor-review": "acp-factory stage: behavior-preservation and seam review",
}


def collect_flow_labels(
    config: FactoryConfig, *, flow_names: list[str] | None = None
) -> list[str]:
    """Return sorted unique GitHub labels required by the selected flows."""
    effective = config.effective_flows()
    selected_names = flow_names or sorted(effective.keys())
    selected: dict[str, FlowConfig] = {
        name: config.get_flow(name) for name in selected_names
    }

    labels: set[str] = set()
    for flow_name, flow_cfg in selected.items():
        if flow_name != "pr":
            labels.add(f"flow:{flow_name}")
        if flow_cfg.entry:
            labels.add(f"factory:{flow_cfg.entry}")
        for stage_name, token_map in flow_cfg.transitions.items():
            labels.add(f"factory:{stage_name}")
            for transition in token_map.values():
                labels.add(transition.label)
                if transition.block_mode == "findings":
                    labels.add(transition.on_max_rounds_label)
    return sorted(labels)


async def sync_labels(
    labels: list[str],
    *,
    cwd: Path,
    repo_slug: str | None = None,
    dry_run: bool = False,
) -> None:
    """Create or update each GitHub label idempotently via `gh label create --force`."""
    for label in labels:
        color = LABEL_COLOR_MAP.get(label, "EDEDED")
        description = LABEL_DESCRIPTIONS.get(
            label, f"acp-factory workflow label ({label})"
        )
        cmd = [
            "label",
            "create",
            label,
            "--color",
            color,
            "--description",
            description,
            "--force",
        ]
        if repo_slug:
            cmd.extend(["--repo", repo_slug])
        if dry_run:
            print(f"gh {shlex.join(cmd)}")
        else:
            await run_gh(cmd, cwd=cwd)
            print(f"Synced label: {label} (#{color})")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for syncing acp-factory GitHub labels."""
    parser = argparse.ArgumentParser(
        description="Create or update GitHub labels for configured acp-factory flows."
    )
    parser.add_argument(
        "--workspace",
        "-w",
        type=Path,
        default=Path("."),
        help="Target repository workspace (default: current directory).",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=None,
        help="Optional path to .factory/config.toml.",
    )
    parser.add_argument(
        "--flow",
        "-f",
        action="append",
        dest="flows",
        default=None,
        help="Flow name(s) to sync labels for (repeatable; defaults to all configured flows).",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Target GitHub repository slug (owner/repo); defaults to current repo.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print `gh label create` commands without mutating GitHub.",
    )
    args = parser.parse_args(argv)

    workspace = args.workspace.resolve()
    cfg = FactoryConfig.discover(workspace, args.config)
    labels = collect_flow_labels(cfg, flow_names=args.flows)
    asyncio.run(
        sync_labels(
            labels,
            cwd=workspace,
            repo_slug=args.repo,
            dry_run=args.dry_run,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
