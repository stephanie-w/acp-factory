# Unattended Scheduling (cron / systemd)

`acp-factory` is designed to be run as a **one-shot command** on a schedule, not as a long-running daemon. See [`docs/fleet-orchestration.md`](fleet-orchestration.md) for the rationale: all orchestration state lives in GitHub labels, the issue body, and git worktrees, so every invocation is stateless and idempotent.

## The Command to Schedule

`acp-factory next` is the poll loop. It finds open issues with actionable `factory:*` labels, advances each one by a single stage, and exits.

- Single repo (run from inside the checkout, or pass `--workspace`):

  ```bash
  acp-factory next
  ```

- Fleet / multi-repo (uses `~/.acp-factory/fleet.toml`):

  ```bash
  acp-factory next --fleet
  ```

Runs stop automatically at the two human gates (`factory:spec-ready`, `factory:pr-ready`), so an unattended scheduler cannot run away or merge PRs.

## Environment Requirements

A cron or systemd environment is minimal and differs from an interactive shell. The runner needs:

1. **`gh` on `PATH` and authenticated.** `gh` is spawned as a subprocess. Authenticate once as the target user (`gh auth login`) or export `GH_TOKEN` / `GITHUB_TOKEN`. `gh` reads its own credentials from `~/.config/gh/hosts.yml`, so `HOME` must resolve to the same user that runs `acp-factory` interactively.
2. **`uv` and the agent binaries (`opencode`, `agy`, ...) on `PATH`.** The agent subprocesses are launched by name and resolve their own auth from the user's config files (via `~/.acpterm/config.json`), so they also depend on a correct `HOME`.
3. **`HOME` set correctly.** `~` paths in `~/.acp-factory/fleet.toml`, personas, and skills are expanded with `Path.expanduser()`, which uses `HOME`. Run the schedule as the same user who can run `acp-factory next` interactively.
4. **A writable `PATH`-adjacent working directory.** For single-repo mode, the runner must execute inside (or point `--workspace` at) the repo checkout. Fleet mode reads `[repositories]` from `~/.acp-factory/fleet.toml`, so no fixed cwd is required there, but the user must have write access to those checkouts and their `.worktrees/` directory.

## Overlap Protection

A single stage can run for many minutes (agent turns with retries). If the scheduler fires again while a run is still active, a second process could read the same labels and advance the same issue twice. Prevent overlap with one of:

- **cron**: wrap the command in `flock -n` (a file lock; a second invocation exits immediately if the lock is held).
- **systemd**: use `OnUnitActiveSec` on the timer, which does not re-fire until the unit finishes.

## Recipe 1 — cron + flock

One line, works on any Linux/macOS with `flock` (Linux) or `shlock` (BSD/macOS). Add to `crontab -e`:

```cron
# Run a fleet pass every 15 minutes; skip if the previous run is still active.
*/15 * * * * flock -n /tmp/acp-factory.lock acp-factory next --fleet >> ~/.acp-factory/cron.log 2>&1
```

Single-repo variant (adjust the working directory):

```cron
*/15 * * * * cd ~/DEV/core-lib && flock -n /tmp/acp-factory.lock acp-factory next >> ~/.acp-factory/cron.log 2>&1
```

Notes:

- `/tmp/acp-factory.lock` is an empty marker file; no data is stored in it.
- `flock -n` exits immediately (non-blocking) if the lock is held, so the next cron fire waits for the following interval instead of queueing.
- Redirect output to a log file, since cron mails stdout/stderr by default.
- cron does not reload `~/.bashrc`/`~/.zshrc`. Put any non-default `PATH` or token exports directly in the crontab line or in a wrapper script.

## Recipe 2 — systemd service + timer

Recommended when you want journald logging, `OnFailure` notifications, and overlap protection without `flock`.

`/etc/systemd/system/acp-factory.service`:

```ini
[Unit]
Description=acp-factory unattended fleet pass
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=<your-user>
EnvironmentFile=-/home/<your-user>/.config/acp-factory/scheduler.env
ExecStart=/home/<your-user>/.local/bin/acp-factory next --fleet
TimeoutStartSec=45min

# Optional: notify on failure (requires a working mail or notify setup)
# OnFailure=notify-failure@%i.service
```

`/etc/systemd/system/acp-factory.timer`:

```ini
[Unit]
Description=Run acp-factory fleet pass every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Unit=acp-factory.service

[Install]
WantedBy=timers.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now acp-factory.timer
```

Notes:

- `Type=oneshot` + `OnUnitActiveSec=15min` means the timer fires 15 minutes **after the previous run finishes**, never while it is still running.
- `EnvironmentFile=-` tolerates a missing file (the leading `-`). Create `~/.config/acp-factory/scheduler.env` (mode `600`) for any secrets the agent or `gh` need:

  ```sh
  # GH_TOKEN=...
  # OPENAI_API_KEY=...
  ```

- Single-repo variant: set `WorkingDirectory=/home/<your-user>/DEV/<repo>` and `ExecStart=/home/<your-user>/.local/bin/acp-factory next`.
- Replace `<your-user>` and the absolute `ExecStart` path with your own before enabling (systemd does not expand `~`).
- Inspect logs with `journalctl -u acp-factory.service` and `systemctl list-timers acp-factory.timer`.

## Verifying Before Going Live

1. Run the exact command manually once and confirm it completes without errors.
2. Do a dry-run pass to confirm issue selection is correct without mutating GitHub:

   ```bash
   acp-factory next --fleet --dry-run
   ```

3. For systemd, test the unit directly before relying on the timer:

   ```bash
   sudo systemctl start acp-factory.service
   systemctl status acp-factory.service --no-pager
   ```
