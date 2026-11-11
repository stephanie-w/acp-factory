# Tracking & Monitoring Guide: Bookmark Filters & GitHub Projects

This guide outlines how to monitor `acp-factory` across repositories using **instant GitHub global bookmark filters** (zero setup) and optional **GitHub Projects (v2)** boards.

---

## 1. Zero-Setup GitHub Global Search Filters (Recommended)

GitHub's global issue search can query across **all repositories** under your account (`stephanie-w`) simultaneously. You can save these direct URLs as bookmarks in your browser for an instant, real-time dashboard:

### 🛑 Filter 1: Human Action Required (Inbox)
> **Direct Link**: [Issues requiring human decision](https://github.com/issues?q=is%3Aopen+user%3Astephanie-w+label%3Afactory%3Aspec-ready%2Cfactory%3Apr-ready%2Cfactory%3Aneeds-human)
* **Search Query**:
  ```text
  is:open user:stephanie-w label:factory:spec-ready,factory:pr-ready,factory:needs-human
  ```
* **Shows**:
  * `factory:spec-ready` — **Gate 1**: Architectural spec is ready for your review and approval.
  * `factory:pr-ready` — **Gate 2**: Reviewed PR is published with `Reviewed-SHA`; ready for you to merge.
  * `factory:needs-human` — Escalation: Open questions, token budget cap breached, or 3-round review cap hit.

---

### 🚀 Filter 2: All Active / In-Flight Factory Tasks
> **Direct Link**: [All active agent stages across repositories](https://github.com/issues?q=is%3Aopen+user%3Astephanie-w+label%3Afactory%3Atriage%2Cfactory%3Aspec%2Cfactory%3Abuild%2Cfactory%3Areview)
* **Search Query**:
  ```text
  is:open user:stephanie-w label:factory:triage,factory:spec,factory:build,factory:review
  ```
* **Shows**: Issues currently being triaged, specified, built in worktrees, or reviewed by agents.

---

### 📋 Filter 3: Single Repository Scope (e.g. `acp-factory`)
> **Direct Link**: [All factory issues in acp-factory](https://github.com/stephanie-w/acp-factory/issues?q=is%3Aissue+label%3Afactory%3Atriage%2Cfactory%3Aspec%2Cfactory%3Aspec-ready%2Cfactory%3Abuild%2Cfactory%3Areview%2Cfactory%3Apr-ready%2Cfactory%3Aneeds-human%2Cfactory%3Adone)
* **Search Query**:
  ```text
  repo:stephanie-w/acp-factory is:issue label:factory:triage,factory:spec,factory:spec-ready,factory:build,factory:review,factory:pr-ready,factory:needs-human,factory:done
  ```

---

## 2. CLI Terminal Dashboard (`acp-factory status`)

Without opening a browser, you can inspect the current workspace or the entire multi-repo fleet directly from your terminal:

* **Current Repository**:
  ```bash
  uv run acp-factory status
  ```
* **Multi-Repo Fleet Dashboard** *(Available in `feat/fleet-orchestration`)*:
  ```bash
  uv run acp-factory status --fleet
  ```
  Outputs an instant, color-coded status table showing every repository, active issue number, stage label, worktree status, and open PR link.

---

## 3. GitHub Projects (v2) Board (Optional)

If you prefer a visual drag-and-drop Kanban board, you can create a Project board in GitHub:

```
[ factory:triage ] ──► [ factory:spec ] ──► [ factory:spec-ready ] (Gate 1)
                                                         │ (Human approves)
[ factory:pr-ready ] (Gate 2) ◄── [ factory:review ] ◄── [ factory:build ]
         │ (Human merges)
[ factory:completed ]
         ▲
         └── [ factory:needs-human ] (Escape hatch: questions / budget / review limit)
```

> [!NOTE]
> **Personal vs. Organization Projects**:
> In GitHub Projects (v2), the **Linked repositories** setting is primarily available for projects created under a **GitHub Organization**. For personal user accounts, GitHub only allows a single **Default repository** for shorthand issue references. However, issues from any repository can still be added to personal project boards by pasting their full URL (or `owner/repo#123`).

### Board Columns Configuration (Status Field)

| Column Name | Color | Trigger Label | Meaning |
| :--- | :--- | :--- | :--- |
| **📥 Triage** | Gray | `factory:triage` | Problem classification & sizing |
| **📝 Spec** | Blue | `factory:spec` | Architectural specification & AC-n criteria |
| **🚦 Spec Ready** | Yellow / Orange | `factory:spec-ready` | **Human Gate 1**: Waiting for human spec review |
| **🔨 Build** | Blue | `factory:build` | Coding, tests, and checks in isolated worktree |
| **🔍 Review** | Purple | `factory:review` | Cross-model review and acceptance verification |
| **🚀 PR Ready** | Green | `factory:pr-ready` | **Human Gate 2**: Reviewed PR opened, waiting for merge |
| **⚠️ Needs Human** | Red | `factory:needs-human` | Open questions, budget breach, or 3-round cap |
| **✅ Completed** | Green / Gray | `factory:completed` | PR merged and worktree cleaned up |

---

## 4. Daily Morning Routine (10–15 Min)

### Step 1: Check the Fleet Pulse (1 min)
Run the fleet dashboard to see all repositories, active stages, human gates, and open worktrees at a glance:
```bash
uv run acp-factory status --fleet
```
*(Or open the **[Human Action Required Inbox](https://github.com/issues?q=is%3Aopen+user%3Astephanie-w+label%3Afactory%3Aspec-ready%2Cfactory%3Apr-ready%2Cfactory%3Aneeds-human)** bookmark above.)*

Focus on the three labels that require human decisions:
1. `factory:pr-ready` (**Human Gate 2** — code is built & reviewed)
2. `factory:spec-ready` (**Human Gate 1** — spec is written & waiting for sign-off)
3. `factory:needs-human` (**Escalation** — open architectural question or 3-round review limit)

### Step 2: Clear Human Gate 2 First — Merge Ready PRs (3–5 min)
Always review and merge `factory:pr-ready` PRs **first**. Because `acp-factory` enforces at most one active code branch/PR per repository to prevent merge-conflict cascades, merging an open PR frees that repository's build slot.
1. Open the PR link displayed in `acp-factory status --fleet`.
2. Inspect the `## Summary`, `Reviewed-SHA`, and diff on GitHub.
3. **If satisfied**: Click **Merge Pull Request** on GitHub (`Closes #<id>` automatically closes the issue).
4. **Clean up & sync `main`**:
   Run the orchestrator to automatically remove the local `.worktrees/issue-<id>` worktree, delete the local `factory/issue-<id>` branch, and transition the issue label to `factory:completed`:
   ```bash
   uv run acp-factory issue <id>
   git checkout main && git pull origin main
   ```
   *(Alternatively, Step 5's batch `next --fleet` pass will clean up all closed issues automatically).*
5. **If changes are needed**: Leave a comment on the issue and return it to `factory:build`:
   ```bash
   gh issue edit <id> -R owner/repo --remove-label factory:pr-ready --add-label factory:build
   ```

### Step 3: Unblock Human Gate 1 & Escalations (5 min)
* **For `factory:spec-ready` (Gate 1)**:
  1. Read the living specification inside the Issue Body (`## Behavior`, `## Assumptions`, `## Acceptance Criteria`).
  2. **Approve for build**:
     ```bash
     gh issue edit <id> -R owner/repo --remove-label factory:spec-ready --add-label factory:build
     ```
  3. **Request spec changes**: Add a comment on the issue and re-run `spec`:
     ```bash
     gh issue edit <id> -R owner/repo --remove-label factory:spec-ready --add-label factory:spec
     ```
* **For `factory:needs-human` (Escalations)**:
  1. Read `## Open Questions` (or review findings) in the Issue Body.
  2. Reply in a standard GitHub issue comment with your decision.
  3. Re-trigger the stage:
     ```bash
     gh issue edit <id> -R owner/repo --remove-label factory:needs-human --add-label factory:spec
     ```

* **For Active Worktrees & `factory:review` (Local Testing & Advancing)**:
  1. **Locate the worktree**: Built issues live in `.worktrees/issue-<id>` on branch `factory/issue-<id>`.
  2. **Ensure a clean working tree**: The orchestrator enforces a strict pre-review integrity check (`git status --porcelain`). Discard local setup noise (such as `uv.lock` path adjustments from `uv sync`) or commit deliberate changes:
     ```bash
     git -C .worktrees/issue-<id> restore uv.lock
     ```
  3. **Run or preview the review**:
     ```bash
     # Preview the review verdict and proposed PR without modifying GitHub:
     uv run acp-factory issue <id> --dry-run

     # Or run live (pushes branch, opens/updates PR, and flips to factory:pr-ready upon acceptance):
     uv run acp-factory issue <id>
     ```

### Step 4: Drop New Ideas into the Inbox (1–2 min)
Create new issues in any repository with `factory:triage` (or `factory:spec` if well-defined):
```bash
gh issue create -R owner/repo \
  --title "Short feature or bug title" \
  --body "Problem description and context..." \
  --label "factory:triage"
```

### Step 5: Kick Off the Factory Run (Unattended)
Trigger the fleet pass and let the agents work in the background:
```bash
uv run acp-factory next --fleet --all
```
This pass automatically:
1. **Cleans up (`done`)**: Removes local `.worktrees/issue-<id>` for the PRs you merged in Step 2 and sets `factory:completed`.
2. **Advances `triage` & `spec`**: Runs read-only preparation for new issues up to `factory:spec-ready` (Gate 1).
3. **Builds & Reviews**: Branches off fresh `origin/main` for one approved `factory:build` issue per repository, runs checks, executes the independent review (`factory:review`), and opens/updates the PR (`factory:pr-ready`).

---

## 5. Daily Operator Cheat Sheet

| Situation | Filter / Column | Your Action |
| :--- | :--- | :--- |
| **New feature idea** | Create issue | Add `factory:spec` or `factory:triage` label. Factory begins writing the specification. |
| **Spec is ready** | `factory:spec-ready` | Read the living spec in the issue body. If satisfied, remove `factory:spec-ready` and add `factory:build`. |
| **Questions arose** | `factory:needs-human` | Read `## Open Questions` in the issue body. Post a comment answering them, then add `factory:spec`. |
| **Budget cap hit** | `factory:needs-human` | Review the budget diagnostic. If approved, increase `max_tokens_per_run` in config and re-trigger. |
| **Code built in worktree** | `factory:review` | Ensure clean tree (`restore uv.lock`), then run `acp-factory issue <id>` (or `--dry-run`) to review and open PR. |
| **PR is ready** | `factory:pr-ready` | Inspect the Pull Request description, diff, and `Reviewed-SHA`. Click **Merge Pull Request**. |
| **PR merged** | `factory:completed` | Run `acp-factory issue <id>` (or `next`) to clean `.worktrees/issue-<id>`, delete branch, and label `factory:completed`. |
