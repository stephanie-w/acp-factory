"""Unified AI resource packaging, discovery, and resolution."""

from __future__ import annotations

import atexit
import importlib.resources
import json
import os
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ResolvedResources:
    """Resolved AI resources for a stage, session, or prompt check."""

    persona_text: str | None
    instructions: list[Path]
    skills: list[Path]
    resource_dirs: list[Path]
    explicit_skills: list[Path] = field(default_factory=list)
    discovered_skills: list[Path] = field(default_factory=list)


_BUNDLED_EXIT_STACK = ExitStack()
atexit.register(_BUNDLED_EXIT_STACK.close)
_BUNDLED_MATERIALIZED_DIR: Path | None = None


def get_bundled_resources_dir() -> Path:
    """Return the filesystem path to the package-bundled orchestration resources."""
    global _BUNDLED_MATERIALIZED_DIR
    if _BUNDLED_MATERIALIZED_DIR is not None and _BUNDLED_MATERIALIZED_DIR.is_dir():
        return _BUNDLED_MATERIALIZED_DIR

    ref = importlib.resources.files("acp_factory.resources")
    path = Path(str(ref))
    if path.is_dir():
        return path.resolve()

    materialized = _BUNDLED_EXIT_STACK.enter_context(
        importlib.resources.as_file(ref)
    ).resolve()
    _BUNDLED_MATERIALIZED_DIR = materialized
    return materialized


def _parse_env_resource_dirs() -> list[str]:
    """Parse FACTORY_RESOURCES_DIRS (JSON list or os.pathsep) and FACTORY_RESOURCES_DIR."""
    dirs: list[str] = []
    raw_multi = os.environ.get("FACTORY_RESOURCES_DIRS", "").strip()
    if raw_multi:
        if raw_multi.startswith("["):
            try:
                parsed = json.loads(raw_multi)
                if isinstance(parsed, list):
                    dirs.extend(str(item) for item in parsed if str(item).strip())
            except json.JSONDecodeError:
                pass
        else:
            dirs.extend(p for p in raw_multi.split(os.pathsep) if p.strip())

    raw_single = os.environ.get("FACTORY_RESOURCES_DIR", "").strip()
    if raw_single and raw_single not in dirs:
        dirs.append(raw_single)
    return dirs


def _expand_dir_candidate(raw: str | Path, config_dir: Path | None) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() and config_dir is not None:
        return (config_dir / candidate).resolve()
    return candidate.resolve()


def resolve_resource_dirs(
    resources_dir: str | Path | None = None,
    resource_dirs: list[str] | list[Path] | tuple[str | Path, ...] | None = None,
    *,
    config_dir: Path | None = None,
    include_bundled: bool = False,
) -> list[Path]:
    """Resolve layered external AI resource catalog directories in precedence order.

    Precedence:
    1. Explicit `resources_dir` and `resource_dirs` arguments.
    2. `FACTORY_RESOURCES_DIRS` / `FACTORY_RESOURCES_DIR` environment variables.
    3. Package-bundled resources (`get_bundled_resources_dir()`) when `include_bundled=True`.
    """
    resolved: list[Path] = []

    def _add_if_dir(path: Path) -> None:
        if path.is_dir() and path not in resolved:
            resolved.append(path)

    explicit_raw: list[str | Path] = []
    if resources_dir is not None and str(resources_dir).strip():
        explicit_raw.append(resources_dir)
    if resource_dirs:
        for item in resource_dirs:
            if str(item).strip():
                explicit_raw.append(item)

    for raw in explicit_raw:
        _add_if_dir(_expand_dir_candidate(raw, config_dir))

    if not explicit_raw:
        for raw in _parse_env_resource_dirs():
            _add_if_dir(_expand_dir_candidate(raw, config_dir))

    if include_bundled:
        _add_if_dir(get_bundled_resources_dir())

    return resolved


def resolve_resources_dir(
    resources_dir: str | Path | None = None,
    *,
    config_dir: Path | None = None,
) -> Path | None:
    """Resolve the primary external AI resources catalog directory (legacy single-dir helper)."""
    dirs = resolve_resource_dirs(resources_dir=resources_dir, config_dir=config_dir)
    return dirs[0] if dirs else None


def build_search_roots(
    *,
    workspace: Path | None = None,
    config_dir: Path | None = None,
    resources_dir: Path | None = None,
    resource_dirs: list[Path] | None = None,
    include_cwd: bool = False,
) -> list[Path]:
    """Build ordered search roots: config_dir -> workspace -> resource_dirs -> bundled."""
    search_roots: list[Path] = []

    def _append(path: Path) -> None:
        r = path.resolve()
        if r not in search_roots:
            search_roots.append(r)

    if config_dir is not None:
        _append(config_dir)
    if workspace is not None:
        _append(workspace)

    if resource_dirs is not None:
        for d in resource_dirs:
            if d.is_dir():
                _append(d)
    if resources_dir is not None and resources_dir.is_dir():
        _append(resources_dir)
    if resource_dirs is None and resources_dir is None:
        for d in resolve_resource_dirs(config_dir=config_dir):
            _append(d)

    if include_cwd:
        _append(Path.cwd())

    bundled = get_bundled_resources_dir()
    if bundled.is_dir():
        _append(bundled)

    return search_roots


def _find_case_insensitive_file(dir_path: Path, filename: str) -> Path | None:
    """Find a file inside `dir_path` matching `filename` case-insensitively."""
    if not dir_path.is_dir():
        return None
    lower_target = filename.lower()
    try:
        for entry in dir_path.iterdir():
            if entry.is_file() and entry.name.lower() == lower_target:
                return entry
    except OSError:
        return None
    return None


def load_markdown_or_inline(
    value: str,
    *,
    config_dir: Path | None = None,
    workspace: Path | None = None,
    resources_dir: Path | None = None,
    resource_dirs: list[Path] | None = None,
) -> str:
    """If `value` points to a Markdown/text file on disk, read it; otherwise return inline text."""
    stripped = value.strip()
    if "\n" in stripped:
        return value

    search_roots = build_search_roots(
        workspace=workspace,
        config_dir=config_dir,
        resources_dir=resources_dir,
        resource_dirs=resource_dirs,
        include_cwd=True,
    )

    candidate_path = Path(stripped).expanduser()
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
        for subdir in ("agents", "prompts", "personas"):
            sub_resolved = (root / subdir / candidate_path).resolve()
            if sub_resolved.is_file():
                return sub_resolved.read_text(encoding="utf-8")

    for root in search_roots:
        target_dir = (root / candidate_path.parent).resolve()
        matched = _find_case_insensitive_file(target_dir, candidate_path.name)
        if matched is not None:
            return matched.read_text(encoding="utf-8")
        for subdir in ("agents", "prompts", "personas"):
            sub_dir = (root / subdir / candidate_path.parent).resolve()
            matched = _find_case_insensitive_file(sub_dir, candidate_path.name)
            if matched is not None:
                return matched.read_text(encoding="utf-8")

    if stripped.endswith(".md"):
        raise FileNotFoundError(
            f"Prompt/persona Markdown file not found: '{stripped}' "
            f"(searched in {[str(r) for r in search_roots]})"
        )
    return value


def resolve_persona_text(
    persona: str | None,
    *,
    workspace: Path,
    config_dir: Path | None = None,
    resources_dir: Path | None = None,
    resource_dirs: list[Path] | None = None,
) -> str | None:
    """Resolve an agent persona string (inline text or path to a persona Markdown file)."""
    if not persona or not persona.strip():
        return None
    return load_markdown_or_inline(
        persona,
        config_dir=config_dir,
        workspace=workspace,
        resources_dir=resources_dir,
        resource_dirs=resource_dirs,
    ).strip()


def _iter_apm_lock_deployed_files(workspace: Path) -> list[Path]:
    """Return existing deployed file paths recorded in `apm.lock.yaml`, if present."""
    ws = workspace.resolve()
    lock_file = ws / "apm.lock.yaml"
    if not lock_file.is_file():
        return []
    try:
        parsed: Any = yaml.safe_load(lock_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(parsed, dict):
        return []

    deps = parsed.get("dependencies")
    if not isinstance(deps, list):
        return []

    deployed: list[Path] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        files = dep.get("deployed_files")
        if not isinstance(files, list):
            continue
        for rel in files:
            if not isinstance(rel, str) or not rel.strip():
                continue
            candidate = (ws / rel.strip()).resolve()
            if candidate.is_relative_to(ws) and candidate.is_file():
                deployed.append(candidate)
    return deployed


def discover_workspace_skills(workspace: Path) -> list[Path]:
    """Auto-discover APM/workspace installed SKILL.md files.

    Prefers exact `deployed_files` from `apm.lock.yaml` (target-agnostic), then falls
    back to standard skill directories and `apm_modules/`, deduplicating by skill name.
    """
    ws = workspace.resolve()
    discovered: list[Path] = []
    seen_paths: set[Path] = set()
    seen_names: set[str] = set()

    def _add(p: Path) -> None:
        rp = p.resolve()
        skill_name = p.parent.name
        if rp.is_file() and rp not in seen_paths and skill_name not in seen_names:
            seen_paths.add(rp)
            seen_names.add(skill_name)
            discovered.append(rp)

    for deployed in _iter_apm_lock_deployed_files(ws):
        if deployed.name == "SKILL.md":
            _add(deployed)

    for subdir in (".agents/skills", ".apm/skills"):
        base = ws / subdir
        if base.is_dir():
            for match in sorted(base.glob("*/SKILL.md")):
                _add(match)

    apm_dir = ws / "apm_modules"
    if apm_dir.is_dir():
        for match in sorted(apm_dir.glob("**/SKILL.md")):
            _add(match)

    return discovered


def discover_workspace_instructions(workspace: Path) -> list[Path]:
    """Auto-discover workspace and APM-installed instruction Markdown files.

    Prefers exact `deployed_files` from `apm.lock.yaml` (target-agnostic), then falls
    back to standard instruction directories and `apm_modules/`, deduplicating by filename.
    """
    ws = workspace.resolve()
    discovered: list[Path] = []
    seen_paths: set[Path] = set()
    seen_names: set[str] = set()

    def _add(p: Path) -> None:
        rp = p.resolve()
        if rp.is_file() and rp not in seen_paths and rp.name not in seen_names:
            seen_paths.add(rp)
            seen_names.add(rp.name)
            discovered.append(rp)

    for deployed in _iter_apm_lock_deployed_files(ws):
        if deployed.name.endswith(".instructions.md") or (
            deployed.parent.name == "instructions" and deployed.suffix == ".md"
        ):
            _add(deployed)

    gh_instr = ws / ".github" / "instructions"
    if gh_instr.is_dir():
        for match in sorted(gh_instr.glob("*.instructions.md")):
            _add(match)

    for subdir in (".agents/instructions", ".apm/instructions"):
        base = ws / subdir
        if base.is_dir():
            for match in sorted(base.glob("*.md")):
                _add(match)

    apm_dir = ws / "apm_modules"
    if apm_dir.is_dir():
        for match in sorted(apm_dir.glob("**/*.instructions.md")):
            _add(match)

    return discovered


def resolve_skill_paths(
    skills: list[str],
    *,
    workspace: Path,
    config_dir: Path | None = None,
    resources_dir: Path | None = None,
    resource_dirs: list[Path] | None = None,
    discover_apm: bool = False,
) -> list[Path]:
    """Resolve configured skill paths or APM skill identifiers to concrete SKILL.md Paths.

    Search order for each skill entry:
    1. Absolute or `~`-expanded path (file or directory containing SKILL.md)
    2. Relative to config_dir, workspace, resource_dirs, or bundled resources
    3. `.factory/skills/<skill>/SKILL.md`, `skills/<skill>/SKILL.md`, `.agents/skills/<skill>/SKILL.md`,
       `.apm/skills/<skill>/SKILL.md`, `apm_modules/<skill>/SKILL.md`, or nested under `apm_modules/`
    When `discover_apm=True`, also appends auto-discovered workspace/APM skills.
    """
    resolved_paths: list[Path] = []
    search_roots = build_search_roots(
        workspace=workspace,
        config_dir=config_dir,
        resources_dir=resources_dir,
        resource_dirs=resource_dirs,
        include_cwd=False,
    )
    # Also support legacy repo-root skills/ checkout if present
    bundled_repo_root = Path(__file__).resolve().parents[3]
    if (
        bundled_repo_root not in search_roots
        and (bundled_repo_root / "skills").is_dir()
    ):
        search_roots.append(bundled_repo_root)

    for entry in skills:
        raw = entry.strip()
        if not raw:
            continue

        candidate = Path(raw).expanduser()
        found: Path | None = None

        if candidate.is_absolute():
            if candidate.is_file():
                found = candidate.resolve()
            elif candidate.is_dir() and (candidate / "SKILL.md").is_file():
                found = (candidate / "SKILL.md").resolve()
        else:
            for root in search_roots:
                direct = (root / candidate).resolve()
                if direct.is_file():
                    found = direct
                    break
                if direct.is_dir() and (direct / "SKILL.md").is_file():
                    found = (direct / "SKILL.md").resolve()
                    break

                for subdir in (
                    ".factory/skills",
                    "skills",
                    ".agents/skills",
                    ".apm/skills",
                    "apm_modules",
                ):
                    skill_file = (root / subdir / raw / "SKILL.md").resolve()
                    if skill_file.is_file():
                        found = skill_file
                        break

                if found is None:
                    apm_dir = root / "apm_modules"
                    if apm_dir.is_dir():
                        matches = sorted(apm_dir.glob(f"**/skills/{raw}/SKILL.md"))
                        if not matches:
                            matches = sorted(apm_dir.glob(f"**/{raw}/SKILL.md"))
                        if matches:
                            found = matches[0].resolve()
                if found is not None:
                    break

        if found is None:
            raise FileNotFoundError(
                f"Skill '{raw}' not found. Expected a SKILL.md file path or skill package "
                f"in .factory/skills/, skills/, .agents/skills/, apm_modules/, or resource_dirs under {workspace}."
            )
        if found not in resolved_paths:
            resolved_paths.append(found)

    if discover_apm:
        for sp in discover_workspace_skills(workspace):
            if sp not in resolved_paths:
                resolved_paths.append(sp)

    return resolved_paths


def resolve_instruction_paths(
    instructions: list[str],
    *,
    workspace: Path,
    config_dir: Path | None = None,
    resources_dir: Path | None = None,
    resource_dirs: list[Path] | None = None,
    discover_apm: bool = False,
) -> list[Path]:
    """Resolve explicit instruction paths and optionally auto-discover workspace/APM instructions."""
    resolved_paths: list[Path] = []
    search_roots = build_search_roots(
        workspace=workspace,
        config_dir=config_dir,
        resources_dir=resources_dir,
        resource_dirs=resource_dirs,
        include_cwd=False,
    )

    for entry in instructions:
        raw = entry.strip()
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        found: Path | None = None

        if candidate.is_absolute():
            if candidate.is_file():
                found = candidate.resolve()
        else:
            for root in search_roots:
                direct = (root / candidate).resolve()
                if direct.is_file():
                    found = direct
                    break
                for subdir in (
                    ".factory/instructions",
                    "instructions",
                    ".github/instructions",
                    ".agents/instructions",
                    ".apm/instructions",
                ):
                    for suffix_name in (raw, f"{raw}.md", f"{raw}.instructions.md"):
                        sub_file = (root / subdir / suffix_name).resolve()
                        if sub_file.is_file():
                            found = sub_file
                            break
                    if found is not None:
                        break
                if found is not None:
                    break

        if found is None:
            raise FileNotFoundError(
                f"Instruction file '{raw}' not found under {workspace} or configured resource_dirs."
            )
        if found not in resolved_paths:
            resolved_paths.append(found)

    if discover_apm:
        for ip in discover_workspace_instructions(workspace):
            if ip not in resolved_paths:
                resolved_paths.append(ip)

    return resolved_paths


def parse_frontmatter_summary(md_path: Path) -> dict[str, str]:
    """Extract lightweight YAML frontmatter metadata (`name`, `description`, `applyTo`) from Markdown."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return {}

    end_idx: int | None = None
    for idx in range(1, min(len(lines), 40)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}

    yaml_block = "\n".join(lines[1:end_idx])
    try:
        parsed: Any = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        return {}

    if not isinstance(parsed, dict):
        return {}

    summary: dict[str, str] = {}
    for key in ("name", "description", "applyTo"):
        val = parsed.get(key)
        if isinstance(val, str) and val.strip():
            summary[key] = " ".join(val.strip().split())
    return summary


def resolve_resources(
    *,
    persona: str | None = None,
    instructions: list[str] | None = None,
    skills: list[str] | None = None,
    workspace: Path,
    config_dir: Path | None = None,
    resources_dir: Path | None = None,
    resource_dirs: list[Path] | None = None,
    discover_apm: bool = True,
) -> ResolvedResources:
    """Resolve persona, instructions, and skills in a single unified pass."""
    effective_dirs = (
        list(resource_dirs)
        if resource_dirs is not None
        else resolve_resource_dirs(resources_dir=resources_dir, config_dir=config_dir)
    )
    persona_text = resolve_persona_text(
        persona,
        workspace=workspace,
        config_dir=config_dir,
        resources_dir=resources_dir,
        resource_dirs=effective_dirs,
    )
    resolved_instructions = resolve_instruction_paths(
        list(instructions or []),
        workspace=workspace,
        config_dir=config_dir,
        resources_dir=resources_dir,
        resource_dirs=effective_dirs,
        discover_apm=discover_apm,
    )
    explicit_skills = resolve_skill_paths(
        list(skills or []),
        workspace=workspace,
        config_dir=config_dir,
        resources_dir=resources_dir,
        resource_dirs=effective_dirs,
        discover_apm=False,
    )
    discovered_skills: list[Path] = []
    if discover_apm:
        for sp in discover_workspace_skills(workspace):
            if sp not in explicit_skills and sp not in discovered_skills:
                discovered_skills.append(sp)
    resolved_skills = [*explicit_skills, *discovered_skills]
    return ResolvedResources(
        persona_text=persona_text,
        instructions=resolved_instructions,
        skills=resolved_skills,
        resource_dirs=effective_dirs,
        explicit_skills=explicit_skills,
        discovered_skills=discovered_skills,
    )
