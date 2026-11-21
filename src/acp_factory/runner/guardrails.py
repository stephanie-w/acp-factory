"""Security guardrails for ACP agent execution: workspace path containment and command filters."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
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

# Generated lockfiles are derived artifacts: agents should inspect the manifest
# that declares dependencies instead of dumping the resolved lock graph.
GENERATED_LOCKFILE_MANIFESTS: dict[str, str] = {
    "uv.lock": "pyproject.toml",
    "poetry.lock": "pyproject.toml",
    "package-lock.json": "package.json",
    "pnpm-lock.yaml": "package.json",
    "yarn.lock": "package.json",
    "Cargo.lock": "Cargo.toml",
}

# Pure cache/vendor directories whose contents are never useful source context.
# `.venv` is intentionally absent so agents can inspect installed dependencies.
NOISY_DIRECTORIES: frozenset[str] = frozenset(
    {
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".ty_cache",
        ".tox",
        ".nox",
    }
)

_FILE_DUMP_COMMANDS: frozenset[str] = frozenset({"cat", "head", "tail", "less", "more"})
_GREP_COMMANDS: frozenset[str] = frozenset({"grep", "egrep", "fgrep"})
_BROAD_FIND_ROOTS: frozenset[str] = frozenset({".", "./", "$PWD", "${PWD}"})
# Short/long grep options that consume a value. They must be skipped so a value
# such as `-r` passed to `-e` is not mistaken for recursive traversal.
_GREP_SHORT_OPTIONS_WITH_VALUE: frozenset[str] = frozenset(
    {"e", "f", "m", "A", "B", "C", "d", "D"}
)
_GREP_LONG_OPTIONS_WITH_VALUE: frozenset[str] = frozenset(
    {
        "--regexp",
        "--file",
        "--max-count",
        "--after-context",
        "--before-context",
        "--context",
        "--directories",
        "--devices",
        "--label",
        "--binary-files",
        "--exclude",
        "--exclude-from",
        "--exclude-dir",
        "--include",
    }
)


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


def _split_command_text(cmd_str: str) -> list[list[str]]:
    """Tokenize a shell string into simple-command segments without redirections."""
    return _iter_command_segments(_strip_redirections(_tokenize_command(cmd_str)))


def _shell_wrapper_payload(args: Sequence[str]) -> str | None:
    """Return the command string passed to a shell wrapper's ``-c`` option."""
    for index, arg in enumerate(args):
        if arg == "-c":
            return args[index + 1] if index + 1 < len(args) else None
        if not arg.startswith("-"):
            return None
    return None


def _wrapper_payload_segments(name: str, args: Sequence[str]) -> list[list[str]] | None:
    """Return the simple commands wrapped by a shell keyword or prefix launcher."""
    if name == "eval":
        return _split_command_text(" ".join(args)) if args else None
    if name == "command":
        # ``-v``/``-V`` only resolve how a name would be interpreted.
        for index, arg in enumerate(args):
            if arg in ("-v", "-V"):
                return None
            if arg == "--":
                return [list(args[index + 1 :])] if index + 1 < len(args) else None
            if arg.startswith("-") and arg != "-":
                continue
            return [list(args[index:])]
        return None
    if name == "env":
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
                    return None
                return _split_command_text(args[index + 1])
            if arg.startswith(_ENV_INLINE_STRING_PREFIXES):
                if arg.startswith("--split-string="):
                    return _split_command_text(arg.split("=", 1)[1])
                index += 1
                continue
            if (arg.startswith("-") and arg != "-") or _is_assignment(arg):
                index += 1
                continue
            return [list(args[index:])]
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
    return [list(args[index:])] if index < len(args) else None


def _unwrap_command_segments(
    segments: Sequence[Sequence[str]], depth: int = 0
) -> list[list[str]]:
    """Return leaf simple commands after descending every wrapper prefix."""
    leaves: list[list[str]] = []
    for segment in segments:
        if depth > 6:
            continue
        index = 0
        while index < len(segment) and _is_assignment(segment[index]):
            index += 1
        if index >= len(segment):
            continue
        tokens = list(segment[index:])
        name = _command_name(tokens[0])
        args = tokens[1:]
        if name in _SHELL_WRAPPERS:
            payload = _shell_wrapper_payload(args)
            if payload is not None:
                leaves.extend(
                    _unwrap_command_segments(
                        _split_command_text(payload), depth=depth + 1
                    )
                )
            continue
        if name == "env" or name in _COMMAND_WRAPPERS:
            wrapped = _wrapper_payload_segments(name, args)
            if wrapped is not None:
                leaves.extend(_unwrap_command_segments(wrapped, depth=depth + 1))
            elif name == "env":
                # A bare ``env`` is itself a dump candidate, not a launcher.
                leaves.append(tokens)
            continue
        leaves.append(tokens)
    return leaves


def _iter_unwrapped_commands(cmd_str: str) -> list[list[str]]:
    """Return every leaf simple command in *cmd_str*, unwrapping shell prefixes."""
    return _unwrap_command_segments(_split_command_text(cmd_str))


def _check_env_dump_tokens(tokens: Sequence[str]) -> str | None:
    """Inspect one already-unwrapped simple command for environment dumping."""
    index = 0
    while index < len(tokens) and _is_assignment(tokens[index]):
        index += 1
    if index >= len(tokens):
        return None

    name = _command_name(tokens[index])
    args = tokens[index + 1 :]
    if name == "env":
        return _environment_dump_reason("env")
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


def _check_env_dump_command(cmd_str: str) -> str | None:
    """Return a denial reason when a command dumps the process environment."""
    for tokens in _iter_unwrapped_commands(cmd_str):
        reason = _check_env_dump_tokens(tokens)
        if reason:
            return reason
    return None


def _is_env_secret_path(target_resolved: Path) -> bool:
    """Return True if target_resolved is a .env secret file (excluding safe templates)."""
    name = target_resolved.name
    if name in _SAFE_ENV_TEMPLATES:
        return False
    return name == ".env" or name.startswith(".env.")


_SPILL_DIR_PREFIX = "acp-factory-spill-"


def _is_spill_path(target_resolved: Path) -> bool:
    """Return True if *target_resolved* is inside an ``acp-factory-spill-*`` temp dir.

    Only direct children of the system temp directory with that prefix qualify,
    so arbitrary temp paths and look-alike directories elsewhere stay blocked.
    """
    try:
        temp_root = Path(tempfile.gettempdir()).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    if not target_resolved.is_relative_to(temp_root):
        return False
    relative = target_resolved.relative_to(temp_root)
    return bool(relative.parts) and relative.parts[0].startswith(_SPILL_DIR_PREFIX)


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


def _path_parts(path_text: str) -> tuple[str, ...]:
    """Return normalized path components without ``.`` or empty segments."""
    try:
        normalized = os.path.normpath(path_text)
    except (TypeError, ValueError):
        return ()
    return tuple(part for part in Path(normalized).parts if part not in ("", "."))


def _noisy_directory_in_parts(parts: Sequence[str]) -> str | None:
    """Return the first blocked cache/vendor directory component in *parts*."""
    for index, part in enumerate(parts):
        if part in NOISY_DIRECTORIES:
            return part
        if part == ".git" and "objects" in parts[index + 1 :]:
            return ".git/objects"
    return None


def _lockfile_reason(target: str, name: str) -> str:
    manifest = GENERATED_LOCKFILE_MANIFESTS[name]
    return (
        f"'{target}' is a generated lockfile ('{name}'); "
        f"read the manifest '{manifest}' instead"
    )


def _blocked_path_parts_match(
    parts: Sequence[str], blocked_path: str | Path
) -> str | None:
    """Return the matched blocked path entry when *parts* falls under it."""
    entry_text = str(blocked_path).strip()
    if not entry_text:
        return None
    entry_parts = _path_parts(entry_text)
    if not entry_parts:
        return None
    if str(blocked_path).startswith(("/", "~")):
        return entry_text if tuple(parts[: len(entry_parts)]) == entry_parts else None
    width = len(entry_parts)
    for start in range(len(parts) - width + 1):
        if tuple(parts[start : start + width]) == entry_parts:
            return entry_text
    return None


def _resolve_blocked_path(blocked_path: str | Path, ws_resolved: Path) -> Path | None:
    """Resolve a configured blocked path against the workspace, ignoring bad entries."""
    entry = Path(str(blocked_path).strip()).expanduser()
    if not entry.is_absolute():
        entry = ws_resolved / entry
    try:
        return entry.resolve()
    except (OSError, RuntimeError):
        return None


def _blocked_path_match(
    target_resolved: Path,
    ws_resolved: Path,
    blocked_paths: Sequence[str | Path] | None,
) -> str | None:
    """Return the configured blocked path entry containing *target_resolved*."""
    if not blocked_paths:
        return None
    for blocked in blocked_paths:
        if not str(blocked).strip():
            continue
        resolved = _resolve_blocked_path(blocked, ws_resolved)
        if resolved is None:
            continue
        if target_resolved == resolved or target_resolved.is_relative_to(resolved):
            return str(blocked).strip()
    return None


def _tool_available(injected: bool | None, *binaries: str) -> bool:
    """Resolve tool availability from an injected test value or ``PATH``."""
    if injected is not None:
        return injected
    return any(shutil.which(binary) for binary in binaries)


def _check_blocked_read_path(
    target_resolved: Path,
    ws_resolved: Path,
    blocked_paths: Sequence[str | Path] | None,
) -> str | None:
    """Return a denial reason for reads of lockfiles or noisy cache directories."""
    relative = target_resolved.relative_to(ws_resolved)
    parts = relative.parts
    if relative.name in GENERATED_LOCKFILE_MANIFESTS:
        return _lockfile_reason(str(relative), relative.name)
    noisy = _noisy_directory_in_parts(parts)
    if noisy is not None:
        return (
            f"'{relative}' is inside noisy cache/vendor directory '{noisy}', "
            "which is blocked by guardrails"
        )
    matched = _blocked_path_match(target_resolved, ws_resolved, blocked_paths)
    if matched is not None:
        return (
            f"'{relative}' matches configured blocked path '{matched}' "
            "from blocked_paths"
        )
    return None


def _dump_target_reason(
    target: str, blocked_paths: Sequence[str | Path] | None
) -> str | None:
    """Return a denial reason for a path passed to a file-dump command."""
    parts = _path_parts(target)
    if not parts:
        return None
    name = parts[-1]
    if name in GENERATED_LOCKFILE_MANIFESTS:
        return _lockfile_reason(target, name)
    noisy = _noisy_directory_in_parts(parts)
    if noisy is not None:
        return (
            f"'{target}' is inside noisy cache/vendor directory '{noisy}', "
            "which is blocked by guardrails"
        )
    for blocked in blocked_paths or []:
        matched = _blocked_path_parts_match(parts, blocked)
        if matched is not None:
            return (
                f"'{target}' matches configured blocked path '{matched}' "
                "from blocked_paths"
            )
    return None


def _check_dump_command(
    command: str, args: Sequence[str], blocked_paths: Sequence[str | Path] | None
) -> str | None:
    """Check paths passed to a file-dump command such as ``cat`` or ``head``."""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            continue
        if arg.startswith("-") and arg != "-":
            index += 1
            continue
        reason = _dump_target_reason(arg, blocked_paths)
        if reason is not None:
            return f"File-dump command '{command}' is blocked: {reason}"
        index += 1
    return None


def _is_recursive_grep(args: Sequence[str]) -> bool:
    """Return True when grep arguments request recursive traversal."""
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--":
            break
        if not arg.startswith("-") or arg == "-":
            continue
        if arg.startswith("--"):
            name, separator, value = arg.partition("=")
            if name in ("--recursive", "--dereference-recursive"):
                return True
            if name == "--directories":
                directories = (
                    value if separator else (args[index] if index < len(args) else "")
                )
                if directories == "recurse":
                    return True
                if not separator:
                    index += 1
            elif name in _GREP_LONG_OPTIONS_WITH_VALUE and not separator:
                index += 1
            continue

        # Short option clusters: consume the value of options such as ``-e`` so a
        # pattern/value that happens to be ``-r`` is not mistaken for recursion.
        position = 0
        short = arg[1:]
        while position < len(short):
            option = short[position]
            if option in _GREP_SHORT_OPTIONS_WITH_VALUE:
                value = short[position + 1 :]
                if not value and index < len(args):
                    value = args[index]
                    index += 1
                if option == "d" and value == "recurse":
                    return True
                break
            if option in "rR":
                return True
            position += 1
    return False


def _find_maxdepth(args: Sequence[str]) -> int | None:
    """Return a valid ``-maxdepth`` value, or None when absent or malformed."""
    for index, arg in enumerate(args):
        value: str | None = None
        if arg == "-maxdepth" and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith("-maxdepth="):
            value = arg.split("=", 1)[1]
        if value is None:
            continue
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _workspace_search_roots(workspace: str | Path | None) -> set[Path]:
    """Return resolved roots that represent workspace-wide ``find`` targets."""
    roots: set[Path] = set()
    for raw in (workspace, Path.cwd()):
        if raw is None:
            continue
        try:
            roots.add(Path(raw).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    return roots


def _is_broad_find(args: Sequence[str], workspace: str | Path | None = None) -> bool:
    """Return True for workspace-wide ``find`` invocations without a shallow bound."""
    if any(arg in ("--help", "--version") for arg in args):
        return False
    index = 0
    while index < len(args) and args[index] in ("-L", "-H", "-P"):
        index += 1
    roots: list[str] = []
    while index < len(args) and not args[index].startswith("-"):
        roots.append(args[index])
        index += 1

    maxdepth = _find_maxdepth(args)
    if maxdepth is not None and maxdepth <= 1:
        return False
    if not roots:
        return True

    search_roots = _workspace_search_roots(workspace)
    for root in roots:
        if os.path.normpath(root) in _BROAD_FIND_ROOTS:
            return True
        if not Path(root).is_absolute():
            continue
        try:
            if Path(root).expanduser().resolve() in search_roots:
                return True
        except (OSError, RuntimeError):
            continue
    return False


def _check_resource_tokens(
    tokens: Sequence[str],
    *,
    blocked_paths: Sequence[str | Path] | None,
    rg_available: bool,
    fd_available: bool,
    workspace: str | Path | None,
) -> str | None:
    """Inspect one already-unwrapped simple command for blocked resource access."""
    index = 0
    while index < len(tokens) and _is_assignment(tokens[index]):
        index += 1
    if index >= len(tokens):
        return None

    name = _command_name(tokens[index])
    args = tokens[index + 1 :]

    if name in _FILE_DUMP_COMMANDS:
        return _check_dump_command(name, args, blocked_paths)
    if name in _GREP_COMMANDS:
        if rg_available and _is_recursive_grep(args):
            return (
                "Recursive grep is blocked when 'rg' is available; "
                "use 'rg' instead for gitignore-aware searches"
            )
        return None
    if name == "find" and fd_available and _is_broad_find(args, workspace):
        return (
            "Broad find is blocked when 'fd' is available; "
            "use 'fd' instead for gitignore-aware searches"
        )
    return None


def _check_resource_command(
    cmd_str: str,
    *,
    blocked_paths: Sequence[str | Path] | None,
    rg_available: bool,
    fd_available: bool,
    workspace: str | Path | None = None,
) -> str | None:
    """Inspect every unwrapped command for blocked dumps and unbounded searches."""
    for tokens in _iter_unwrapped_commands(cmd_str):
        reason = _check_resource_tokens(
            tokens,
            blocked_paths=blocked_paths,
            rg_available=rg_available,
            fd_available=fd_available,
            workspace=workspace,
        )
        if reason:
            return reason
    return None


def is_path_contained(
    target_path: str | Path,
    workspace: str | Path,
    *,
    allowed_paths: list[str | Path] | None = None,
    blocked_paths: list[str | Path] | None = None,
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

    # Spill files from truncated check output are read-only accessible so agents
    # can inspect omitted lines without exposing all of the system temp dir.
    if is_read and _is_spill_path(target_resolved):
        return True, None

    # 1. Target is inside current workspace
    if target_resolved == ws_resolved or target_resolved.is_relative_to(ws_resolved):
        if deny_runs and _is_run_artifact_path(target_resolved, ws_resolved):
            return (
                False,
                f"Target path '{target_path}' references '.factory/runs' or run artifacts, which are restricted for this stage",
            )
        if is_read:
            blocked_reason = _check_blocked_read_path(
                target_resolved, ws_resolved, blocked_paths
            )
            if blocked_reason is not None:
                return (
                    False,
                    f"Target path '{target_path}' is blocked by guardrails: {blocked_reason}",
                )
        else:
            matched = _blocked_path_match(target_resolved, ws_resolved, blocked_paths)
            if matched is not None:
                return (
                    False,
                    f"Target path '{target_path}' matches configured blocked path '{matched}' from blocked_paths",
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
    blocked_paths: list[str | Path] | None = None,
    deny_runs: bool = False,
    rg_available: bool | None = None,
    fd_available: bool | None = None,
    workspace: str | Path | None = None,
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

    resource_reason = _check_resource_command(
        cmd_str,
        blocked_paths=blocked_paths,
        rg_available=_tool_available(rg_available, "rg"),
        fd_available=_tool_available(fd_available, "fd", "fdfind"),
        workspace=workspace,
    )
    if resource_reason:
        return False, f"Command blocked by guardrail: {resource_reason} ('{cmd_str}')"

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
        "--block-path",
        action="append",
        default=[],
        help="Workspace-relative path to block from reads and file dumps (repeatable)",
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
    blocked_paths = [
        *args.block_path,
        *_parse_env_json_list("FACTORY_GUARDRAILS_BLOCKED_PATHS"),
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
            blocked_paths=blocked_paths,
            is_read=is_read,
            resource_dirs=res_dirs,
            deny_runs=args.deny_runs,
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
            cmd_candidate,
            custom_denied=custom_denied,
            blocked_paths=blocked_paths,
            deny_runs=args.deny_runs,
            workspace=cwd,
        )
        if not safe_cmd:
            print(f"[guardrails] DENIED: {cmd_reason}", file=sys.stderr)
            sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
