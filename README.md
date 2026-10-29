# ProofPush

> **ProofPush: Local AI Website Regression Auditor**
> Audits website functionality, visual regressions, and security before Git push.

Unlike conventional code-review bots that only analyze static text diffs, **ProofPush** executes your website before and after changes in isolated local environments, evaluates actual user-facing behavior via Playwright browser automation, detects regressions and hardcoded secrets, and produces evidence-backed reports to allow or block pushes.

---

## Key Features

1. **Universal Git Pre-Push Interception**
   - Automatically intercepts `git push` attempts via standard Git hooks.
   - Preserves pre-existing repository hooks by chaining them as `pre-push.legacy`.
   - Correctly processes standard input refs across multi-commit pushes, force pushes, new branches, and ref deletions.
   - Built-in recursion protection (`PROOFPUSH_RUNNING`).

2. **Task Context Awareness**
   - Resolves what you or an AI agent (Claude Code, Gemini CLI, Cursor, Codex) intended to change from:
     1. Explicit CLI arguments (`--task "..."`) or `PROOFPUSH_TASK` environment variable.
     2. Agent lifecycle metadata (`.claude/current_task.json`, `.gemini/task.md`).
     3. Commit messages being pushed.
     4. Task context file (`.proofpush-task.md` or `.git/proofpush-task.md`).
     5. Interactive developer prompt.
   - Built-in prompt injection defense to sanitize adversarial instructions in commit messages.

3. **Isolated Parallel Execution**
   - Launches baseline and proposed versions in separate temporary Git worktrees (`git worktree add --detach`).
   - Automatically allocates collision-free local ports (e.g. 3100 vs 3200).
   - Isolates mutable state (test databases, environment variables).
   - Cleanly cleans up temporary worktrees and child processes upon completion.

4. **Playwright-Based Functional Testing**
   - Framework route discovery (Next.js App/Pages router, FastAPI, Django, Express, HTML files, sitemaps).
   - Monitors unhandled JavaScript console exceptions and failed network/API requests.
   - Executes multi-step user workflows using accessible and semantic selectors.
   - Built-in safety filters to avoid clicking destructive actions (e.g., delete, purge, charge, pay) during exploration.

5. **Visual Regression Testing**
   - Captures synchronized screenshots across configurable desktop and mobile viewports.
   - Normalizes and stabilizes dynamic content (animations, timestamps, scrollbars).
   - Computes pixel-level difference ratios and bounding boxes highlighting shifted or modified layouts.

6. **Unrelated Changes & Security Audit**
   - Classifies outgoing changed files: `Relevant`, `Supporting`, `Possibly related`, `Unrelated`, `Suspicious`, `Sensitive`.
   - Scans diffs for accidentally committed credentials, API keys (OpenAI, AWS, GitHub), and private keys.
   - Detects suspicious deletions of automated test suites.

7. **Local-First & Modular AI**
   - Default 100% deterministic local rule engine requiring **zero external network requests**.
   - Optional modular adapters for OpenAI / Anthropic APIs with automated secret redaction.

---

## Quick Start & Installation

### Option 1: One-Command Installation (Automated)
Clone the repository and run the installer script, which sets up Python dependencies and Playwright browser binaries in a single command:

```bash
git clone https://github.com/mmhtanvir/ProofPush.git
cd ProofPush
./install.sh
```

### Option 2: Pip Installation
```bash
git clone https://github.com/mmhtanvir/ProofPush.git
cd ProofPush
pip install -e .
proofpush install
```

> **Note**: Running `proofpush install` inside any repository automatically verifies and downloads any required Playwright browser binaries if they are not already installed.

### Step 3: Run Diagnostics
```bash
proofpush doctor
```

---

## CLI Command Reference

| Command | Description |
|---|---|
| `proofpush install` | Installs the pre-push hook in the current Git repository. Preserves existing hooks. |
| `proofpush install --global` | Configures global Git hook path. |
| `proofpush uninstall` | Safely uninstalls the pre-push hook and restores any preserved legacy hook. |
| `proofpush doctor` | Diagnoses environment, dependencies, Git hooks, and configuration. |
| `proofpush review` | Audits pending or proposed changes against the baseline. |
| `proofpush review --full` | Runs comprehensive full audit (broader crawl, all viewports). |
| `proofpush review --mode strict` | Forces strict enforcement mode (blocks on any critical issue). |
| `proofpush baseline capture` | Captures and validates a new baseline snapshot for the current HEAD commit. |
| `proofpush baseline list` | Lists locally captured baselines for the repository. |
| `proofpush report` | Displays the latest audit report in Markdown format. |
| `proofpush report --json` | Outputs the latest report in structured JSON. |
| `proofpush report --open` | Opens the interactive HTML evidence viewer in your browser. |
| `proofpush config` | Displays the effective configuration. |
| `proofpush test` | Runs the automated 30-scenario test suite. |

---

## Configuration (`.proofpush.yml` or `.change-auditor.yml`)

Place `.proofpush.yml` in your repository root:

```yaml
# Audit Mode: advisory, interactive, strict
mode: interactive

# Audit Depth: quick, full
audit_level: quick

# Baseline Strategy: merge-base, captured, commit, latest-passed
baseline:
  strategy: merge-base
  reference_ref: origin/main

# Application Startup Configuration
application:
  type: custom # nextjs, django, fastapi, node, custom
  start_command: "npm run dev -- -p $PORT"
  port: 3000
  health_check: "/"
  health_timeout: 30
  env:
    NODE_ENV: test

# Routes to test
routes:
  auto_discover: true
  base_routes:
    - "/"
    - "/checkout"
  excluded_routes:
    - "/admin/purge*"

# Custom Functional Workflows
workflows:
  - name: "Checkout Navigation"
    path: "/"
    steps:
      - action: navigate
        value: "/"
      - action: click
        selector: "button[data-testid='checkout-button']"
      - action: assert_url
        expected: "/checkout"

# Visual Regression Settings
visual:
  enabled: true
  diff_threshold: 0.02
  viewports:
    - width: 1280
      height: 800
      name: desktop
    - width: 375
      height: 667
      name: mobile
  excluded_selectors:
    - ".timestamp"
    - ".dynamic-id"

# Security & Secret Scanning
security:
  sensitive_paths:
    - ".env*"
    - "**/*.pem"
    - "**/*.key"
  fail_on_secret: true
  fail_on_unrelated: false

# Enforcement Policies
policies:
  fail_open: false
  interactive_fallback: block # block or allow in non-interactive CI / AI CLI
```

---

## Enforcement Policies

### 1. Advisory Mode (`mode: advisory`)
Executes the full audit, displays terminal findings and visual comparisons, but **never blocks the push** (exit code `0`). Ideal for onboarding existing codebases.

### 2. Interactive Mode (`mode: interactive`)
When regressions, secrets, or suspicious changes are detected:
- Displays summary in the terminal.
- Prompts developer: `Approve push anyway? [y/N/inspect/open report]`.
- Allows inspecting error details or opening the standalone HTML evidence report.
- In non-interactive environments (CI, autonomous AI CLI without a TTY), falls back to `policies.interactive_fallback` (default: `block`).

### 3. Strict Mode (`mode: strict`)
Automatically blocks the push (exit code `1`) if:
- A critical existing workflow or route regresses.
- An unhandled browser console exception or failing network request occurs.
- A hardcoded credential or secret is detected.
- An existing automated test suite is deleted.

---

## Limitations & Bypass Disclosure

1. **Local Hook Bypass**:
   Git hooks run entirely client-side. Like any local Git hook, ProofPush can be bypassed using:
   - `git push --no-verify`
   - Modifying `.git/hooks/pre-push`
   - Pushing via remote web hosting APIs (GitHub/GitLab web editor).
   *Local-only protection is not a substitute for remote branch protection rules in multi-developer teams.*

2. **Port Clashing**:
   If background development servers are already listening on default ports, ProofPush scans and binds to alternative available local ports dynamically.

---

## Verification & Automated Test Suite

ProofPush includes a comprehensive test suite covering 30 specific real-world scenarios:

```bash
proofpush test
```

### Verified Test Scenarios:
1. Normal human-initiated push.
2. Simulated autonomous AI CLI push.
3. Relevant change introducing no regression (`UNCHANGED`).
4. New feature functioning only in proposed version (`NEW_FUNCTIONALITY`).
5. Existing feature breaking after change (`REGRESSION`).
6. Visual-only layout change detection with bounding box calculation.
7. Unrelated code change flagged during focused task.
8. Relevant change causing regression in another workflow.
9. Renamed route detection.
10. Unexpectedly removed route detection (`REMOVED_FUNCTIONALITY`).
11. Changed route authentication requirement (401/403 or redirect).
12. Failed application startup handling with captured error logs.
13. Database isolation preventing host pollution.
14. Clear handling when baseline Git revision cannot be established.
15. Seamless fallback to local rule engine when no AI provider is configured.
16. Unhandled browser console exception detection.
17. Intentionally changed workflow classification (`EXPECTED_CHANGE`).
18. Hardcoded API key and secret detection in diff.
19. Force push detection when remote ref is not an ancestor.
20. Multiple commits in a single outgoing push ref update.
21. Uncommitted working tree modifications cleanly ignored.
22. Strict mode push blocking on critical regression (exit code 1).
23. Advisory mode push allowance with logged warnings (exit code 0).
24. Preservation and chaining of pre-existing Git hooks (`pre-push.legacy`).
25. Direct push bypass using `--no-verify`.
26. Graceful handling of unavailable browser binaries or ports.
27. Prompt injection defense in commit messages and task descriptions.
28. Safety filtering against destructive interactive exploratory actions.
29. Complete cleanup of temporary worktrees and child processes.
30. Verification that blocked pushes do not update remote Git refs.
