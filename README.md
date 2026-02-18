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

- **Python 3.10+**
- A [CodeDD](https://codedd.ai) account and a CLI token (Account → CLI Access → Generate Token)

### From source (development)

```bash
git clone https://github.com/codedd/codedd-cli.git
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

## Quick start

### 1. Authenticate

Generate a CLI token at [codedd.ai](https://codedd.ai) (Account → CLI Access), then:

```bash
codedd auth login --token <your_token>
```

Or run `codedd auth login` and paste the token when prompted.

### 2. Select an audit

```bash
codedd audits list
codedd audits select
```

Choose a **group audit** (multiple repos) or a **single audit** (one repo). The selected audit becomes the active context for scope and audit commands.

### 3. Define scope

Add the local paths that correspond to the audit’s repositories (each must be a Git repo root):

```bash
codedd scope add /path/to/my-repo
codedd scope list
codedd scope confirm
```

`scope confirm` scans the directories, shows a preview (files, LoC), and registers scope with CodeDD. If you change files later, run `codedd audit start` — it will offer to re-sync scope (delta update) before starting.

### 4. Run an audit

Configure at least one LLM API key (used for file-level auditing):

```bash
codedd config set-key anthropic
# or: codedd config set-key openai
```

Then start the audit:

```bash
codedd audit start
```

The CLI will:

- Sync scope with CodeDD (and prompt to re-confirm if local files changed).
- Run pre-flight checks (payment, LoC budget).
- Optionally open payment in the browser or deduct from budget.
- Fetch the audit plan, run file auditing and complexity analysis locally, submit results, submit dependencies, submit architecture, and trigger server-side post-processing (consolidation, recommendations, completion email).

Results and recommendations are available in the CodeDD dashboard; you can also run `codedd audits list` to see status.

---

## Workflow overview

High-level flow:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  LOCAL                                                                       │
│  1. codedd audits select     → Pick audit (group or single)                 │
│  2. codedd scope add <path>  → Add repo root(s)                              │
│  3. codedd scope confirm      → Scan & register scope with CodeDD            │
│  4. codedd audit start        → Sync (if needed) → Pre-flight → Pay/budget   │
│     └─ File audit (LLM)       → Local                                        │
│     └─ Complexity            → Local                                        │
│     └─ Submit results        → CodeDD                                       │
│     └─ Dependencies          → Local scan → Submit → CodeDD                │
│     └─ Architecture          → Local phases → Submit → CodeDD               │
│     └─ Complete              → CodeDD runs consolidation & recommendations   │
└─────────────────────────────────────────────────────────────────────────────┘
```

| Step              | Where it runs | What happens |
|-------------------|---------------|--------------|
| Scope add/confirm | Local         | Scan dirs, count files/LoC; register or delta-update scope on CodeDD. |
| Pre-flight        | CodeDD        | Check payment, budget, status. |
| File audit        | Local         | LLM (Anthropic/OpenAI) analyses each file; results sent to CodeDD. |
| Complexity        | Local         | Radon/Lizard; metrics sent to CodeDD. |
| Dependencies      | Local + CodeDD| Lockfiles/imports sent; CodeDD does metadata, vulns, licenses. |
| Architecture     | Local + CodeDD| Components/relations sent; CodeDD does Phase 3 and storage. |
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
| `CODEDD_API_URL`   | Override API base URL (default from config) |

---

## Security

- CLI tokens and LLM keys are stored in the OS credential store, not in plaintext on disk.
- TLS certificate verification is always enabled for API requests.
- Config file and `~/.codedd` directory use owner-only permissions where supported.
- Tokens expire after 90 days (server-configurable); re-generate from the CodeDD dashboard when needed.

---

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

---

## License

MIT License — see [LICENSE](LICENSE).

---

## Support

- **Issues:** [GitHub Issues](https://github.com/codedd/codedd-cli/issues)
- **Product:** [CodeDD](https://codedd.ai)
