# Agent Operational Guidelines & Protocols

This document defines the **mandatory operational behaviors and protocols** for AI Agents working within this environment.

## 1. Core Operational Directives

### 🛡️ Safety & Integrity
1.  **Read Before Write**: You must read `pyproject.toml` and related source files (except for `uv.lock`) *before* proposing changes. Never guess the stack.
2.  **Atomic Changes**: Do not rewrite entire files for small changes. Target specific lines or blocks to minimize regression risk and minimize context/output volume.
3.  **No Hallucinations**: Do not import libraries that are not explicitly installed in `pyproject.toml`. Use `uv add` only if strictly necessary and authorized.
4.  **Secrets Management**: **NEVER** output, log, or commit API keys, passwords, or tokens. Use `os.environ` or `pydantic-settings`.

### 🐙 Version Control (Git)

1.  **Commit Format**: Use [Conventional Commits](https://www.conventionalcommits.org/):
    *   `feat(scope): description`
    *   `fix(scope): description`
    *   `docs(scope): description`
    *   `refactor(scope): description`
2.  **Atomic Commits**: One logical change per commit. Do not bundle formatting changes with logic changes.
3.  **Safety**:
    *   Never commit directly to `main` or `develop`.
    *   Never force push (`git push -f`) to shared branches.
    *   Never commit `.env` files or secrets.
4.  **Checkpoint Commits**: For major feature implementations or architectural changes, perform a commit once a significant sub-task is verified, rather than waiting for the entire request to be finished.
5. **Git Worktrees**: For major feature implementations with some uncertainties, perform a checkpoint commit and use a git worktree

### 🔄 Development Workflow
1.  **Pattern Recognition**: Analyze `src/` to mimic existing architectural patterns (naming, typing, structure).
2.  **Test-Driven Mindset**: If you write logic, you must write a test for it.
3.  **Verification Loop**: You are responsible for the quality of your output. When you make changes to executable code or tests, always check your LSP diagnostics and if no LSP available for you always run:
    ```bash
	just fix
	just typecheck
	just test-fast
    ```
	 You can also `uv run pytest` to run specific test(s).
    *Do NOT run tests or linters for documentation-only, markdown, or text changes.*
    *Fix all errors reported by these linting and typechecking tools before asking the user to review.*
4.  **UI Testing Protocol**: For UI implementations that require user interaction (modals, keyboard navigation, visual layout), explicitly ask the user to test before marking tasks complete. Create test scripts in `demos/` directory when debugging complex UI issues.
5.  **Task Completion Verification**: Never mark a task as completed without explicit user verification when the task involves user-facing functionality. Always ask "Could you test this?" before updating TODO.md.
6.  **Centralize First**: Group related code (e.g., Pydantic models) in a single module first. Split into separate files only when the file grows too large or has clear independent re-usability.

### Conversational Coding Workflow

To ensure a safe and collaborative development experience, follow this interactive workflow for all code modifications:

1.  **Research & Analysis**: Thoroughly investigate the codebase to understand the current implementation and dependencies.
2.  **Proposed Strategy**: Before writing any code, provide a concise summary of the intended changes, including:
    *   **The "Why"**: Technical rationale for the approach.
    *   **The "How"**: Specific files to be modified and new logic to be added.
    *   **Impact**: Potential side effects or architectural considerations.
3.  **Review & Confirmation**: Explicitly ask the user to review and confirm the strategy. **Wait for user approval** before proceeding with the implementation.
4.  **Surgical Execution**: Once confirmed, apply the changes iteratively, providing clear intent for each step.
5.  **Validation**: Verify the changes through testing and quality checks (linting, type checking) as defined in this project.

## 2. Strict Anti-Patterns (The "Do Not Use" List)

Violating these rules will result in rejected code.

### 🚫 Code Structure
- **No Mutable Defaults**: `def foo(items=[])` is forbidden. Use `items=None`.
- **No Broad Exceptions**: `except Exception:` without logging or re-raising is forbidden.
- **No Global State**: Do not rely on or modify global variables. Pass state explicitly.
- **No Magic Numbers**: Define constants with descriptive names (e.g., `MAX_RETRIES = 3` instead of just `3`).

### 🚫 Observability
- **No `print()` Statements**: Use the standard `logging` module or `structlog`. `print()` is for CLI output only, not debugging.
- **No Silent Failures**: Errors must be explicitly handled or bubbled up.

### 🚫 Filesystem
- **No Hardcoded Paths**: Use `pathlib.Path` and relative paths. Never use absolute paths like `/home/user/...`.

### 🚫 Typing (Modern Python 3.10+)
- **No `typing.List`, `typing.Dict`, etc.**: Use built-in generics (e.g., `list[str]`, `dict[str, int]`).
- **No `typing.Optional`**: Use the union operator `X | None`.
- **No `typing.Union`**: Use the `|` operator (e.g., `int | str`).

## 3. Python Tech Stack Summary

- **Minimum Version**: 3.14+
- **Manager**: `uv` (Command: `uv run ...`)
- **Task Runner**: `just` (Preferred) or `make` (Legacy)
- **Linter/Formatter**: `ruff`
- **Type Checker**: `ty`
- **Testing**: `pytest`

## 4. Quality Assurance Checklist

Before signaling completion, verify:

- [ ] **Context**: Did I read the existing code to match the style?
- [ ] **Types**: Are all functions fully type-hinted?
- [ ] **Docs**: Do public functions have Google-style docstrings?
- [ ] **Tests**: Did I add/update tests? Do they pass?
- [ ] **Lint**: Did `ruff` and `ty` pass?
- [ ] **Safety**: Did I avoid hardcoding secrets or paths?


## 5. Living Documentation (this file - `AGENTS.md`)

- This document (`AGENTS.md`) serves as the primary instruction for you. If you learn new information or receive important guidance, update the section below. If you encounter something in the project that surprises you or confuses you, please, alert the developer and update the section below with this information to help prevent agents from having the same issue. 
- Append only, do not remove or modify existing content unless it is incorrect or outdated.
- If you find useful documentation (for example about libraries, tools, or techniques) from external sources, add links to it here, so that you can get back to it later.
- Keep notes about your development process, decisions made, the current architecture of the project.

## 6. Important Things to Keep in Mind

- Always assume the user's time is valuable; provide high-signal updates.
- **Verification Scoping**: Only run test and typecheck commands (`just fix`, `just typecheck`, `just test-fast`) when code logic or tests were modified. Never run tests for documentation-only, markdown, or planning updates.
- Prioritize stability and protocol correctness over visual flair.
- There is a `demos` repository dedicated to implement simplified UI to test UI features or modifications. USE IT...
- **Textual Modal Patterns**: When using `ModalScreen` with `push_screen_wait()`, always use `run_worker()` to avoid `NoActiveWorker` errors. Modal screens require proper worker context for async operations.
- **Widget ID Rules**: Textual widget IDs must contain only letters, numbers, underscores, or hyphens, and must not begin with a number. Sanitize dynamic IDs (replace `/`, `.`, `:` with hyphens).
- **Keyboard Navigation**: Use `ListView` with `ListItem` widgets instead of `Static` widgets for selectable lists. `Static` widgets don't support keyboard navigation.
- **Search & Filtering Tool Preference**: Always prefer `rg` over `grep`, `fd` over `find`, and `jq` over ad-hoc `sed`/`awk`/Python for JSON parsing. These tools respect `.gitignore`, avoid vendor noise, and are faster and more precise.

## 7. Breaking Loops & Isolation

**CRITICAL:** If you find yourself stuck in a loop or struggling with a persistent issue:
- **Isolate the Problem**: Do not keep modifying the core application. Create small, standalone reproduction scripts or mockups in the `demos` folder (e.g., `repro_issue.py`, `mock_agent.py`) to verify assumptions in a controlled environment.
- **Verify the Layer**: Determine if the bug is in the communication layer (JSON-RPC), the process management, or the UI before applying broad changes.

## 8. Requesting Assistance

- **Interactive Commands**: If a task requires running an interactive CLI or a command that depends on a specific terminal state (like `uv run acp-client`), **request the developer to run it**.
- **Observational Data**: Ask the developer for specific output or logs if the environment prevents you from seeing them directly.
