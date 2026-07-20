"""
``codedd audit`` sub-commands: start.

Manages the audit lifecycle — pre-flight checks, payment, local file
auditing via LLM, and result submission to CodeDD.

Workflow:
    1. ``codedd scope confirm``        – register scope with CodeDD
    2. ``codedd audit start``          – auto-sync, pre-flight, pay/budget,
                                         fetch plan, audit locally, submit results
"""

import json
import os
import time
import webbrowser
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)
from rich.prompt import Confirm
from rich.table import Table

from codedd_cli.api.client import CodeDDClient
from codedd_cli.api.endpoints import Endpoints
from codedd_cli.auditor.complexity_analyzer import (
    FileComplexityResult,
    LocalComplexityAnalyzer,
    aggregate_complexity_results,
)
from codedd_cli.auditor.dependency_scanner import (
    LocalDependencyScanner,
    ManifestResult,
    ImportResult,
)
from codedd_cli.auditor.file_auditor import AuditFileResult, LocalFileAuditor
from codedd_cli.auditor.vulnerability_validator import (
    LocalVulnerabilityValidator,
    ValidationCandidate,
)
from codedd_cli.auth.session import require_auth
from codedd_cli.config.settings import ConfigManager
from codedd_cli.llm.key_manager import PROVIDER_MODELS, LLMKeyManager
from codedd_cli.utils.display import (
    STYLE_DEBUG_LOG,
    SYMBOL_FAIL,
    SYMBOL_INFO,
    SYMBOL_OK,
    SYMBOL_WARN,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from codedd_cli.utils.payload_inspector import review_payload, review_request

console = Console()
audit_app = typer.Typer(no_args_is_help=True)


def _require_active_audit(cfg: ConfigManager) -> None:
    """Abort if no audit has been selected."""
    if not cfg.active_audit_uuid:
        print_error(
            "No active audit.  Run [bold cyan]codedd audits select[/bold cyan] first."
        )
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# codedd audit start
# ---------------------------------------------------------------------------

@audit_app.command("start")
@require_auth
def start_audit(
    skip_sync: bool = typer.Option(
        False,
        "--skip-sync",
        help="Skip the automatic scope sync before starting.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Auto-confirm prompts (non-interactive mode).",
    ),
    show: bool = typer.Option(
        False,
        "--show",
        "-s",
        help="Write each API request/payload to a file and open it for review before sending.",
    ),
    debug_llm: bool = typer.Option(
        False,
        "--debug-llm",
        help="Print full LLM prompt, raw response, and parsed result to the CLI (for debugging).",
    ),
    debug_llm_full_prompt: bool = typer.Option(
        False,
        "--debug-llm-full-prompt",
        help="Include full proprietary system prompt in --debug-llm output (sensitive).",
    ),
) -> None:
    """
    Start the active audit on the CodeDD platform.

    This command:
      1. Auto-syncs local scope with CodeDD (detects file changes).
      2. Runs pre-flight checks (status, payment, LoC budget).
      3. Handles payment scenarios (budget deduction or Stripe checkout).
      4. Enqueues the audit for processing.
    """
    cfg = ConfigManager()
    _require_active_audit(cfg)

    audit_uuid = cfg.active_audit_uuid
    audit_name = cfg.active_audit_name
    audit_type = cfg.active_audit_type

    console.print(
        f"\n[bold]Starting audit:[/bold]  {audit_name}  [dim]({audit_type})[/dim]\n"
    )

    # -----------------------------------------------------------------------
    # Phase 1 — Auto-sync (unless skipped)
    # -----------------------------------------------------------------------
    if not skip_sync:
        sync_ok = _auto_sync(cfg, auto_confirm=yes, show=show)
        if not sync_ok:
            print_error("Scope sync failed or was cancelled.  Audit not started.")
            raise typer.Exit(code=1)
        console.print()

    # -----------------------------------------------------------------------
    # Phase 1b — Verify LLM API key availability
    # -----------------------------------------------------------------------
    llm_ok = _check_llm_keys(cfg)
    if not llm_ok:
        raise typer.Exit(code=1)
    console.print()

    # -----------------------------------------------------------------------
    # Phase 2 — Pre-flight check
    # -----------------------------------------------------------------------
    console.print("[dim]Pre-flight checks…[/dim]\n")

    if show:
        confirmed = review_request(
            "GET",
            Endpoints.AUDIT_CAN_START,
            params={"audit_uuid": audit_uuid},
            command_label="Audit Start — Pre-flight",
            context_note="This request checks whether the audit can be started (payment, scope, etc.). Only the audit UUID is sent.",
            confirm_prompt="Proceed with this request to CodeDD?",
        )
        if not confirmed:
            print_info("Cancelled.")
            raise typer.Exit(code=0)

    with CodeDDClient(config=cfg) as client:
        preflight = _fetch_preflight(client, audit_uuid)

    if preflight is None:
        raise typer.Exit(code=1)

    # Server returned an error (e.g. 500 with status/message)
    if preflight.get("status") == "error":
        print_error(preflight.get("message", "Pre-flight check failed."))
        raise typer.Exit(code=1)

    # Legacy or alternate error shape
    if "error" in preflight:
        print_error(preflight["error"])
        raise typer.Exit(code=1)

    # Malformed or unexpected response
    if "can_start" not in preflight:
        print_error(
            preflight.get("message", "Invalid pre-flight response from server.")
        )
        raise typer.Exit(code=1)

    _render_preflight(preflight)
    console.print()

    # -----------------------------------------------------------------------
    # Phase 3 — Decision tree
    # -----------------------------------------------------------------------
    # Use .get() so we don't KeyError when backend returns a minimal response
    # (e.g. can_start=False, reason="Not all repositories have been scoped").
    can_start = preflight.get("can_start", False)
    is_paid = preflight.get("is_paid", False)
    payment_required = preflight.get("payment_required", False)
    loc_delta = preflight.get("loc_delta", 0)
    loc_budget = preflight.get("loc_budget", 0)
    total_loc = preflight.get("total_lines_of_code", 0)

    use_budget = False

    if can_start and is_paid and loc_delta == 0:
        # Fully paid, no changes — good to go
        print_success("Audit is fully paid. Ready to start.")
        if not yes and not Confirm.ask("Start audit now?", default=True):
            print_info("Audit start cancelled.")
            raise typer.Exit()

    elif can_start and is_paid and loc_delta > 0:
        # Paid but scope grew, budget covers the delta
        print_warning(
            f"Scope increased by [bold]+{loc_delta:,}[/bold] LoC since payment.\n"
            f"  Your budget ([bold]{loc_budget:,}[/bold] LoC) can cover the difference."
        )
        if not yes and not Confirm.ask("Deduct from budget and start?", default=True):
            print_info("Audit start cancelled.")
            raise typer.Exit()
        use_budget = True

    elif can_start and not is_paid:
        # Not paid, budget covers the full audit
        print_info(
            f"Using LoC budget: [bold]{total_loc:,}[/bold] LoC "
            f"(budget: {loc_budget:,} LoC)."
        )
        if not yes and not Confirm.ask("Deduct from budget and start?", default=True):
            print_info("Audit start cancelled.")
            raise typer.Exit()
        use_budget = True

    elif payment_required and is_paid and loc_delta > 0:
        # Paid but scope grew beyond budget — need additional payment
        shortfall = loc_delta - loc_budget
        print_warning(
            f"Scope increased by [bold]+{loc_delta:,}[/bold] LoC since payment.\n"
            f"  Budget: {loc_budget:,} LoC | Shortfall: [bold red]{shortfall:,}[/bold red] LoC\n"
            f"  Additional payment is needed for the LoC difference."
        )
        if not Confirm.ask("Open payment page in your browser?", default=True):
            print_info("Audit start cancelled. You can pay at [bold cyan]codedd.ai[/bold cyan].")
            raise typer.Exit()

        paid_ok = _handle_checkout(cfg, audit_uuid, loc_delta, show=show)
        if not paid_ok:
            raise typer.Exit(code=1)

    elif payment_required and not is_paid:
        # Not paid, budget insufficient — full payment needed
        print_warning(
            f"LoC budget insufficient: [bold]{loc_budget:,}[/bold] available, "
            f"[bold]{total_loc:,}[/bold] needed.\n"
            f"  Payment required for this audit."
        )
        if not Confirm.ask("Open payment page in your browser?", default=True):
            print_info("Audit start cancelled. You can pay at [bold cyan]codedd.ai[/bold cyan].")
            raise typer.Exit()

        paid_ok = _handle_checkout(cfg, audit_uuid, total_loc, show=show)
        if not paid_ok:
            raise typer.Exit(code=1)

    else:
        # Fallback — preflight said cannot start
        print_error(preflight.get("reason", "Audit cannot be started."))
        raise typer.Exit(code=1)

    # -----------------------------------------------------------------------
    # Phase 4 — Budget deduction (if applicable)
    # -----------------------------------------------------------------------
    if use_budget:
        start_payload = {
            "audit_uuid": audit_uuid,
            "use_budget": True,
            "local_execution": True,  # budget only — CLI drives the audit locally
        }
        if show:
            confirmed = review_payload(
                start_payload,
                command_label="Audit Start — Budget Deduction",
                context_note="This payload tells CodeDD to deduct LoC from your budget. Only audit UUID and budget flag are sent. No server-side audit is triggered.",
                confirm_prompt="Proceed with budget deduction on CodeDD?",
            )
            if not confirmed:
                print_info("Cancelled.")
                raise typer.Exit(code=0)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("Deducting budget…", total=None)
            with CodeDDClient(config=cfg) as client:
                resp = client.post(Endpoints.AUDIT_START, json=start_payload)

        if resp.status_code != 200:
            msg = "Failed to start audit (budget deduction)"
            try:
                msg = resp.json().get("message", msg)
            except Exception:
                pass
            print_error(msg)
            raise typer.Exit(code=1)

        body = resp.json()
        if body.get("status") != "success":
            print_error(body.get("message", "Unknown error during budget deduction"))
            raise typer.Exit(code=1)

        loc_deducted = body.get("loc_deducted", 0)
        if loc_deducted:
            print_info(f"  {loc_deducted:,} LoC deducted from budget")

    # -----------------------------------------------------------------------
    # Phase 5 — Local file audit
    # -----------------------------------------------------------------------
    _run_local_audit(
        cfg,
        audit_uuid,
        show=show,
        debug_llm=debug_llm,
        debug_llm_full_prompt=debug_llm_full_prompt,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_llm_keys(cfg: ConfigManager) -> bool:
    """
    Verify that at least one LLM API key is configured and matches the
    provider preference.

    Displays which provider(s) will be used.  Returns ``False`` if no
    usable key is found.
    """
    preference = cfg.llm_provider
    configured = LLMKeyManager.get_configured_providers()

    if not configured:
        print_error(
            "No LLM API keys configured.\n"
            "  CodeDD uses LLMs (Anthropic or OpenAI) to run the source code audit.\n"
            "  Add an API key with [bold cyan]codedd config set-key[/bold cyan]."
        )
        return False

    # Determine which providers will actually be used
    if preference == "both":
        active = configured  # use whatever is available
    elif preference in configured:
        active = [preference]
    else:
        # Preference set to a provider without a key — fall back to what's available
        active = configured
        console.print(
            f"  [yellow]{SYMBOL_WARN}[/yellow] Preferred provider "
            f"[bold]{preference}[/bold] has no key stored.  "
            f"Falling back to: {', '.join(active)}"
        )

    # Display summary
    primary = active[0]
    fallback = active[1] if len(active) > 1 else None
    model_primary = PROVIDER_MODELS.get(primary, "")
    info_parts = [f"[bold]{primary}[/bold] [dim]({model_primary})[/dim]"]
    if fallback:
        model_fallback = PROVIDER_MODELS.get(fallback, "")
        info_parts.append(f"fallback: [bold]{fallback}[/bold] [dim]({model_fallback})[/dim]")

    console.print(
        f"  [green]{SYMBOL_OK}[/green] LLM provider(s): {' | '.join(info_parts)}"
    )
    return True


def _auto_sync(cfg: ConfigManager, auto_confirm: bool = False, show: bool = False) -> bool:
    """
    Run an automatic scope sync.  If changes are detected, prompt to
    re-confirm.

    Returns True if sync is OK (no changes or changes were re-confirmed).
    """
    # Import sync internals from scope_cmd
    from codedd_cli.commands.scope_cmd import (
        _compute_diff,
        _run_reconfirm,
    )
    from codedd_cli.scanner.file_walker import scan_repository

    dirs = cfg.scope_directories
    if not dirs:
        print_warning("No directories in scope. Run [bold cyan]codedd scope add[/bold cyan] first.")
        return False

    audit_uuid = cfg.active_audit_uuid

    console.print("[dim]Phase 1 — Syncing scope with CodeDD…[/dim]\n")

    if show:
        confirmed = review_request(
            "GET",
            Endpoints.SCOPE_FILES,
            params={"audit_uuid": audit_uuid},
            command_label="Audit Start — Sync Scope",
            context_note="This request fetches the registered scope from CodeDD to detect local changes. Only the audit UUID is sent.",
            confirm_prompt="Proceed with this request to CodeDD?",
        )
        if not confirmed:
            print_info("Cancelled.")
            return False

    # Fetch remote state
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Fetching remote scope…", total=None)
        with CodeDDClient(config=cfg) as client:
            resp = client.get(Endpoints.SCOPE_FILES, params={"audit_uuid": audit_uuid})

    if resp.status_code != 200:
        print_warning("Could not fetch remote scope. Skipping sync.")
        return True  # Non-fatal — let pre-flight catch issues

    body = resp.json()
    if body.get("status") != "success":
        print_warning("Remote scope fetch returned non-success. Skipping sync.")
        return True

    remote_sub_audits = body.get("sub_audits", [])
    remote_by_name: dict[str, list[dict]] = {}
    for sa in remote_sub_audits:
        remote_by_name[sa.get("repo_name", "")] = sa.get("files", [])

    # Scan local and compute diffs
    has_changes = False
    for entry in dirs:
        repo_path = entry["path"]
        repo_name = entry.get("repo_name", Path(repo_path).name)

        result = scan_repository(repo_path)

        local_files = {
            f.relative_path: {
                "lines_of_code": f.lines_of_code,
                "lines_of_doc": f.lines_of_doc,
            }
            for f in result.files
        }

        remote_files = {}
        for rf in remote_by_name.get(repo_name, []):
            remote_files[rf["relative_path"]] = {
                "lines_of_code": rf.get("lines_of_code", 0),
                "lines_of_doc": rf.get("lines_of_doc", 0),
            }

        diff = _compute_diff(local_files, remote_files)
        if diff["added"] or diff["removed"] or diff["changed"]:
            has_changes = True
            total_changes = len(diff["added"]) + len(diff["removed"]) + len(diff["changed"])
            console.print(
                f"  [bold yellow]{SYMBOL_WARN}[/bold yellow] [bold]{repo_name}[/bold]  "
                f"{total_changes} change(s) detected"
            )
        else:
            console.print(
                f"  [bold green]{SYMBOL_OK}[/bold green] [bold]{repo_name}[/bold]  in sync"
            )

    if has_changes:
        console.print()
        print_warning("Local files have changed since last registration.")
        if auto_confirm or Confirm.ask("Re-confirm scope now?", default=True):
            reconfirm_ok = _run_reconfirm(cfg, show=show)
            if not reconfirm_ok:
                print_error("Scope re-registration failed.  Cannot start audit.")
                return False
            return True
        else:
            return False

    return True


def _fetch_preflight(client: CodeDDClient, audit_uuid: str) -> dict | None:
    """
    Call the pre-flight endpoint and return the parsed response dict.
    Returns None on network/auth failure (error is already printed).
    """
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Running pre-flight checks…", total=None)
        resp = client.get(
            Endpoints.AUDIT_CAN_START,
            params={"audit_uuid": audit_uuid},
        )

    if resp.status_code == 401:
        print_error("Authentication failed.  Run [bold cyan]codedd auth login[/bold cyan].")
        return None

    try:
        return resp.json()
    except Exception:
        print_error("Invalid response from server.")
        return None


def _render_preflight(preflight: dict) -> None:
    """Display a summary table from the pre-flight check.

    Handles partial responses when the backend blocks early (e.g. not all
    repos scoped) and omits total_lines_of_code, is_paid, loc_budget, etc.
    """
    table = Table(title="Pre-flight Summary", show_header=True, padding=(0, 1))
    table.add_column("Item", style="bold")
    table.add_column("Value", justify="right")

    total_loc = preflight.get("total_lines_of_code")
    if total_loc is not None:
        table.add_row("Total LoC", f"{total_loc:,}")
    table.add_row("Repositories", str(len(preflight.get("sub_audits", []))))

    is_paid = preflight.get("is_paid")
    if is_paid is not None:
        table.add_row(
            "Payment",
            "[green]Paid[/green]" if is_paid else "[yellow]Unpaid[/yellow]",
        )
        if is_paid and preflight.get("lines_purchased", 0) > 0:
            table.add_row("Lines purchased", f"{preflight['lines_purchased']:,}")

    loc_delta = preflight.get("loc_delta", 0)
    if loc_delta is not None and loc_delta > 0:
        table.add_row("Scope delta", f"[yellow]+{loc_delta:,}[/yellow]")

    loc_budget = preflight.get("loc_budget")
    if loc_budget is not None:
        table.add_row("LoC budget", f"{loc_budget:,}")

    table.add_row(
        "Status",
        "[green]Ready[/green]" if preflight.get("can_start") else "[red]Blocked[/red]",
    )

    console.print(table)


def _handle_checkout(
    cfg: ConfigManager,
    audit_uuid: str,
    lines_of_code: int,
    show: bool = False,
) -> bool:
    """
    Create a Stripe checkout session, open in the browser, and poll
    until payment is confirmed.

    Returns True if payment was confirmed, False otherwise.
    """
    console.print()

    checkout_payload = {"audit_uuid": audit_uuid, "lines_of_code": lines_of_code}
    if show:
        confirmed = review_payload(
            checkout_payload,
            command_label="Audit Checkout",
            context_note="This payload requests a payment checkout session for the given audit and line count.",
            confirm_prompt="Proceed with creating the payment session on CodeDD?",
        )
        if not confirmed:
            print_info("Cancelled.")
            return False

    # Create checkout session
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Creating payment session…", total=None)
        with CodeDDClient(config=cfg) as client:
            resp = client.post(
                Endpoints.AUDIT_CHECKOUT,
                json=checkout_payload,
            )

    if resp.status_code != 200:
        msg = "Failed to create checkout session"
        try:
            msg = resp.json().get("message", msg)
        except Exception:
            pass
        print_error(msg)
        return False

    body = resp.json()
    checkout_url = body.get("checkout_url", "")
    if not checkout_url:
        print_error("No checkout URL received from server.")
        return False

    # Open browser
    print_info(f"Opening payment page…\n  [link={checkout_url}]{checkout_url}[/link]\n")
    try:
        webbrowser.open(checkout_url)
    except Exception:
        print_warning("Could not open browser automatically. Please visit the URL above.")

    # Poll for payment confirmation
    console.print("[dim]Waiting for payment confirmation…  (Ctrl+C to cancel)[/dim]\n")

    poll_interval = 5  # seconds
    max_wait = 600  # 10 minutes
    elapsed = 0

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Waiting for payment…", total=None)

            while elapsed < max_wait:
                time.sleep(poll_interval)
                elapsed += poll_interval

                with CodeDDClient(config=cfg) as client:
                    poll_resp = client.get(
                        Endpoints.AUDIT_PAYMENT_STATUS,
                        params={"audit_uuid": audit_uuid},
                    )

                if poll_resp.status_code == 200:
                    poll_body = poll_resp.json()
                    if poll_body.get("is_paid"):
                        print_success("Payment confirmed!")
                        return True

                progress.update(
                    task, description=f"Waiting for payment… ({elapsed}s)"
                )

    except KeyboardInterrupt:
        console.print()
        print_warning("Payment polling cancelled.")
        print_info("You can complete payment at [bold cyan]codedd.ai[/bold cyan] and retry.")
        return False

    print_warning(
        "Payment was not confirmed within the timeout.\n"
        "  Complete payment at [bold cyan]codedd.ai[/bold cyan] and run this command again."
    )
    return False


# ---------------------------------------------------------------------------
# Local audit execution
# ---------------------------------------------------------------------------

def _run_local_audit(
    cfg: ConfigManager,
    audit_uuid: str,
    show: bool = False,
    debug_llm: bool = False,
    debug_llm_full_prompt: bool = False,
) -> None:
    """
    Execute the full local audit flow:
        1. Fetch the audit plan from CodeDD (file list + system prompt).
        2. Audit each file locally via LLM (with concurrency + progress bar).
        3. Submit results in batches to CodeDD.
        4. Signal completion (triggers post-processing Steps 7-9).
    """
    console.print()

    # ---- Step 1: Fetch audit plan ----------------------------------------
    console.print("[bold]Fetching audit plan from CodeDD…[/bold]\n")

    if show:
        confirmed = review_request(
            "GET",
            Endpoints.AUDIT_PLAN,
            params={"audit_uuid": audit_uuid},
            command_label="Audit Plan",
            context_note=(
                "This request fetches the audit execution plan (file list, "
                "system prompt, config) from CodeDD.  Only the audit UUID is sent."
            ),
            confirm_prompt="Proceed with fetching the audit plan?",
        )
        if not confirmed:
            print_info("Cancelled.")
            raise typer.Exit(code=0)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Downloading plan…", total=None)
        with CodeDDClient(config=cfg) as client:
            resp = client.get(
                Endpoints.AUDIT_PLAN,
                params={"audit_uuid": audit_uuid},
            )

    if resp.status_code != 200:
        msg = "Failed to fetch audit plan"
        try:
            msg = resp.json().get("message", msg)
        except Exception:
            pass
        print_error(msg)
        raise typer.Exit(code=1)

    plan = resp.json()
    if plan.get("status") != "success":
        print_error(plan.get("message", "Unexpected response from server"))
        raise typer.Exit(code=1)

    system_prompt = plan.get("prompts", {}).get("file_audit", "")
    vulnerability_validation_prompt = plan.get("prompts", {}).get("vulnerability_validation", "")
    if not system_prompt:
        print_error("Server returned an empty system prompt.  Cannot audit.")
        raise typer.Exit(code=1)

    sub_audits = plan.get("sub_audits", [])
    plan_config = plan.get("config", {})
    batch_size = plan_config.get("batch_size", 10)
    server_max = plan_config.get("max_concurrent", 8)
    # Use the local config value, capped by the server-side maximum
    max_concurrent = min(cfg.llm_concurrency, server_max)

    # Flatten file list and build scope_dirs mapping
    all_files: list[dict] = []
    scope_dirs: dict[str, str] = {}
    total_loc = 0

    local_dirs = cfg.scope_directories
    dir_by_name = {e.get("repo_name", ""): e.get("path", "") for e in local_dirs}

    for sa in sub_audits:
        repo_name = sa.get("repo_name", "")
        local_path = dir_by_name.get(repo_name, "")
        scope_dirs[repo_name] = local_path

        for f in sa.get("files", []):
            f["repo_name"] = repo_name
            f["sub_audit_uuid"] = sa["audit_uuid"]
            all_files.append(f)
            total_loc += f.get("lines_of_code", 0)

    if not all_files:
        print_warning("No files to audit.  Check your scope selection.")
        raise typer.Exit(code=0)

    # Retrieve LLM API keys now (keyring can be slow on Windows; do it before "Auditing files…")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Retrieving API keys…", total=None)
        anthropic_key = LLMKeyManager.retrieve_key("anthropic")
        openai_key = LLMKeyManager.retrieve_key("openai")

    # Display summary
    repo_names = [sa.get("repo_name", "?") for sa in sub_audits]
    console.print(
        f"  Repositories: [bold]{len(sub_audits)}[/bold] ({', '.join(repo_names)})"
    )
    console.print(f"  Files to audit: [bold]{len(all_files)}[/bold]")
    console.print(f"  Total LoC: [bold]{total_loc:,}[/bold]")
    console.print(f"  Concurrency: [bold]{max_concurrent}[/bold] parallel LLM calls\n")

    # ---- Step 2: Audit files locally with progress -----------------------
    console.print("[bold]Auditing files…[/bold]\n")

    preference = cfg.llm_provider
    # anthropic_key, openai_key already retrieved above (avoids slow keyring delay here)
    provider_stats: dict[str, int] = {}
    failed_files: list[AuditFileResult] = []
    successful_results: list[AuditFileResult] = []
    completed_count = 0
    debug_lines: list[str] = []
    start_time = time.monotonic()

    # Build a Rich Live display with a progress bar + debug log
    from rich.live import Live

    def _build_display() -> Table:
        """Build the live display table (progress + last debug lines)."""
        grid = Table.grid(padding=(0, 0))
        grid.add_column()

        # Progress bar row
        pct = int(completed_count / len(all_files) * 100) if all_files else 0
        elapsed_s = time.monotonic() - start_time
        elapsed_m, elapsed_sec = divmod(int(elapsed_s), 60)

        bar_width = 30
        filled = int(bar_width * completed_count / len(all_files)) if all_files else 0
        bar = "[green]" + "━" * filled + "[/green]" + "[dim]━[/dim]" * (bar_width - filled)

        ok_count = len(successful_results)
        fail_count = len(failed_files)
        status_str = f"[green]{ok_count} ok[/green]"
        if fail_count:
            status_str += f"  [red]{fail_count} failed[/red]"

        grid.add_row(
            f"  {bar}  [bold]{completed_count}[/bold]/{len(all_files)}  "
            f"({pct}%)  {status_str}  "
            f"[dim]{elapsed_m}m {elapsed_sec:02d}s[/dim]"
        )

        # Last completed file
        if successful_results or failed_files:
            last = (successful_results + failed_files)[-1]
            short = last.relative_path
            if len(short) > 60:
                short = "..." + short[-57:]
            if last.success:
                prov = last.provider_used or "?"
                grid.add_row(
                    f"  [green]{SYMBOL_OK}[/green] {short}  "
                    f"[dim]({prov})[/dim]"
                )
            else:
                grid.add_row(
                    f"  [red]{SYMBOL_FAIL}[/red] {short}  "
                    f"[dim]{last.error or ''}[/dim]"
                )

        # Debug log (last 6 lines) — light grey so they don't compete with progress
        if debug_lines:
            grid.add_row("")
            for line in debug_lines[-6:]:
                grid.add_row(f"  [{STYLE_DEBUG_LOG}]{line}[/{STYLE_DEBUG_LOG}]")

        return grid

    def _on_debug(msg: str) -> None:
        """Receive debug messages from the auditor."""
        debug_lines.append(msg)
        # Keep buffer bounded
        if len(debug_lines) > 50:
            debug_lines[:] = debug_lines[-30:]

    def _on_progress(result: AuditFileResult) -> None:
        """Callback invoked after each file completes."""
        nonlocal completed_count
        completed_count += 1
        if result.success:
            provider = result.provider_used or "unknown"
            provider_stats[provider] = provider_stats.get(provider, 0) + 1
            successful_results.append(result)
        else:
            failed_files.append(result)

    def _on_dump_llm(
        full_prompt: str,
        response_text: str,
        audit_data: dict | None,
        none_count: int,
    ) -> None:
        """Print full prompt, raw response, and parse result to the CLI (debug)."""
        sep = "=" * 60
        console.print(f"\n[bold cyan]{sep}[/bold cyan]")
        console.print("[bold cyan]  LLM DEBUG DUMP[/bold cyan]")
        console.print(f"[bold cyan]{sep}[/bold cyan]\n")
        console.print("[bold]--- PROMPT sent to LLM ---[/bold]")
        if debug_llm_full_prompt:
            console.print(f"[dim]{full_prompt}[/dim]\n")
        else:
            console.print(
                "[dim][system prompt redacted] Use --debug-llm-full-prompt to include it.[/dim]\n"
            )
        console.print("[bold]--- RAW RESPONSE from LLM ---[/bold]")
        console.print(f"[dim]{response_text}[/dim]\n")
        console.print("[bold]--- PARSED RESULT (response_parser) ---[/bold]")
        console.print(f"  none_response_count: [bold]{none_count}[/bold]")
        if audit_data is not None:
            try:
                console.print("[dim]" + json.dumps(audit_data, indent=2) + "[/dim]")
            except (TypeError, ValueError):
                console.print(f"[dim]{audit_data}[/dim]")
        else:
            console.print("  [red]None[/red] (parse failed or incomplete)")
        console.print()

    with LocalFileAuditor(
        anthropic_key=anthropic_key,
        openai_key=openai_key,
        system_prompt=system_prompt,
        provider_preference=preference,
        max_concurrent=max_concurrent,
        on_debug=_on_debug,
        on_dump_llm=_on_dump_llm if debug_llm else None,
    ) as auditor:
        with Live(
            _build_display(),
            console=console,
            refresh_per_second=4,
            transient=False,
        ) as live:
            # Run a background refresh so the display updates during LLM calls
            import threading

            _stop_refresh = threading.Event()

            def _refresh_loop() -> None:
                while not _stop_refresh.is_set():
                    live.update(_build_display())
                    _stop_refresh.wait(0.3)

            refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
            refresh_thread.start()

            try:
                results = auditor.audit_batch(
                    files=all_files,
                    scope_dirs=scope_dirs,
                    on_progress=_on_progress,
                )
            finally:
                _stop_refresh.set()
                refresh_thread.join(timeout=2)
                live.update(_build_display())

    elapsed = time.monotonic() - start_time
    console.print()

    if failed_files:
        print_warning(f"{len(failed_files)} file(s) could not be audited:")
        for f in failed_files[:10]:
            console.print(f"    [dim]{f.relative_path}[/dim]  — {f.error}")
        if len(failed_files) > 10:
            console.print(f"    … and {len(failed_files) - 10} more")
        console.print()

    if not successful_results:
        print_error("No files were successfully audited.  Nothing to submit.")
        raise typer.Exit(code=1)

    # ---- Step 2b: Local complexity analysis (cyclomatic + Halstead) ------
    console.print("[bold]Analysing code complexity…[/bold]\n")

    cx_debug_lines: list[str] = []
    cx_completed = 0
    cx_ok_count = 0
    cx_fail_count = 0
    cx_start = time.monotonic()

    def _cx_on_debug(msg: str) -> None:
        cx_debug_lines.append(msg)
        if len(cx_debug_lines) > 30:
            cx_debug_lines[:] = cx_debug_lines[-20:]

    def _cx_build_display() -> Table:
        grid = Table.grid(padding=(0, 0))
        grid.add_column()
        pct = int(cx_completed / len(all_files) * 100) if all_files else 0
        elapsed_s = time.monotonic() - cx_start
        elapsed_m, elapsed_sec = divmod(int(elapsed_s), 60)
        bar_w = 30
        filled = int(bar_w * cx_completed / len(all_files)) if all_files else 0
        bar = "[green]" + "━" * filled + "[/green]" + "[dim]━[/dim]" * (bar_w - filled)
        status_str = f"[green]{cx_ok_count} ok[/green]"
        if cx_fail_count:
            status_str += f"  [red]{cx_fail_count} skipped[/red]"
        grid.add_row(
            f"  {bar}  [bold]{cx_completed}[/bold]/{len(all_files)}  "
            f"({pct}%)  {status_str}  "
            f"[dim]{elapsed_m}m {elapsed_sec:02d}s[/dim]"
        )
        if cx_debug_lines:
            for line in cx_debug_lines[-3:]:
                grid.add_row(f"  [{STYLE_DEBUG_LOG}]{line}[/{STYLE_DEBUG_LOG}]")
        return grid

    cx_results: list[FileComplexityResult] = []

    def _cx_on_progress(result: FileComplexityResult) -> None:
        nonlocal cx_completed, cx_ok_count, cx_fail_count
        cx_completed += 1
        if result.success:
            cx_ok_count += 1
        else:
            cx_fail_count += 1
        cx_results.append(result)

    cx_analyzer = LocalComplexityAnalyzer(
        max_workers=min(max_concurrent, os.cpu_count() or 4, 8),
        on_debug=_cx_on_debug,
    )

    with Live(
        _cx_build_display(),
        console=console,
        refresh_per_second=4,
        transient=False,
    ) as cx_live:
        import threading as _cx_threading

        _cx_stop = _cx_threading.Event()

        def _cx_refresh() -> None:
            while not _cx_stop.is_set():
                cx_live.update(_cx_build_display())
                _cx_stop.wait(0.3)

        cx_refresh_t = _cx_threading.Thread(target=_cx_refresh, daemon=True)
        cx_refresh_t.start()
        try:
            cx_results = cx_analyzer.analyze_batch(
                files=all_files,
                scope_dirs=scope_dirs,
                on_progress=_cx_on_progress,
            )
        finally:
            _cx_stop.set()
            cx_refresh_t.join(timeout=2)
            cx_live.update(_cx_build_display())

    cx_elapsed = time.monotonic() - cx_start
    cx_ok = [r for r in cx_results if r.success]
    console.print()
    if cx_ok:
        console.print(
            f"  [green]{SYMBOL_OK}[/green] Complexity analysed: "
            f"[bold]{len(cx_ok)}[/bold] files  "
            f"[dim]({int(cx_elapsed)}s)[/dim]"
        )
    if cx_fail_count:
        console.print(
            f"  [yellow]{SYMBOL_WARN}[/yellow] {cx_fail_count} file(s) skipped "
            f"(non-source or unreadable)"
        )
    console.print()

    # ---- Step 2c: Dependency scanning (manifests + imports + OSV) ---------
    console.print("[bold]Scanning dependencies…[/bold]\n")

    dep_debug_lines: list[str] = []
    dep_start = time.monotonic()

    def _dep_on_debug(msg: str) -> None:
        dep_debug_lines.append(msg)
        if len(dep_debug_lines) > 30:
            dep_debug_lines[:] = dep_debug_lines[-20:]

    def _dep_build_display() -> Table:
        grid = Table.grid(padding=(0, 0))
        grid.add_column()
        elapsed_s = time.monotonic() - dep_start
        elapsed_m, elapsed_sec = divmod(int(elapsed_s), 60)
        if dep_debug_lines:
            for line in dep_debug_lines[-4:]:
                grid.add_row(f"  [{STYLE_DEBUG_LOG}]{line}[/{STYLE_DEBUG_LOG}]")
        grid.add_row(f"  [dim]{elapsed_m}m {elapsed_sec:02d}s[/dim]")
        return grid

    # Fetch dependency config from an authenticated internal endpoint.
    # 404 means backend doesn't support it yet; defaults remain safe.
    dep_config: dict = {}
    try:
        with CodeDDClient(config=cfg) as client:
            dep_cfg_resp = client.get(Endpoints.AUDIT_DEPENDENCY_CONFIG)
        if dep_cfg_resp.status_code == 200:
            dep_cfg_body = dep_cfg_resp.json()
            if dep_cfg_body.get("status") == "success":
                dep_config = dep_cfg_body.get("config", {})
                _dep_on_debug(f"Dependency config loaded (version {dep_cfg_body.get('version', '?')})")
        elif dep_cfg_resp.status_code == 404:
            _dep_on_debug("Server does not support dependency config (404) — using defaults")
        if not dep_config and dep_cfg_resp.status_code != 404:
            _dep_on_debug("Warning: could not fetch dependency config — using defaults")
    except Exception as dep_cfg_exc:
        _dep_on_debug(f"Warning: dependency config fetch failed: {dep_cfg_exc}")

    dep_scanner = LocalDependencyScanner(config=dep_config, on_debug=_dep_on_debug)

    manifest_results: list[ManifestResult] = []
    import_results: list[ImportResult] = []
    vuln_results: dict[str, dict] = {}

    with Live(
        _dep_build_display(),
        console=console,
        refresh_per_second=4,
        transient=False,
    ) as dep_live:
        import threading as _dep_threading

        _dep_stop = _dep_threading.Event()

        def _dep_refresh() -> None:
            while not _dep_stop.is_set():
                dep_live.update(_dep_build_display())
                _dep_stop.wait(0.3)

        dep_refresh_t = _dep_threading.Thread(target=_dep_refresh, daemon=True)
        dep_refresh_t.start()

        try:
            # 1. Scan manifest files
            _dep_on_debug("Scanning manifest files…")
            manifest_results = dep_scanner.scan_manifests(scope_dirs)

            total_manifest_pkgs = sum(len(m.packages) for m in manifest_results if not m.error)
            _dep_on_debug(
                f"Found {len(manifest_results)} manifest(s), "
                f"{total_manifest_pkgs} packages"
            )

            # 2. Extract imports from source files
            _dep_on_debug("Extracting imports from source files…")
            import_results = dep_scanner.scan_source_imports(all_files, scope_dirs)

            # 3. Scan vulnerabilities via OSV
            if manifest_results or import_results:
                _dep_on_debug("Querying OSV for vulnerabilities…")
                vuln_results = dep_scanner.scan_vulnerabilities(manifest_results, import_results)
                vuln_with_issues = sum(
                    1 for v in vuln_results.values() if v.get("vulnerability_count", 0) > 0
                )
                _dep_on_debug(
                    f"Vulnerability scan complete: {len(vuln_results)} packages checked, "
                    f"{vuln_with_issues} with known vulnerabilities"
                )
        finally:
            _dep_stop.set()
            dep_refresh_t.join(timeout=2)
            dep_live.update(_dep_build_display())

    dep_elapsed = time.monotonic() - dep_start
    console.print()

    manifest_ok = [m for m in manifest_results if not m.error]
    total_dep_pkgs = sum(len(m.packages) for m in manifest_ok)
    total_import_pkgs = sum(len(r.packages) for r in import_results)
    vuln_found = sum(1 for v in vuln_results.values() if v.get("vulnerability_count", 0) > 0)

    if manifest_ok or import_results:
        console.print(
            f"  [green]{SYMBOL_OK}[/green] Dependencies scanned: "
            f"[bold]{total_dep_pkgs}[/bold] from manifests, "
            f"[bold]{total_import_pkgs}[/bold] from imports  "
            f"[dim]({int(dep_elapsed)}s)[/dim]"
        )
        if vuln_found:
            console.print(
                f"  [yellow]{SYMBOL_WARN}[/yellow] {vuln_found} package(s) with known vulnerabilities"
            )
    else:
        console.print(
            f"  [dim]{SYMBOL_INFO}[/dim] No dependency manifests found  "
            f"[dim]({int(dep_elapsed)}s)[/dim]"
        )
    console.print()

    # ---- Step 2d: Collect and submit git statistics (local repos) -------
    from codedd_cli.auditor.git_stats_collector import collect_git_statistics

    console.print("[bold]Collecting git statistics…[/bold]\n")
    git_submitted = 0
    git_skipped = 0
    for sa in sub_audits:
        sub_uuid = sa.get("audit_uuid", "")
        repo_name = sa.get("repo_name", "")
        path = scope_dirs.get(repo_name, "")
        if not path or not os.path.isdir(path):
            git_skipped += 1
            continue
        stats = collect_git_statistics(
            path,
            repository_name=repo_name or None,
            repository_url=None,
            on_debug=None,
        )
        if not stats:
            git_skipped += 1
            continue
        payload = {
            "audit_uuid": sub_uuid,
            "repository_name": stats.get("repository_name"),
            "repository_url": stats.get("repository_url"),
            "commit_history": stats.get("commit_history", {}),
            "author_stats": stats.get("author_stats", {}),
            "merge_stats": stats.get("merge_stats", {}),
            "branch_stats": stats.get("branch_stats", {}),
            "meta_info": stats.get("meta_info", {}),
            "time_based_stats": stats.get("time_based_stats", {}),
            "release_stats": stats.get("release_stats", {}),
            "code_churn_stats": stats.get("code_churn_stats", {}),
            "collaboration_stats": stats.get("collaboration_stats", {}),
        }
        try:
            with CodeDDClient(config=cfg) as client:
                resp = client.post(Endpoints.AUDIT_GIT_STATISTICS, json=payload)
            if resp.status_code == 200:
                git_submitted += 1
            else:
                try:
                    msg = resp.json().get("message", "git statistics submission failed")
                except Exception:
                    msg = "git statistics submission failed"
                print_warning(f"Git stats for {repo_name}: {msg}")
        except Exception as e:
            print_warning(f"Git stats for {repo_name}: {e}")
    if git_submitted:
        console.print(
            f"  [green]{SYMBOL_OK}[/green] Git statistics submitted: "
            f"[bold]{git_submitted}[/bold] repo(s)"
        )
    if git_skipped and not git_submitted:
        console.print(
            f"  [dim]{SYMBOL_INFO}[/dim] No git repositories in scope or collection skipped"
        )
    console.print()

    # ---- Step 2e: Architecture analysis (Phase 1+2+synthesis local; server does Phase 3 storage) --
    from codedd_cli.auditor.architecture_analyzer import run_architecture_analysis

    console.print("[bold]Analysing architecture…[/bold]\n")

    arch_debug_lines: list[str] = []
    arch_start = time.monotonic()

    def _arch_on_debug(msg: str) -> None:
        arch_debug_lines.append(msg)
        if len(arch_debug_lines) > 30:
            arch_debug_lines[:] = arch_debug_lines[-20:]

    def _arch_on_progress(msg: str) -> None:
        _arch_on_debug(msg)

    def _arch_build_display() -> Table:
        grid = Table.grid(padding=(0, 0))
        grid.add_column()
        elapsed_s = time.monotonic() - arch_start
        elapsed_m, elapsed_sec = divmod(int(elapsed_s), 60)
        if arch_debug_lines:
            for line in arch_debug_lines[-5:]:
                grid.add_row(f"  [{STYLE_DEBUG_LOG}]{line}[/{STYLE_DEBUG_LOG}]")
        grid.add_row(f"  [dim]{elapsed_m}m {elapsed_sec:02d}s[/dim]")
        return grid

    arch_submitted = 0
    arch_components_total = 0
    arch_relationships_total = 0

    from rich.live import Live as ArchLive
    import threading as _arch_threading

    with ArchLive(
        _arch_build_display(),
        console=console,
        refresh_per_second=4,
        transient=False,
    ) as arch_live:
        _arch_stop = _arch_threading.Event()

        def _arch_refresh() -> None:
            while not _arch_stop.is_set():
                arch_live.update(_arch_build_display())
                _arch_stop.wait(0.3)

        arch_refresh_t = _arch_threading.Thread(target=_arch_refresh, daemon=True)
        arch_refresh_t.start()

        try:
            for sa in sub_audits:
                sub_uuid = sa.get("audit_uuid", "")
                repo_name = sa.get("repo_name", "")
                file_paths = [
                    f["file_path"] for f in all_files if f.get("sub_audit_uuid") == sub_uuid
                ]
                if not file_paths:
                    continue
                try:
                    _arch_on_progress(f"Analysing {repo_name} ({len(file_paths)} files)…")
                    phase1, phase2 = run_architecture_analysis(
                        sub_uuid,
                        repo_name or "repo",
                        file_paths,
                        scope_dirs=scope_dirs,
                        on_progress=_arch_on_progress,
                        on_debug=_arch_on_debug,
                    )

                    n_comp = len(phase2.get("architectural_components") or {})
                    n_rel = len(phase2.get("component_relationships") or [])
                    _arch_on_debug(
                        f"Submitting to CodeDD ({n_comp} components, {n_rel} relationships)…"
                    )

                    payload = {
                        "audit_uuid": sub_uuid,
                        "phase1_results": phase1,
                        "phase2_results": phase2,
                    }
                    # Architecture storage is fast now (no server-side LLM calls),
                    # but allow extra time for TypeDB writes on large repos.
                    with CodeDDClient(config=cfg) as client:
                        resp = client.post(
                            Endpoints.AUDIT_ARCHITECTURE, json=payload, timeout=120
                        )
                    if resp.status_code == 200:
                        arch_submitted += 1
                        arch_components_total += n_comp
                        arch_relationships_total += n_rel
                    else:
                        try:
                            msg = resp.json().get("message", "architecture submission failed")
                        except Exception:
                            msg = "architecture submission failed"
                        print_warning(f"Architecture for {repo_name}: {msg}")
                except Exception as e:
                    print_warning(f"Architecture for {repo_name}: {e}")
        finally:
            _arch_stop.set()
            arch_refresh_t.join(timeout=2)
            arch_live.update(_arch_build_display())

    arch_elapsed = time.monotonic() - arch_start
    console.print()
    if arch_submitted:
        console.print(
            f"  [green]{SYMBOL_OK}[/green] Architecture analysed: "
            f"[bold]{arch_components_total}[/bold] components, "
            f"[bold]{arch_relationships_total}[/bold] relationships "
            f"([bold]{arch_submitted}[/bold] repo(s))  "
            f"[dim]({int(arch_elapsed)}s)[/dim]"
        )
    console.print()

    # ---- Step 3: Submit results in batches -------------------------------
    console.print("[bold]Submitting results to CodeDD…[/bold]\n")

    # Group results by sub_audit_uuid
    results_by_sub: dict[str, list[dict]] = {}
    for r in successful_results:
        # Find the sub_audit_uuid from all_files
        sub_uuid = ""
        for f in all_files:
            if f["file_path"] == r.file_path:
                sub_uuid = f.get("sub_audit_uuid", "")
                break
        results_by_sub.setdefault(sub_uuid, []).append({
            "file_path": r.file_path,
            "audit_data": r.audit_data,
        })

    total_ingested = 0
    total_errors = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        console=console,
    ) as progress:
        total_batches = sum(
            (len(items) + batch_size - 1) // batch_size
            for items in results_by_sub.values()
        )
        task = progress.add_task("Submitting…", total=total_batches)

        with CodeDDClient(config=cfg) as client:
            for sub_uuid, items in results_by_sub.items():
                for i in range(0, len(items), batch_size):
                    batch = items[i : i + batch_size]
                    payload = {
                        "audit_uuid": sub_uuid,
                        "results": batch,
                    }

                    if show:
                        confirmed = review_payload(
                            {"audit_uuid": sub_uuid, "results_count": len(batch)},
                            command_label=f"Submit Batch ({i // batch_size + 1})",
                            context_note=(
                                "This payload submits structured audit results "
                                "(field names + values, NO source code) to CodeDD."
                            ),
                            confirm_prompt="Submit this batch?",
                        )
                        if not confirmed:
                            print_info("Batch skipped.")
                            progress.advance(task)
                            continue

                    try:
                        resp = client.post(
                            Endpoints.AUDIT_RESULTS, json=payload, timeout=90
                        )

                        if resp.status_code == 200:
                            body = resp.json()
                            total_ingested += body.get("ingested", 0)
                            total_errors += len(body.get("errors", []))
                        else:
                            msg = "batch submission failed"
                            try:
                                msg = resp.json().get("message", msg)
                            except Exception:
                                pass
                            print_warning(f"Batch error: {msg}")
                            total_errors += len(batch)
                    except Exception as result_exc:
                        print_warning(f"Result batch submission failed: {result_exc}")
                        total_errors += len(batch)

                    progress.advance(task)

    console.print()
    console.print(
        f"  [green]{SYMBOL_OK}[/green] Results submitted: "
        f"[bold]{total_ingested}[/bold] ingested"
        + (f", [yellow]{total_errors} error(s)[/yellow]" if total_errors else "")
    )
    console.print()

    # ---- Step 3a.1: Run local source-based vulnerability validation --------
    validation_by_sub: dict[str, list[dict]] = {}
    file_to_sub_uuid = {f["file_path"]: f.get("sub_audit_uuid", "") for f in all_files}
    file_to_local_path: dict[str, str] = {}
    for f in all_files:
        repo_name = f.get("repo_name", "")
        rel_path = f.get("relative_path", "")
        local_root = scope_dirs.get(repo_name, "")
        if local_root and rel_path:
            file_to_local_path[f["file_path"]] = os.path.join(
                local_root, rel_path.replace("/", os.sep).replace("\\", os.sep)
            )

    validation_candidates: list[ValidationCandidate] = []
    for r in successful_results:
        audit_data = r.audit_data or {}
        flag_color = str(audit_data.get("flag_color", "")).strip().lower()
        reasons = str(audit_data.get("reasons_of_flag", "")).strip()
        if flag_color not in {"red", "orange"}:
            continue
        if not reasons or reasons.upper() in {"N/A", "NONE"}:
            continue

        local_path = file_to_local_path.get(r.file_path, "")
        sub_uuid = file_to_sub_uuid.get(r.file_path, "")
        if not local_path or not sub_uuid:
            continue

        revised_time = audit_data.get("time_to_fix_flag")
        try:
            revised_time = float(revised_time) if revised_time is not None else None
        except (TypeError, ValueError):
            revised_time = None

        validation_candidates.append(
            ValidationCandidate(
                file_path=r.file_path,
                local_path=local_path,
                hypothesis=reasons,
                flag_color=flag_color,
                stored_time_to_fix_hours=revised_time,
            )
        )

    if validation_candidates:
        console.print("[bold]Validating flagged vulnerabilities locally…[/bold]\n")
        with LocalVulnerabilityValidator(
            anthropic_key=anthropic_key,
            openai_key=openai_key,
            provider_preference=preference,
            system_prompt=vulnerability_validation_prompt or None,
            on_debug=None,
        ) as validator:
            validation_outcomes = validator.validate(validation_candidates)

        for out in validation_outcomes:
            sub_uuid = file_to_sub_uuid.get(out.file_path, "")
            if not sub_uuid:
                continue
            validation_by_sub.setdefault(sub_uuid, []).append({
                "file_path": out.file_path,
                "conclusion": out.conclusion,
                "impact": out.impact,
                "confidence": out.confidence,
                "recommended_action": out.recommended_action,
                "revised_time_to_fix_hours": out.revised_time_to_fix_hours,
            })

    if validation_by_sub:
        console.print("[bold]Submitting vulnerability validation outcomes…[/bold]\n")
        validation_applied = 0
        validation_failed = 0
        with CodeDDClient(config=cfg) as client:
            for sub_uuid, validations in validation_by_sub.items():
                payload = {
                    "audit_uuid": sub_uuid,
                    "validations": validations,
                }
                try:
                    resp = client.post(
                        Endpoints.AUDIT_VULNERABILITY_VALIDATION,
                        json=payload,
                        timeout=90,
                    )
                    if resp.status_code == 200:
                        body = resp.json()
                        validation_applied += int(body.get("applied", 0))
                        validation_failed += len(body.get("errors", []))
                    else:
                        validation_failed += len(validations)
                except Exception:
                    validation_failed += len(validations)

        console.print(
            f"  [green]{SYMBOL_OK}[/green] Validation outcomes submitted: "
            f"[bold]{validation_applied}[/bold] applied"
            + (f", [yellow]{validation_failed} failed[/yellow]" if validation_failed else "")
        )
        console.print()

    # ---- Step 3b: Submit complexity results --------------------------------
    if cx_ok:
        console.print("[bold]Submitting complexity metrics to CodeDD…[/bold]\n")

        # Group by sub_audit_uuid
        cx_by_sub: dict[str, list[dict]] = {}
        for r in cx_ok:
            sub_uuid = ""
            for f in all_files:
                if f["file_path"] == r.file_path:
                    sub_uuid = f.get("sub_audit_uuid", "")
                    break
            cx_by_sub.setdefault(sub_uuid, []).append({
                "file_path": r.file_path,
                "metrics": r.metrics,
            })

        # Compute per-sub-audit aggregated summary
        cx_ingested = 0
        cx_errors = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            console=console,
        ) as cx_prog:
            total_cx_batches = sum(
                (len(items) + batch_size - 1) // batch_size
                for items in cx_by_sub.values()
            )
            cx_task = cx_prog.add_task("Submitting…", total=total_cx_batches)

            with CodeDDClient(config=cfg) as client:
                for sub_uuid, items in cx_by_sub.items():
                    # Build aggregated summary for this sub-audit
                    agg_input = {}
                    for item in items:
                        fp = item["file_path"]
                        agg_input[fp] = item["metrics"]
                    summary = aggregate_complexity_results(agg_input)

                    for i in range(0, len(items), batch_size):
                        batch = items[i : i + batch_size]
                        payload: dict = {
                            "audit_uuid": sub_uuid,
                            "results": batch,
                        }
                        # Attach summary only on the last batch
                        if i + batch_size >= len(items):
                            payload["summary"] = summary

                        if show:
                            confirmed = review_payload(
                                {"audit_uuid": sub_uuid, "results_count": len(batch)},
                                command_label=f"Complexity Batch ({i // batch_size + 1})",
                                context_note=(
                                    "This payload submits structured complexity metrics "
                                    "(cyclomatic + Halstead, NO source code) to CodeDD."
                                ),
                                confirm_prompt="Submit this batch?",
                            )
                            if not confirmed:
                                print_info("Batch skipped.")
                                cx_prog.advance(cx_task)
                                continue

                        try:
                            resp = client.post(
                                Endpoints.AUDIT_COMPLEXITY, json=payload, timeout=90
                            )
                            if resp.status_code == 200:
                                body = resp.json()
                                cx_ingested += body.get("ingested", 0)
                                cx_errors += len(body.get("errors", []))
                            else:
                                msg = "complexity batch submission failed"
                                try:
                                    msg = resp.json().get("message", msg)
                                except Exception:
                                    pass
                                print_warning(f"Complexity batch error: {msg}")
                                cx_errors += len(batch)
                        except Exception as cx_exc:
                            print_warning(f"Complexity batch submission failed: {cx_exc}")
                            cx_errors += len(batch)

                        cx_prog.advance(cx_task)

        console.print()
        console.print(
            f"  [green]{SYMBOL_OK}[/green] Complexity metrics submitted: "
            f"[bold]{cx_ingested}[/bold] ingested"
            + (f", [yellow]{cx_errors} error(s)[/yellow]" if cx_errors else "")
        )
        console.print()

    # ---- Step 3c: Submit dependency results --------------------------------
    if manifest_ok or import_results:
        console.print("[bold]Submitting dependency data to CodeDD…[/bold]\n")

        dep_ingested_pkgs = 0
        dep_ingested_imports = 0
        dep_vulns_stored = 0
        dep_errors = 0

        # Group manifest results by sub_audit_uuid (via repo_name → sub_audit)
        sub_audit_by_repo: dict[str, str] = {}
        for sa in sub_audits:
            sub_audit_by_repo[sa.get("repo_name", "")] = sa["audit_uuid"]

        # Build per-sub-audit payloads
        dep_by_sub: dict[str, dict] = {}
        for mr in manifest_ok:
            sub_uuid = sub_audit_by_repo.get(mr.repo_name, "")
            if not sub_uuid:
                continue
            entry = dep_by_sub.setdefault(sub_uuid, {
                "manifest_packages": [],
                "import_packages": [],
                "vulnerabilities": {},
            })
            # Build manifest_path as cli:// path
            manifest_cli_path = f"cli://{sub_uuid}/{mr.repo_name}/{mr.manifest_path}"
            entry["manifest_packages"].append({
                "manifest_path": manifest_cli_path,
                "registry": mr.registry,
                "packages": mr.packages,
            })

        for ir in import_results:
            # Determine sub_audit_uuid from file_path
            sub_uuid = ""
            for f in all_files:
                if f["file_path"] == ir.file_path:
                    sub_uuid = f.get("sub_audit_uuid", "")
                    break
            if not sub_uuid:
                continue
            entry = dep_by_sub.setdefault(sub_uuid, {
                "manifest_packages": [],
                "import_packages": [],
                "vulnerabilities": {},
            })
            entry["import_packages"].append({
                "file_path": ir.file_path,
                "registry_prefix": ir.registry_prefix,
                "packages": ir.packages,
            })

        # Attach vulnerability data to each sub-audit payload
        for sub_uuid, entry in dep_by_sub.items():
            entry["vulnerabilities"] = vuln_results

        dep_endpoint_404_shown = False  # Only show once when server doesn't support the endpoint

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            console=console,
        ) as dep_prog:
            dep_task = dep_prog.add_task("Submitting…", total=len(dep_by_sub))

            with CodeDDClient(config=cfg) as client:
                for sub_uuid, dep_payload in dep_by_sub.items():
                    payload = {
                        "audit_uuid": sub_uuid,
                        **dep_payload,
                    }

                    if show:
                        confirmed = review_payload(
                            {
                                "audit_uuid": sub_uuid,
                                "manifest_count": len(dep_payload["manifest_packages"]),
                                "import_count": len(dep_payload["import_packages"]),
                                "vuln_count": len(dep_payload["vulnerabilities"]),
                            },
                            command_label="Dependency Submission",
                            context_note=(
                                "This payload submits structured dependency data "
                                "(package names + versions, NO source code) to CodeDD."
                            ),
                            confirm_prompt="Submit dependency data?",
                        )
                        if not confirmed:
                            print_info("Dependency submission skipped.")
                            dep_prog.advance(dep_task)
                            continue

                    try:
                        # Dependency submission involves TypeDB writes on the
                        # server; allow extra time for large manifests.
                        resp = client.post(
                            Endpoints.AUDIT_DEPENDENCIES, json=payload, timeout=120
                        )
                        if resp.status_code == 200:
                            body = resp.json()
                            dep_ingested_pkgs += body.get("ingested_packages", 0)
                            dep_ingested_imports += body.get("ingested_imports", 0)
                            dep_vulns_stored += body.get("vulnerabilities_stored", 0)
                            dep_errors += len(body.get("errors", []))
                            # Metadata enrichment (scorecards, vuln scanning)
                            # runs as part of the Celery chain during post-processing
                        elif resp.status_code == 404:
                            if not dep_endpoint_404_shown:
                                print_warning(
                                    "Server does not support dependency submission (404). "
                                    "Upgrade the CodeDD backend to enable dependency tracking."
                                )
                                dep_endpoint_404_shown = True
                        else:
                            msg = "dependency submission failed"
                            try:
                                msg = resp.json().get("message", msg)
                            except Exception:
                                pass
                            print_warning(f"Dependency error: {msg}")
                            dep_errors += 1
                    except Exception as dep_submit_exc:
                        print_warning(
                            f"Dependency submission failed for sub-audit "
                            f"{sub_uuid[:8]}…: {dep_submit_exc}"
                        )
                        dep_errors += 1

                    dep_prog.advance(dep_task)

        console.print()
        console.print(
            f"  [green]{SYMBOL_OK}[/green] Dependencies submitted: "
            f"[bold]{dep_ingested_pkgs}[/bold] packages, "
            f"[bold]{dep_ingested_imports}[/bold] imports, "
            f"[bold]{dep_vulns_stored}[/bold] vulns stored"
            + (f", [yellow]{dep_errors} error(s)[/yellow]" if dep_errors else "")
        )
        if dep_ingested_pkgs > 0:
            console.print(
                f"  [dim]{SYMBOL_INFO}[/dim] "
                "Package metadata enrichment (registry info, scorecards) "
                "will run during post-processing on CodeDD."
            )
        console.print()

    # ---- Step 4: Signal completion ---------------------------------------
    console.print("[bold]Triggering post-processing on CodeDD…[/bold]\n")

    complete_payload = {"audit_uuid": audit_uuid, "trigger_next_steps": True}
    if show:
        confirmed = review_payload(
            complete_payload,
            command_label="Audit Complete",
            context_note=(
                "This tells CodeDD that file-level auditing is done and to "
                "trigger consolidation / recommendations (Steps 7-9)."
            ),
            confirm_prompt="Proceed with audit completion?",
        )
        if not confirmed:
            print_info("Cancelled.")
            raise typer.Exit(code=0)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as prog:
        prog.add_task("Finalising…", total=None)
        with CodeDDClient(config=cfg) as client:
            resp = client.post(Endpoints.AUDIT_COMPLETE, json=complete_payload)

    if resp.status_code != 200:
        msg = "Failed to signal audit completion"
        try:
            msg = resp.json().get("message", msg)
        except Exception:
            pass
        print_error(msg)
        raise typer.Exit(code=1)

    complete_body = resp.json()
    if complete_body.get("status") != "success":
        print_error(complete_body.get("message", "Unknown error during completion"))
        raise typer.Exit(code=1)

    # ---- Final summary ---------------------------------------------------
    console.print()
    minutes, seconds = divmod(int(elapsed), 60)
    time_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

    print_success("[bold]Audit complete![/bold]")
    console.print(f"    Files audited: [bold]{len(successful_results)}[/bold]")
    if cx_ok:
        console.print(f"    Complexity analysed: [bold]{len(cx_ok)}[/bold] files")
    if arch_submitted:
        console.print(
            f"    Architecture: [bold]{arch_components_total}[/bold] components, "
            f"[bold]{arch_relationships_total}[/bold] relationships"
        )
    if manifest_ok or import_results:
        console.print(
            f"    Dependencies: [bold]{total_dep_pkgs}[/bold] packages, "
            f"[bold]{total_import_pkgs}[/bold] imports"
        )
        if vuln_found:
            console.print(f"    Vulnerabilities: [bold yellow]{vuln_found}[/bold yellow] packages affected")
    console.print(f"    Time: [bold]{time_str}[/bold]")

    if provider_stats:
        provider_parts = []
        for p, count in sorted(provider_stats.items()):
            provider_parts.append(f"{p.capitalize()} ({count} files)")
        console.print(f"    Provider: {' | '.join(provider_parts)}")

    if complete_body.get("post_processing_triggered"):
        console.print(
            f"    [dim]Post-processing (dependency enrichment, consolidation "
            f"& recommendations) is running on CodeDD.[/dim]"
        )

    console.print()
    print_info(
        "Track results at [bold cyan]codedd.ai[/bold cyan] "
        "or run [bold cyan]codedd audits list[/bold cyan]."
    )
