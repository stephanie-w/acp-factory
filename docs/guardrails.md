# Security & Execution Guardrails

`acp-factory` enforces defense-in-depth guardrails around ACP agent sessions, shell checks, GitHub mutations, and worktree state. All enforcement lives in the orchestrator—agents cannot disable or bypass these controls.

---

## 1. ACP Permission Hook (`acp_factory.runner.guardrails`)

When `guardrails.enabled` is `true` (default), [`FactoryACPClient.start()`](../src/acp_factory/acp/client.py) registers `python -m acp_factory.runner.guardrails` as an `acpterm` `permission` hook. Every file access or command permission request is checked before execution.

### Workspace Path Containment (`is_path_contained`)
- **Workspace boundary:** Relative and symlinked paths are resolved against `ACPTERM_CWD`. Any path outside the active workspace (or worktree) is denied.
- **External & bundled resources catalogs (read-only):** When `kind` is `read` or `view`, reading from configured `resource_dirs` / `resources_dir` (`--resources-dir` / `FACTORY_RESOURCES_DIRS` / `FACTORY_RESOURCES_DIR`) and `acp-factory`'s package-bundled resources is permitted.
- **Explicit allowlist:** Paths listed in `[guardrails].allowed_paths` (`--allow-path` / `FACTORY_GUARDRAILS_ALLOWED_PATHS`) are permitted.
- **Run artifact isolation (`--deny-runs`):** In all stages except `retro`, access to `.factory/runs/`, `.factory/latest-report.md`, and `.factory/latest-build-notes.md` is denied so agents do not read prior run transcripts or reports directly from disk.

### Command & Sensitive Path Filtering (`is_command_safe`)
For `execute` / `command` / `bash` requests, commands are checked against:

- **Built-in dangerous command patterns (`DANGEROUS_COMMAND_PATTERNS`):**
  - Privilege escalation: `sudo`, `su`, `doas`
  - Recursive deletion: `rm -rf`, `rm -r`, `rmdir -r`, `unlink -r`
  - Remote script piping: `curl ... | sh`, `wget ... | bash` (and `zsh`/`dash`/`ash`)
  - Disk/filesystem formatting: `mkfs*`, `newfs_*`, `wipefs`, `parted`, `fdisk`, `gdisk`, `sgdisk`, `cryptsetup`
  - Raw device writes: `dd ... of=/dev/...`
  - Destructive or unauthorized git operations: `git push`, `git reset --hard`, `git clean -f` / `--force`
  - System power commands: `shutdown`, `reboot`, `poweroff`, `init 0`
  - Output redirection (`>` / `>>`) into `/dev/sd*`, `/dev/nvme*`, `/etc/`, `/boot/`, `/root/`, `~/.ssh/`, `~/.aws/`, `~/.gnupg/`, `~/.config/gh/`, `~/.config/gcloud/`
- **Sensitive host paths (`SENSITIVE_HOST_PATHS`):**
  - `/etc/shadow`, `/etc/passwd`, `/etc/sudoers`
  - `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.docker/config.json`, `~/.config/gh`, `~/.config/gcloud`
- **Dotenv secret files:** `.env` and `.env.*` (e.g., `.env.local`, `.env.production`) are blocked for both path access and shell commands, while safe templates (`.env.example`, `.env.sample`, `.env.template`) remain allowed.
- **Custom denied patterns:** Case-insensitive substring matches from `[guardrails].denied_commands` (`--deny-cmd` / `FACTORY_GUARDRAILS_DENIED_COMMANDS`).
- **Run artifact commands (`--deny-runs`):** Shell commands referencing `.factory/runs`, `.factory/latest-report.md`, or `.factory/latest-build-notes.md` are blocked outside `retro`.

---

## 2. Read-Only `gh` CLI Shim (`runner/checks.py`)

[`build_workspace_env()`](../src/acp_factory/runner/checks.py) prepends a generated `gh` wrapper (`_GH_READONLY_SHIM_SCRIPT`) to `PATH` for ACP agents and check subprocesses:

- **Allowed:** Read-only queries (`gh issue view`, `gh pr view`, `gh pr diff`, `gh api` reads, etc.) are forwarded to the real `gh` binary.
- **Blocked (exit 1):** Mutating subcommands—`edit`, `create`, `comment`, `close`, `reopen`, `delete`, `merge`, `lock`, `unlock`, `transfer`, `pin`, `unpin`, `ready`, `review`—are rejected.
- All GitHub state transitions (labels, issue body updates, PR creation) are performed exclusively by the `acp-factory` runner after checks pass.

---

## 3. Subprocess Environment Isolation (`runner/checks.py`)

To prevent the orchestrator's own virtualenv, runtime, or host credentials from leaking into target repositories, `build_workspace_env()` strips `DEFAULT_HOST_ENV_STRIP` variables and any host variable whose name matches `DEFAULT_SENSITIVE_ENV_PATTERNS` before spawning setup commands, checks, or ACP agents:

- **Python / Conda:** `VIRTUAL_ENV`, `VIRTUAL_ENV_PROMPT`, `PYTHONPATH`, `PYTHONHOME`, `CONDA_PREFIX`, `CONDA_DEFAULT_ENV`, `CONDA_PROMPT_MODIFIER`
- **Node / npm:** `NODE_PATH`, `NODE_OPTIONS`, `INIT_CWD`, `npm_config_prefix`
- **Ruby / Bundler:** `BUNDLE_GEMFILE`, `BUNDLE_BIN_PATH`, `GEM_HOME`, `GEM_PATH`, `RUBYOPT`
- **Sensitive credential patterns (case-insensitive):** `TOKEN`, `API_?KEY`, `SECRET`, `PASS(?:WORD|WD)`, `CREDENTIAL`, `^AWS_`, `PRIVATE_?KEY`, `ACCESS_?KEY`

### Allowlisting with `keep_env`

Sensitive-pattern and `DEFAULT_HOST_ENV_STRIP` variables are stripped unless their exact name is listed in `keep_env` (global or per-stage, merged in insertion order). `strip_env` always takes precedence over `keep_env`, and explicit `env` / `extra_env` values are applied after filtering.

```toml
keep_env = ["OPENAI_API_KEY", "AWS_DEFAULT_REGION"]

[stages.build]
keep_env = ["CUSTOM_TEST_TOKEN"]
```

Host `GH_TOKEN` is stripped by default. When `FACTORY_GH_TOKEN` is set it is promoted to `GH_TOKEN` for `gh` subprocesses and the internal `FACTORY_GH_TOKEN` variable is not exposed; use `keep_env = ["GH_TOKEN"]` only to intentionally forward a personal token.

Because a fleet target repository's own `.factory/config.toml` can add `keep_env` entries, treat fleet target repositories as fully trusted: they already control arbitrary `pre`/`checks` commands and can read the host environment directly. Central governance should only dispatch fleet runs to repositories whose configuration is trusted to re-inherit host credentials deliberately.

---

## 4. Stage, Worktree & Publish Integrity Guards (`runner/worktree.py`, `runner/loop.py`)

- **Read-only stages (`read_only = true`):** `triage`, `spec`, `review`, and `retro` run with `read_only=True` passed to `ACPAgent`.
- **Isolated issue worktrees:** `build` and `review` execute in `.worktrees/issue-<id>` on branch `factory/issue-<id>` rather than mutating the main checkout.
- **Pre-publish integrity check:** Before `review` publishes or pushes a PR, `run_issue_stage()` verifies that `HEAD` SHA is unchanged (`post_review_sha == pre_review_sha`) and the worktree is clean (`is_worktree_clean`). Any unexpected modification aborts publication and escalates the issue to `factory:needs-human`.
- **Baseline pre-checks (`[stages.build] pre`):** Running `uv run --frozen ruff check` and `uv run --frozen ty check` in `pre` aborts a `build` run with zero agent turns if the base branch is already broken.

---

## 5. Budget & Retry Caps (`config.py`, `runner/loop.py`)

- **Budget caps (`[budget]`):** `max_tokens_per_run`, `max_cost_usd`, and `max_turns` are checked after every turn via `check_usage_cap()`. Breaching a cap stops the run and escalates the issue to `factory:needs-human`.
- **Retry & timeout limits:** `[retry].max_attempts`, `max_review_rounds`, and `[agent].timeout` bound self-healing loops and stuck turns.

---

## 6. Configuration (`.factory/config.toml`)

```toml
[guardrails]
enabled = true
allowed_paths = []
denied_commands = []

[budget]
max_tokens_per_run = 100000
max_cost_usd = 2.00
max_turns = 3
```
