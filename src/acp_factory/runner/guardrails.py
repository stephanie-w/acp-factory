"""Security guardrails for ACP agent execution: workspace path containment and command filters."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Built-in patterns for catastrophic, unrecoverable, or unauthorized host operations.
# Modeled after the headless hard-block rules in coding-ai-resources/pi-extensions/bash-guard.
DANGEROUS_COMMAND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b(sudo|su|doas)\b", re.IGNORECASE),
        "Elevated privileges (sudo/su/doas) are strictly disallowed",
    ),
    (
        re.compile(
            r"\b(rm|rmdir|unlink)\b\s+.*-(?:[a-zA-Z]*r[a-zA-Z]*f|f[a-zA-Z]*r)\b",
            re.IGNORECASE,
        ),
        "Forced recursive deletion (rm -rf) is blocked",
    ),
    (
        re.compile(r"\b(rm|rmdir|unlink)\b\s+.*-[a-zA-Z]*r\b", re.IGNORECASE),
        "Recursive file deletion (rm -r) is blocked",
    ),
    (
        re.compile(
            r"\b(curl|wget)\b.*\|\s*(?:ba|z|da|a)?sh\b",
            re.IGNORECASE,
        ),
        "Piping remote scripts into a shell (curl/wget | sh) is blocked",
    ),
    (
        re.compile(
            r"\b(mkfs(?:\.[a-zA-Z0-9]+)?|newfs_[a-zA-Z0-9]+|wipefs|parted|fdisk|gdisk|sgdisk|cryptsetup)\b",
            re.IGNORECASE,
        ),
        "Filesystem/disk partition formatting commands are blocked",
    ),
    (
        re.compile(r"\bdd\b.*(?:of\s*=\s*/dev/|of=/dev/)", re.IGNORECASE),
        "Direct raw device overwrite (dd of=/dev/...) is blocked",
    ),
    (
        re.compile(r"\bgit\s+push\b", re.IGNORECASE),
        "Git push is reserved for human/orchestrator control and blocked for agents",
    ),
    (
        re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
        "Destructive git reset --hard is blocked",
    ),
    (
        re.compile(
            r"\bgit\s+clean\s+.*-(?:[a-zA-Z]*f[a-zA-Z]*|force)\b",
            re.IGNORECASE,
        ),
        "Forced git clean is blocked",
    ),
    (
        re.compile(
            r"\b(shutdown|reboot|poweroff|init\s+0)\b",
            re.IGNORECASE,
        ),
        "System shutdown or reboot commands are blocked",
    ),
    (
        re.compile(
            r"(?:>|>>)\s*(?:/dev/sd[a-z]|/dev/nvme|/etc/|/boot/|/root/|~?/\.ssh/|~?/\.aws/|~?/\.gnupg/|~?/\.config/gh/|~?/\.config/gcloud/)",
            re.IGNORECASE,
        ),
        "Output redirection targeting system or sensitive host credential paths is blocked",
    ),
]

SENSITIVE_HOST_PATHS: list[re.Pattern[str]] = [
    re.compile(
        r"(?:/etc/(?:shadow|passwd|sudoers)|~?/\.(?:ssh|aws|gnupg|kube|docker/config\.json|config/gh|config/gcloud))",
        re.IGNORECASE,
    ),
]


def is_path_contained(
    target_path: str | Path,
    workspace: str | Path,
    *,
    allowed_paths: list[str | Path] | None = None,
    is_read: bool = False,
    resources_dir: str | Path | None = None,
) -> tuple[bool, str | None]:
    """Verify that a target file path is contained within the workspace or explicitly permitted paths."""
    if not target_path:
        return True, None

    ws_resolved = Path(workspace).expanduser().resolve()
    target_str = str(target_path).strip()
    if not target_str:
        return True, None

    target = Path(target_str).expanduser()
    if not target.is_absolute():
        target = ws_resolved / target

    try:
        target_resolved = target.resolve()
    except (OSError, RuntimeError) as exc:
        return False, f"Invalid target path '{target_path}': {exc}"

    # 1. Target is inside current workspace
    if target_resolved == ws_resolved or target_resolved.is_relative_to(ws_resolved):
        return True, None

    # 2. Reading from external resources catalog (e.g. skills or prompts)
    if is_read and resources_dir:
        res_resolved = Path(resources_dir).expanduser().resolve()
        if target_resolved == res_resolved or target_resolved.is_relative_to(
            res_resolved
        ):
            return True, None

    # 3. Explicitly allowed paths from configuration
    if allowed_paths:
        for allowed in allowed_paths:
            allowed_res = Path(allowed).expanduser().resolve()
            if target_resolved == allowed_res or target_resolved.is_relative_to(
                allowed_res
            ):
                return True, None

    return False, f"Target path '{target_path}' escapes workspace '{workspace}'"


def is_command_safe(
    command: str,
    *,
    custom_denied: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Verify that a shell command does not match dangerous or unauthorized patterns."""
    cmd_str = command.strip()
    if not cmd_str:
        return True, None

    for pattern, reason in DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(cmd_str):
            return False, f"Command blocked by guardrail: {reason} ('{cmd_str}')"

    for pattern in SENSITIVE_HOST_PATHS:
        if pattern.search(cmd_str):
            return False, f"Command targets sensitive host path: '{cmd_str}'"

    if custom_denied:
        for denied in custom_denied:
            denied_clean = denied.strip()
            if denied_clean and denied_clean.lower() in cmd_str.lower():
                return (
                    False,
                    f"Command matched custom denied pattern '{denied_clean}': '{cmd_str}'",
                )

    return True, None


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint invoked by acpterm permission hook."""
    parser = argparse.ArgumentParser(
        description="acp-factory permission hook guardrails"
    )
    parser.add_argument(
        "--resources-dir", default=None, help="Path to external resources dir"
    )
    parser.add_argument(
        "--allow-path", action="append", default=[], help="Allowed external path"
    )
    parser.add_argument(
        "--deny-cmd", action="append", default=[], help="Custom denied command pattern"
    )
    args = parser.parse_args(argv)

    kind = os.environ.get("ACPTERM_PERMISSION_KIND", "").strip().lower()
    title = os.environ.get("ACPTERM_PERMISSION_TITLE", "").strip()
    path = os.environ.get("ACPTERM_PERMISSION_PATH", "").strip()
    cwd = os.environ.get("ACPTERM_CWD", "").strip() or str(Path.cwd())
    res_dir = (
        args.resources_dir
        or os.environ.get("FACTORY_RESOURCES_DIR", "").strip()
        or None
    )

    allowed_paths = list(args.allow_path)
    allowed_raw = os.environ.get("FACTORY_GUARDRAILS_ALLOWED_PATHS", "")
    if allowed_raw:
        try:
            allowed_paths.extend(json.loads(allowed_raw))
        except Exception:
            pass

    custom_denied = list(args.deny_cmd)
    denied_raw = os.environ.get("FACTORY_GUARDRAILS_DENIED_COMMANDS", "")
    if denied_raw:
        try:
            custom_denied.extend(json.loads(denied_raw))
        except Exception:
            pass

    is_read = kind in ("read", "view")

    # 1. Path containment check
    if path:
        safe_path, path_reason = is_path_contained(
            path,
            cwd,
            allowed_paths=allowed_paths,
            is_read=is_read,
            resources_dir=res_dir,
        )
        if not safe_path:
            print(f"[guardrails] DENIED: {path_reason}", file=sys.stderr)
            sys.exit(1)

    # 2. Command safety check
    if kind in ("execute", "command", "bash") or (not kind and title):
        cmd_candidate = (
            path if kind in ("execute", "command", "bash") and path else title
        )
        safe_cmd, cmd_reason = is_command_safe(
            cmd_candidate, custom_denied=custom_denied
        )
        if not safe_cmd:
            print(f"[guardrails] DENIED: {cmd_reason}", file=sys.stderr)
            sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
