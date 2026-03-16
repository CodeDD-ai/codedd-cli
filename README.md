# CodeDD CLI

**Run code audits from your terminal.** The CodeDD CLI lets you define scope locally, run file-level and complexity analysis on your machine (using your own LLM API keys), and sync results to [CodeDD](https://codedd.ai) for consolidation, recommendations, and dashboards.

---

## What is CodeDD CLI?

CodeDD CLI is the official command-line interface for the CodeDD platform. You:

- **Define scope** — Add one or more local Git repository roots to an audit.
- **Run analysis locally** — File audits (LLM-based) and complexity metrics run on your machine; only metadata and results are sent to CodeDD.
- **Sync to CodeDD** — Scope metadata, audit results, complexity data, dependencies, and architecture are submitted to CodeDD, where consolidation, dependency enrichment, security scoring, and recommendations run on the server.

Ideal for teams who want to keep source code local while still using CodeDD’s analytics, recommendations, and reporting.

---

## Features

- **Scope management** — Add/remove local directories, sync with CodeDD, detect changes and re-confirm scope (delta updates).
- **Local file auditing** — LLM-based file analysis using your Anthropic or OpenAI API keys; supports batching and progress feedback.
- **Complexity analysis** — Cyclomatic complexity and Halstead metrics (Radon/Lizard) run locally and are submitted to CodeDD.
- **Dependency scanning** — Local lockfile/manifest and import parsing; dependency data is sent to CodeDD for vulnerability and license analysis.
- **Architecture analysis** — Local component/relationship extraction with optional LLM enhancement; Phase 3 synthesis and storage on CodeDD.
- **Payment and budget** — Pre-flight checks, LoC budget deduction, or Stripe checkout when additional payment is required.
- **Secure auth** — CLI tokens stored in the OS credential store (Windows Credential Locker, macOS Keychain, Linux Secret Service).

---

## Installation

### Requirements

- **Python 3.11+**
- A [CodeDD](https://codedd.ai) account and a CLI token (Account → CLI Access → Generate Token)

### From source (development)

```bash
git clone https://gitlab.com/codedd1/codedd-cli
cd codedd-cli
pip install -e .
```

### From PyPI (when available)

```bash
pip install codedd-cli
```

Verify:

```bash
codedd --version
```

---

## Quick start (recommended workflow)

### 1. Authenticate once

Generate a CLI token at [codedd.ai](https://codedd.ai) (Account -> CLI Access), then:

```bash
codedd auth login --token <your_token>
```

Or run `codedd auth login` and paste the token when prompted.

Optional sanity check:

```bash
codedd auth status
```

### 2. Select the active audit context

```bash
codedd audits list
codedd audits select
```

Choose a **group audit** (multiple repos) or a **single audit** (one repo). The selected audit becomes the active context used by all `scope` and `audit` commands.

### 3. Define local scope

Add local paths that correspond to the repositories in that audit (each path must be a Git repository root with commits):

```bash
codedd scope add /path/to/my-repo
codedd scope list
codedd scope confirm
```

`scope confirm` performs a metadata scan (paths, file types, LoC) and registers scope with CodeDD.  
If files change later, `codedd audit start` auto-checks sync and prompts for re-confirmation when needed.

### 4. Configure LLM key(s)

Configure at least one provider key (used for local file-level auditing):

```bash
codedd config set-key anthropic
# or: codedd config set-key openai
```

Optional (recommended if both are configured):

```bash
codedd config provider both
```

### 5. Start the audit

```bash
codedd audit start
```

The CLI will:

- Sync scope with CodeDD (and prompt to re-confirm if local files changed).
- Run pre-flight checks (payment, LoC budget).
- Optionally open payment in the browser or deduct from budget.
- Fetch the plan, run local analysis, submit structured results, and trigger server-side post-processing.

Results and recommendations are available in the CodeDD dashboard; you can also run `codedd audits list` to see status.

---

## Workflow overview

Use this exact order for a predictable run:

```text
1. codedd auth login
2. codedd audits select
3. codedd scope add <repo-path> [more paths...]
4. codedd scope confirm
5. codedd config set-key <anthropic|openai>   # at least one
6. codedd audit start
```

What `codedd audit start` does, in order:

```text
A. Auto-sync scope -> if changed, asks to re-confirm
B. Pre-flight on CodeDD -> checks status/payment/budget
C. Payment path -> budget deduction OR checkout flow
D. Local execution -> file audit (LLM), complexity, dependencies, git stats, architecture
E. Submission -> sends structured outputs to CodeDD
F. Completion -> triggers server-side consolidation/recommendations
```

If you update files after confirming scope:

```text
Run: codedd audit start
-> CLI detects drift
-> Re-confirm prompt appears
-> Continue with updated scope
```

| Step              | Where it runs | What happens |
|-------------------|---------------|--------------|
| Scope add/confirm | Local         | Scan dirs, count files/LoC; register or delta-update scope on CodeDD. |
| Pre-flight        | CodeDD        | Check payment, budget, status. |
| File audit        | Local         | LLM (Anthropic/OpenAI) analyses each file; results sent to CodeDD. |
| Complexity        | Local         | Radon/Lizard; metrics sent to CodeDD. |
| Dependencies      | Local + CodeDD| Lockfiles/imports scanned locally; package/vuln data stored and enriched on CodeDD. |
| Git statistics    | Local + CodeDD| Commit/churn/collaboration metrics collected locally, then submitted. |
| Architecture      | Local + CodeDD| Components/relations extracted locally; persisted and processed on CodeDD. |
| Recommendations   | CodeDD        | Consolidation, technical debt, security, licenses, etc. |

---

## Commands reference

### Authentication

| Command | Description |
|--------|-------------|
| `codedd auth login` | Log in with a CLI token (prompt or `--token`) |
| `codedd auth logout` | Clear stored credentials |
| `codedd auth status` | Show current account and token state |

### Audits

| Command | Description |
|--------|-------------|
| `codedd audits list` | List audits (`--type single\|group`, `--limit`, `--page`) |
| `codedd audits select [uuid]` | Set active audit (interactive if UUID omitted) |

### Scope

| Command | Description |
|--------|-------------|
| `codedd scope add <path> [path ...]` | Add Git repository root(s) to the active audit’s scope |
| `codedd scope remove <n>` | Remove directory by list number |
| `codedd scope list` | List directories in scope |
| `codedd scope clear` | Remove all directories from scope |
| `codedd scope status` | Show scope and sync state per directory |
| `codedd scope confirm` | Scan, preview, and register scope with CodeDD |
| `codedd scope sync` | Compare local vs CodeDD and show changes |

### Audit execution

| Command | Description |
|--------|-------------|
| `codedd audit start` | Sync scope (if needed), pre-flight, pay/budget, then run full local audit and submit to CodeDD. Use `--skip-sync` to skip scope sync; `--yes` to auto-confirm; `--debug-llm` for LLM debug output. |

### Configuration

| Command | Description |
|--------|-------------|
| `codedd config show` | Show current config (API URL, active audit, scope, etc.) |
| `codedd config set <key> <value>` | Set a config value |
| `codedd config set-key [anthropic\|openai]` | Store an LLM API key in the OS keychain |
| `codedd config show-keys` | List which providers have keys configured (not the keys themselves) |
| `codedd config remove-key <anthropic\|openai>` | Remove a stored LLM API key from keychain |
| `codedd config provider [anthropic\|openai\|both]` | Set preferred LLM provider |
| `codedd config concurrency <n>` | Set max concurrent LLM requests (default 6) |

---

## Configuration

- **Config file:** `~/.codedd/config.toml` (TOML). Stores API URL, active audit, scope directories, LLM provider, concurrency. Permissions are restricted to the owner (Unix).
- **Secrets:** The CLI token and LLM API keys are stored in the system keychain (Windows Credential Locker, macOS Keychain, Linux Secret Service), not in the config file.

### Environment variables

| Variable | Purpose |
|---------|---------|
| `CODEDD_API_TOKEN` | Override the stored CLI token (e.g. for CI) |


---

## Security

- CLI tokens and LLM keys are stored in the OS credential store, not in plaintext on disk.
- TLS certificate verification is always enabled for API requests.
- Config file and `~/.codedd` directory use owner-only permissions where supported.
- Tokens expire after 90 days (server-configurable); re-generate from the CodeDD dashboard when needed.

---

## Development

```bash
pip install -e .
pip install pytest pytest-httpx pytest-mock ruff
pytest
ruff check .
```

---

## License

MIT License — see [LICENSE](LICENSE).

---

## Support

- **Issues:** [GitLab Issues](https://gitlab.com/codedd1/codedd-cli/-/work_items)
- **Product:** [CodeDD](https://codedd.ai)
