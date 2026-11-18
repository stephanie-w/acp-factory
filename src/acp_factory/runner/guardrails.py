"""Security guardrails for ACP agent execution: workspace path containment and command filters."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import sys
from collections.abc import Sequence
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
    (
        re.compile(r"\bgh\s+auth\b", re.IGNORECASE),
        "GitHub auth commands (gh auth) are blocked for agents",
    ),
]

SENSITIVE_HOST_PATHS: list[re.Pattern[str]] = [
    re.compile(
        r"(?:/etc/(?:shadow|passwd|sudoers)|~?/\.(?:ssh|aws|gnupg|kube|docker/config\.json|config/gh|config/gcloud))",
        re.IGNORECASE,
    ),
]

_RUN_ARTIFACTS_CMD_RE = re.compile(
    r"(?:^|[\s/'\"=])\.factory/(?:runs(?:/|\b)|latest-(?:report|build-notes)\.md\b)",
    re.IGNORECASE,
)

_SAFE_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})

_ENV_FILE_CMD_RE = re.compile(
    r"(?:^|[\s/'\"=])\.env(?:\.(?!example\b|sample\b|template\b)[a-zA-Z0-9_.-]+)?(?=$|[\s'\";|&>])"
)

_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON_NAME_RE = re.compile(r"(?:python|pythonw|pypy)\d*(?:\.\d+)*$")
_SHELL_WRAPPERS = frozenset({"sh", "bash", "zsh", "dash", "ash", "ksh", "fish"})
_SHELL_CONTROL_CHARS = frozenset("|&;()")
_COMMAND_WRAPPERS = frozenset(
    {"eval", "command", "builtin", "time", "exec", "nice", "nohup", "xargs", "busybox"}
)
_WRAPPER_OPTIONS_WITH_VALUE: dict[str, frozenset[str]] = {
    "nice": frozenset({"-n", "--adjustment"}),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "exec": frozenset({"-a"}),
    "xargs": frozenset(
        {
            "-a",
            "--arg-file",
            "-d",
            "--delimiter",
            "-E",
            "--eof",
            "-I",
            "--replace",
            "-L",
            "--max-lines",
            "-n",
            "--max-args",
            "-P",
            "--max-procs",
            "-s",
            "--max-chars",
        }
    ),
}
_ENV_OPTIONS_WITH_VALUE = frozenset({"-u", "--unset", "-C", "--chdir"})
_ENV_OPTIONS_WITH_STRING = frozenset({"-S", "--split-string"})
_ENV_INLINE_STRING_PREFIXES = ("--unset=", "--chdir=", "--split-string=")
_UV_RUN_OPTIONS_WITH_VALUE = frozenset(
    {
        "-p",
        "--python",
        "-w",
        "--with",
        "--group",
        "--project",
        "--directory",
        "--env-file",
        "--extra",
        "--index",
        "--index-url",
        "--reinstall-package",
        "--resolution",
        "--prerelease",
        "--exclude-newer",
    }
)


def _is_assignment(arg: str) -> bool:
    """Return True for shell ``NAME=value`` environment assignments."""
    return _ASSIGNMENT_RE.match(arg) is not None


def _is_quote_noise(arg: str) -> bool:
    """Return True for fragments left behind by malformed or unbalanced quoting."""
    return bool(arg) and all(char in "'\"" for char in arg)


def _command_name(arg: str) -> str:
    """Return the lowercase basename of an executable argument."""
    return arg.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _environment_dump_reason(tool: str) -> str:
    return f"Environment-dumping command '{tool}' is blocked"


def _tokenize_command(cmd_str: str) -> list[str]:
    """Tokenize a shell command, falling back to whitespace splitting on bad quoting."""
    try:
        lexer = shlex.shlex(cmd_str, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return cmd_str.split()


def _is_redirection(arg: str) -> bool:
    """Return True for shell redirection operators such as ``>``, ``>>``, or ``2>&1``."""
    return (
        bool(arg)
        and any(char in "<>" for char in arg)
        and all(char in "<>&|" for char in arg)
    )


def _strip_redirections(tokens: Sequence[str]) -> list[str]:
    """Drop redirection operators and destinations so filenames are not read as commands."""
    cleaned: list[str] = []
    index = 0
    while index < len(tokens):
        arg = tokens[index]
        if (
            arg.isdigit()
            and index + 1 < len(tokens)
            and _is_redirection(tokens[index + 1])
        ):
            index += 2
        elif _is_redirection(arg):
            index += 1
        else:
            cleaned.append(arg)
            index += 1
            continue
        if index < len(tokens):
            index += 1
    return cleaned


def _is_shell_separator(arg: str) -> bool:
    """Return True for control operators, including process-substitution openers."""
    if not arg:
        return False
    if arg.endswith("<(") or arg.endswith(">("):
        return True
    return all(char in _SHELL_CONTROL_CHARS for char in arg)


def _iter_command_segments(tokens: Sequence[str]) -> list[list[str]]:
    """Split tokens into simple-command segments at shell control operators."""
    segments: list[list[str]] = []
    segment: list[str] = []
    for arg in tokens:
        if _is_shell_separator(arg):
            if segment:
                segments.append(segment)
                segment = []
            continue
        segment.append(arg)
    if segment:
        segments.append(segment)
    return segments


def _check_env_invocation(args: Sequence[str]) -> str | None:
    """Block ``env`` invocations that dump the environment instead of launching a command."""
    index = 0
    while index < len(args):
        arg = args[index]
        if _is_quote_noise(arg) or arg == "--":
            index += 1
            continue
        if arg in _ENV_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if arg in _ENV_OPTIONS_WITH_STRING:
            if index + 1 >= len(args):
                return _environment_dump_reason("env")
            return _check_env_dump_command(args[index + 1])
        if arg.startswith(_ENV_INLINE_STRING_PREFIXES):
            if arg.startswith("--split-string="):
                return _check_env_dump_command(arg.split("=", 1)[1])
            index += 1
            continue
        if arg.startswith("-") and arg != "-":
            index += 1
            continue
        if _is_assignment(arg):
            index += 1
            continue
        return _check_command_tokens(args[index:])
    return _environment_dump_reason("env")


def _check_printenv_invocation(args: Sequence[str]) -> str | None:
    """Block ``printenv`` unless it queries specific variables."""
    variables = 0
    for arg in args:
        if arg in ("--help", "--version"):
            return None
        if arg == "--" or _is_quote_noise(arg):
            continue
        if arg.startswith("-") and arg != "-":
            continue
        variables += 1
    if variables == 0:
        return _environment_dump_reason("printenv")
    return None


def _check_export_invocation(args: Sequence[str]) -> str | None:
    """Block ``export``/``export -p`` forms that print the environment."""
    if any(arg in ("-p", "--print") for arg in args):
        return _environment_dump_reason("export")
    for arg in args:
        if arg == "--" or _is_quote_noise(arg):
            continue
        if arg.startswith("-") and arg != "-":
            continue
        return None
    return _environment_dump_reason("export")


def _python_code_dumps_environ(code: str) -> bool:
    """Return True when inline Python dumps ``os.environ``/``os.environb``.

    Single-variable reads via ``os.environ.get(...)`` or ``os.environ[...]`` are
    allowed, mirroring the permitted ``printenv USER`` query form. Malformed code
    falls back to a conservative substring check.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "os.environ" in code

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Attribute)
            and node.attr in ("environ", "environb")
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        ):
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Attribute) and parent.attr == "get":
            continue
        if isinstance(parent, ast.Subscript) and parent.value is node:
            continue
        return True
    return False


def _check_python_inline(tokens: Sequence[str], interpreter_index: int) -> str | None:
    """Block inline Python ``-c`` probes that dump ``os.environ``/``os.environb``."""
    index = interpreter_index + 1
    while index < len(tokens):
        arg = tokens[index]
        if arg == "-c":
            code = " ".join(tokens[index + 1 :])
            if _python_code_dumps_environ(code):
                return "Inline Python environment dump via 'os.environ' is blocked"
            return None
        if arg == "-m" or not arg.startswith("-"):
            return None
        if arg in ("-W", "-X"):
            index += 2
            continue
        index += 1
    return None


def _uv_python_index(tokens: Sequence[str], uv_index: int) -> int | None:
    """Return the token index of the Python interpreter passed to ``uv run``."""
    args = tokens[uv_index + 1 :]
    if not args or args[0] != "run":
        return None
    index = 1
    while index < len(args):
        arg = args[index]
        if arg in _UV_RUN_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        break
    if index < len(args) and _PYTHON_NAME_RE.fullmatch(_command_name(args[index])):
        return uv_index + 1 + index
    return None


def _check_shell_wrapper(args: Sequence[str]) -> str | None:
    """Inspect the command string passed to a shell wrapper's ``-c`` option."""
    for index, arg in enumerate(args):
        if arg == "-c":
            if index + 1 < len(args):
                return _check_env_dump_command(args[index + 1])
            return None
        if not arg.startswith("-"):
            return None
    return None


def _check_command_wrapper(name: str, args: Sequence[str]) -> str | None:
    """Inspect the command wrapped by a shell keyword or prefix launcher."""
    if name == "eval":
        if not args:
            return None
        return _check_env_dump_command(" ".join(args))
    if name == "command":
        # ``-v``/``-V`` only resolve how a name would be interpreted.
        for index, arg in enumerate(args):
            if arg in ("-v", "-V"):
                return None
            if arg == "--":
                return _check_command_tokens(args[index + 1 :])
            if arg.startswith("-") and arg != "-":
                continue
            return _check_command_tokens(args[index:])
        return None

    options_with_value = _WRAPPER_OPTIONS_WITH_VALUE.get(name, frozenset())
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if arg in options_with_value:
            index += 2
            continue
        if arg.startswith("-") and arg != "-":
            index += 1
            continue
        break
    if index < len(args):
        return _check_command_tokens(args[index:])
    return None


def _check_command_tokens(tokens: Sequence[str]) -> str | None:
    """Inspect a single simple command for environment-dumping invocations."""
    index = 0
    while index < len(tokens) and _is_assignment(tokens[index]):
        index += 1
    if index >= len(tokens):
        return None

    name = _command_name(tokens[index])
    args = tokens[index + 1 :]
    if name == "env":
        return _check_env_invocation(args)
    if name == "printenv":
        return _check_printenv_invocation(args)
    if name == "export":
        return _check_export_invocation(args)
    if _PYTHON_NAME_RE.fullmatch(name):
        return _check_python_inline(tokens, index)
    if name == "uv":
        python_index = _uv_python_index(tokens, index)
        if python_index is not None:
            return _check_python_inline(tokens, python_index)
        return None
    if name in _SHELL_WRAPPERS:
        return _check_shell_wrapper(args)
    if name in _COMMAND_WRAPPERS:
        return _check_command_wrapper(name, args)
    return None


def _check_env_dump_command(cmd_str: str) -> str | None:
    """Return a denial reason when a command dumps the process environment."""
    tokens = _strip_redirections(_tokenize_command(cmd_str))
    for segment in _iter_command_segments(tokens):
        reason = _check_command_tokens(segment)
        if reason:
            return reason
    return None


def _is_env_secret_path(target_resolved: Path) -> bool:
    """Return True if target_resolved is a .env secret file (excluding safe templates)."""
    name = target_resolved.name
    if name in _SAFE_ENV_TEMPLATES:
        return False
    return name == ".env" or name.startswith(".env.")


def _is_run_artifact_path(target_resolved: Path, ws_resolved: Path) -> bool:
    """Return True if target_resolved points inside .factory/runs or .factory/latest-* pointers."""
    factory_dir = ws_resolved / ".factory"
    runs_dir = factory_dir / "runs"
    if target_resolved == runs_dir or target_resolved.is_relative_to(runs_dir):
        return True
    return target_resolved in (
        factory_dir / "latest-report.md",
        factory_dir / "latest-build-notes.md",
    )


def is_path_contained(
    target_path: str | Path,
    workspace: str | Path,
    *,
    allowed_paths: list[str | Path] | None = None,
    is_read: bool = False,
    resources_dir: str | Path | None = None,
    resource_dirs: Sequence[str | Path] | None = None,
    deny_runs: bool = False,
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

    if _is_env_secret_path(target_resolved):
        return (
            False,
            f"Target path '{target_path}' references a '.env' file, which is blocked by guardrails",
        )

    # 1. Target is inside current workspace
    if target_resolved == ws_resolved or target_resolved.is_relative_to(ws_resolved):
        if deny_runs and _is_run_artifact_path(target_resolved, ws_resolved):
            return (
                False,
                f"Target path '{target_path}' references '.factory/runs' or run artifacts, which are restricted for this stage",
            )
        return True, None

    # 2. Reading from external or bundled resources catalogs (e.g. skills, instructions, or prompts)
    if is_read:
        from ..resources import get_bundled_resources_dir

        read_roots: list[str | Path] = []
        if resources_dir:
            read_roots.append(resources_dir)
        if resource_dirs:
            read_roots.extend(resource_dirs)
        read_roots.append(get_bundled_resources_dir())

        for raw_root in read_roots:
            res_resolved = Path(raw_root).expanduser().resolve()
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
    deny_runs: bool = False,
) -> tuple[bool, str | None]:
    """Verify that a shell command does not match dangerous or unauthorized patterns."""
    cmd_str = command.strip()
    if not cmd_str:
        return True, None

    if deny_runs and _RUN_ARTIFACTS_CMD_RE.search(cmd_str):
        return (
            False,
            f"Command references '.factory/runs' or run artifacts, which are restricted for this stage: '{cmd_str}'",
        )

    if _ENV_FILE_CMD_RE.search(cmd_str):
        return (
            False,
            f"Command targets a '.env' file, which is blocked by guardrails: '{cmd_str}'",
        )

    for pattern, reason in DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(cmd_str):
            return False, f"Command blocked by guardrail: {reason} ('{cmd_str}')"

    for pattern in SENSITIVE_HOST_PATHS:
        if pattern.search(cmd_str):
            return False, f"Command targets sensitive host path: '{cmd_str}'"

    env_dump_reason = _check_env_dump_command(cmd_str)
    if env_dump_reason:
        return False, f"Command blocked by guardrail: {env_dump_reason} ('{cmd_str}')"

    if custom_denied:
        for denied in custom_denied:
            denied_clean = denied.strip()
            if denied_clean and denied_clean.lower() in cmd_str.lower():
                return (
                    False,
                    f"Command matched custom denied pattern '{denied_clean}': '{cmd_str}'",
                )

    return True, None


def _parse_env_json_list(env_var: str) -> list[str]:
    """Parse a JSON list from *env_var*, warning to stderr if invalid."""
    raw = os.environ.get(env_var, "")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"guardrails: ignoring invalid JSON in {env_var}: {exc}",
            file=sys.stderr,
        )
        return []
    if not isinstance(parsed, list):
        print(
            f"guardrails: ignoring non-list value in {env_var}",
            file=sys.stderr,
        )
        return []
    return [str(item) for item in parsed]


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint invoked by acpterm permission hook."""
    parser = argparse.ArgumentParser(
        description="acp-factory permission hook guardrails"
    )
    parser.add_argument(
        "--resources-dir",
        action="append",
        default=[],
        help="Path to external resources dir (repeatable)",
    )
    parser.add_argument(
        "--allow-path", action="append", default=[], help="Allowed external path"
    )
    parser.add_argument(
        "--deny-cmd", action="append", default=[], help="Custom denied command pattern"
    )
    parser.add_argument(
        "--deny-runs",
        action="store_true",
        default=False,
        help="Block access to .factory/runs and .factory/latest-* run artifacts",
    )
    args = parser.parse_args(argv)

    kind = os.environ.get("ACPTERM_PERMISSION_KIND", "").strip().lower()
    title = os.environ.get("ACPTERM_PERMISSION_TITLE", "").strip()
    path = os.environ.get("ACPTERM_PERMISSION_PATH", "").strip()
    cwd = os.environ.get("ACPTERM_CWD", "").strip() or str(Path.cwd())
    res_dirs: list[str] = list(args.resources_dir)
    if not res_dirs:
        from ..resources.resolver import _parse_env_resource_dirs

        res_dirs.extend(_parse_env_resource_dirs())

    allowed_paths = [
        *args.allow_path,
        *_parse_env_json_list("FACTORY_GUARDRAILS_ALLOWED_PATHS"),
    ]
    custom_denied = [
        *args.deny_cmd,
        *_parse_env_json_list("FACTORY_GUARDRAILS_DENIED_COMMANDS"),
    ]

    is_read = kind in ("read", "view")

    # 1. Path containment check
    if path:
        safe_path, path_reason = is_path_contained(
            path,
            cwd,
            allowed_paths=allowed_paths,
            is_read=is_read,
            resource_dirs=res_dirs,
            deny_runs=args.deny_runs,
        )
        if not safe_path:
            print(f"[guardrails] DENIED: {path_reason}", file=sys.stderr)
            sys.exit(1)
    elif title.lower() == "external_directory":
        print(
            "[guardrails] DENIED: External directory access without an explicit allowed path is blocked by guardrails; use workspace-relative paths.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 2. Command safety check
    if kind in ("execute", "command", "bash") or (not kind and title):
        cmd_candidate = (
            path if kind in ("execute", "command", "bash") and path else title
        )
        safe_cmd, cmd_reason = is_command_safe(
            cmd_candidate, custom_denied=custom_denied, deny_runs=args.deny_runs
        )
        if not safe_cmd:
            print(f"[guardrails] DENIED: {cmd_reason}", file=sys.stderr)
            sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
