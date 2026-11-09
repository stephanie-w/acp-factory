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

## 4. Daily Operator Cheat Sheet

| Situation | Filter / Column | Your Action |
| :--- | :--- | :--- |
| **New feature idea** | Create issue | Add `factory:spec` or `factory:triage` label. Factory begins writing the specification. |
| **Spec is ready** | `factory:spec-ready` | Read the living spec in the issue body. If satisfied, remove `factory:spec-ready` and add `factory:build`. |
| **Questions arose** | `factory:needs-human` | Read `## Open Questions` in the issue body. Post a comment answering them, then add `factory:spec`. |
| **Budget cap hit** | `factory:needs-human` | Review the budget diagnostic. If approved, increase `max_tokens_per_run` in config and re-trigger. |
| **PR is ready** | `factory:pr-ready` | Inspect the Pull Request description, diff, and `Reviewed-SHA`. Click **Merge Pull Request**. |
| **PR merged** | `factory:completed` | Factory cleans up `.worktrees/issue-<id>` and deletes the local branch on the next run. |
