"""Bundled orchestration resources and unified AI resource resolution."""

from .resolver import (
    ResolvedResources,
    build_search_roots,
    discover_workspace_instructions,
    discover_workspace_skills,
    get_bundled_resources_dir,
    load_bundled_text,
    load_markdown_or_inline,
    parse_frontmatter_summary,
    resolve_instruction_paths,
    resolve_persona_text,
    resolve_resource_dirs,
    resolve_resources,
    resolve_resources_dir,
    resolve_skill_paths,
)

__all__ = [
    "ResolvedResources",
    "build_search_roots",
    "discover_workspace_instructions",
    "discover_workspace_skills",
    "get_bundled_resources_dir",
    "load_bundled_text",
    "load_markdown_or_inline",
    "parse_frontmatter_summary",
    "resolve_instruction_paths",
    "resolve_persona_text",
    "resolve_resource_dirs",
    "resolve_resources",
    "resolve_resources_dir",
    "resolve_skill_paths",
]
