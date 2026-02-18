"""
``codedd scope`` sub-commands: add, remove, list, clear, status, confirm, sync.

Manages the local directories that will be included in the active audit.

Workflow:
    1. ``codedd audits select``           – pick a group or single audit
    2. ``codedd scope add /path/to/repo`` – add directories (validates git)
    3. ``codedd scope list``              – review the scope
    4. ``codedd scope remove 2``          – drop a directory by number
    5. ``codedd scope confirm``           – scan, preview, and register with CodeDD
    6. ``codedd scope sync``              – compare local vs. remote, detect changes

For **group audits** the user may add 1-n directories.
For **single audits** exactly 1 directory is allowed.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, IntPrompt

from codedd_cli.api.client import CodeDDClient
from codedd_cli.api.endpoints import Endpoints
from codedd_cli.auth.session import require_auth
from codedd_cli.config.settings import ConfigManager
from codedd_cli.scanner.file_walker import scan_repository
from codedd_cli.utils.directory_validator import validate_directory
from codedd_cli.utils.display import (
    print_audit_scope_cloud_info,
    print_error,
    print_info,
    print_success,
    print_warning,
    prompt_deleted_audits_action,
    render_audits_table,
    render_diff_table,
    render_scope_table,
    render_scope_table_with_sync,
    render_validation_result,
    SYMBOL_FAIL,
    SYMBOL_OK,
)
from codedd_cli.commands.audits_cmd import _fetch_audits
from codedd_cli.utils.payload_inspector import review_payload, review_request

console = Console()
scope_app = typer.Typer(no_args_is_help=True)


def _require_active_audit(cfg: ConfigManager) -> None:
    """Abort if no audit has been selected."""
    if not cfg.active_audit_uuid:
        print_error(
            "No active audit selected.  "
            "Run [bold cyan]codedd audits select[/bold cyan] first."
        )
        raise typer.Exit(code=1)


def _enforce_single_audit_limit(cfg: ConfigManager) -> bool:
    """
    For single audits, check that we haven't already reached the 1-directory limit.

    Returns True if adding another directory is allowed, False otherwise.
    """
    if cfg.active_audit_type == "single" and len(cfg.scope_directories) >= 1:
        print_error(
            "Single audits support exactly [bold]1[/bold] directory.  "
            "Remove the existing one first with [bold cyan]codedd scope remove 1[/bold cyan]."
        )
        return False
    return True


# ---------------------------------------------------------------------------
# codedd scope add <path> [<path> ...]
# ---------------------------------------------------------------------------

@scope_app.command("add")
@require_auth
def add_directory(
    paths: list[str] = typer.Argument(
        ...,
        help="One or more absolute paths to local Git repositories.",
    ),
) -> None:
    """
    Add local Git repository directories to the audit scope.

    Each path is validated to ensure it:
      - exists and is readable
      - is the root of a Git repository
      - has at least one commit

    For single audits, only 1 directory is allowed.
    For group audits, you may add multiple directories.
    """
    cfg = ConfigManager()
    _require_active_audit(cfg)

    added_count = 0

    for raw_path in paths:
        # Single audit limit check
        if not _enforce_single_audit_limit(cfg):
            break

        console.print(f"\n[dim]Validating:[/dim] {raw_path}")

        # Run validation
        dir_info = validate_directory(raw_path)
        render_validation_result(dir_info)

        if not dir_info.is_valid:
            continue

        # Add to scope (ConfigManager handles de-duplication)
        was_added = cfg.add_scope_directory(
            path=dir_info.path,
            repo_name=dir_info.repo_name,
            branch=dir_info.branch,
            commit_hash=dir_info.commit_hash,
        )

        if was_added:
            added_count += 1
        else:
            print_warning(f"  [yellow]Skipped (already in scope):[/yellow] {dir_info.path}")

    console.print()

    if added_count:
        print_success(f"{added_count} director{'y' if added_count == 1 else 'ies'} added to scope")

    # Show current scope
    dirs = cfg.scope_directories
    if dirs:
        console.print()
        render_scope_table(dirs, cfg.active_audit_name)

        # Next-step hints
        console.print()
        console.print("[dim]Next steps:[/dim]")
        console.print("  [dim]1.[/dim] Add more directories  [bold cyan]codedd scope add <path>[/bold cyan]")
        console.print("  [dim]2.[/dim] Confirm selection     [bold cyan]codedd scope confirm[/bold cyan]")


# ---------------------------------------------------------------------------
# codedd scope remove <number>
# ---------------------------------------------------------------------------

@scope_app.command("remove")
@require_auth
def remove_directory(
    number: Optional[int] = typer.Argument(
        None,
        help="Number of the directory to remove (from 'codedd scope list'). Omit for interactive.",
    ),
) -> None:
    """
    Remove a directory from the audit scope by its number.

    Run ``codedd scope list`` to see the numbered list.
    """
    cfg = ConfigManager()
    _require_active_audit(cfg)

    dirs = cfg.scope_directories
    if not dirs:
        print_info("Scope is empty — nothing to remove.")
        return

    # Interactive mode if no number given
    if number is None:
        render_scope_table(dirs, cfg.active_audit_name)
        console.print()
        number = IntPrompt.ask(
            "Enter the number of the directory to remove",
            choices=[str(i) for i in range(1, len(dirs) + 1)],
        )

    # Convert 1-based user input to 0-based index
    index = number - 1

    if index < 0 or index >= len(dirs):
        print_error(f"Invalid number. Must be between 1 and {len(dirs)}.")
        raise typer.Exit(code=1)

    removed_entry = dirs[index]
    cfg.remove_scope_directory(index)

    print_success(f"Removed [bold]{removed_entry.get('repo_name', removed_entry['path'])}[/bold] from scope")

    # Show remaining scope
    remaining = cfg.scope_directories
    if remaining:
        console.print()
        render_scope_table(remaining, cfg.active_audit_name)
    else:
        print_info("Scope is now empty.")


# ---------------------------------------------------------------------------
# codedd scope list
# ---------------------------------------------------------------------------

@scope_app.command("list")
@require_auth
def list_directories() -> None:
    """
    Show all directories currently in the audit scope.
    """
    cfg = ConfigManager()
    _require_active_audit(cfg)

    dirs = cfg.scope_directories
    audit_type = cfg.active_audit_type

    # Header with audit info
    console.print(
        f"\n[bold]Audit:[/bold]  {cfg.active_audit_name}  "
        f"[dim]({audit_type})[/dim]  "
        f"[dim]{cfg.active_audit_uuid[:8]}…[/dim]"
    )

    limit_info = "1 directory" if audit_type == "single" else "1-n directories"
    console.print(f"[bold]Limit:[/bold]  {limit_info}\n")

    render_scope_table(dirs, cfg.active_audit_name)

    if dirs:
        console.print()
        remaining_slots = "unlimited" if audit_type == "group" else str(max(0, 1 - len(dirs)))
        print_info(f"Directories: {len(dirs)}  |  Remaining slots: {remaining_slots}")


# ---------------------------------------------------------------------------
# codedd scope clear
# ---------------------------------------------------------------------------

@scope_app.command("clear")
@require_auth
def clear_directories() -> None:
    """
    Remove all directories from the audit scope.
    """
    cfg = ConfigManager()
    _require_active_audit(cfg)

    dirs = cfg.scope_directories
    if not dirs:
        print_info("Scope is already empty.")
        return

    render_scope_table(dirs, cfg.active_audit_name)
    console.print()

    confirmed = Confirm.ask(
        f"Remove all [bold]{len(dirs)}[/bold] director{'y' if len(dirs) == 1 else 'ies'} from scope?",
        default=False,
    )

    if confirmed:
        cfg.clear_scope_directories()
        print_success("Scope cleared.")
    else:
        print_info("Cancelled.")


# ---------------------------------------------------------------------------
# codedd scope status
# ---------------------------------------------------------------------------

@scope_app.command("status")
@require_auth
def scope_status() -> None:
    """
    Show a summary of the current audit scope readiness.
    """
    cfg = ConfigManager()
    _require_active_audit(cfg)

    dirs = cfg.scope_directories
    audit_type = cfg.active_audit_type
    audit_name = cfg.active_audit_name

    console.print(f"\n[bold]Audit:[/bold]  {audit_name}  [dim]({audit_type})[/dim]")
    console.print(f"[bold]UUID:[/bold]   {cfg.active_audit_uuid}")
    console.print(f"[bold]Directories:[/bold] {len(dirs)}")

    if not dirs:
        console.print()
        print_warning("No directories added. Use [bold cyan]codedd scope add <path>[/bold cyan] to begin.")
        return

    # Re-validate all directories (they could have changed since being added)
    console.print("\n[dim]Re-validating directories…[/dim]\n")
    all_valid = True
    for entry in dirs:
        dir_info = validate_directory(entry["path"])
        render_validation_result(dir_info)
        if not dir_info.is_valid:
            all_valid = False

    console.print()

    if all_valid:
        print_success(
            f"Scope is ready — {len(dirs)} valid director{'y' if len(dirs) == 1 else 'ies'}."
        )
    else:
        print_error(
            "Some directories failed validation.  "
            "Fix or remove them before proceeding."
        )

    # Show sync state
    any_confirmed = any(d.get("confirmed") for d in dirs)
    any_dirty = any(d.get("needs_reconfirm") for d in dirs)

    if any_dirty:
        console.print()
        print_warning(
            "Scope has local changes.  Run [bold cyan]codedd scope sync[/bold cyan] "
            "or [bold cyan]codedd scope confirm[/bold cyan] to update."
        )
    elif any_confirmed:
        console.print()
        print_info("Scope is registered with CodeDD.  Run [bold cyan]codedd scope sync[/bold cyan] to check for changes.")

    console.print()
    render_scope_table_with_sync(dirs, audit_name)


# ---------------------------------------------------------------------------
# codedd scope confirm
# ---------------------------------------------------------------------------

@scope_app.command("confirm")
@require_auth
def confirm_scope(
    show: bool = typer.Option(
        False,
        "--show",
        "-s",
        help="Write the API payload to a file and open it for review before sending.",
    ),
) -> None:
    """
    Scan all directories, build metadata, and register the scope with CodeDD.

    This command:
      1. Re-validates every directory in the scope.
      2. Walks each directory to classify files and count lines of code.
      3. Builds a metadata-only payload (no source code).
      4. With --show: writes the payload to a .txt file for inspection.
      5. Asks for confirmation, then sends the metadata to CodeDD.
    """
    cfg = ConfigManager()
    _require_active_audit(cfg)

    dirs = cfg.scope_directories
    if not dirs:
        print_error("No directories in scope.  Use [bold cyan]codedd scope add <path>[/bold cyan] first.")
        raise typer.Exit(code=1)

    audit_uuid = cfg.active_audit_uuid
    audit_type = cfg.active_audit_type
    audit_name = cfg.active_audit_name

    console.print(f"\n[bold]Audit:[/bold]  {audit_name}  [dim]({audit_type})[/dim]")
    console.print(f"[bold]Directories:[/bold] {len(dirs)}\n")

    # Phase 1: Re-validate all directories
    console.print("[dim]Phase 1 — Validating directories…[/dim]\n")
    all_valid = True
    for entry in dirs:
        dir_info = validate_directory(entry["path"])
        render_validation_result(dir_info)
        if not dir_info.is_valid:
            all_valid = False

    if not all_valid:
        console.print()
        print_error("Some directories failed validation. Fix or remove them before confirming.")
        raise typer.Exit(code=1)

    console.print()

    # Phase 2: Scan each directory
    console.print("[dim]Phase 2 — Scanning repositories…[/dim]\n")
    scan_results = []

    for entry in dirs:
        repo_path = entry["path"]
        repo_name = entry.get("repo_name", Path(repo_path).name)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task(f"Scanning [bold]{repo_name}[/bold]…", total=None)

            def _progress_cb(current: int, total: int, file_path: str) -> None:
                progress.update(task, description=f"Scanning [bold]{repo_name}[/bold] ({current}/{total})")

            result = scan_repository(repo_path, progress_callback=_progress_cb)
            scan_results.append(result)

        # Display summary for this repo
        console.print(
            f"  [bold green]{SYMBOL_OK}[/bold green] [bold]{result.repo_name}[/bold]  "
            f"[dim]{result.branch} @ {result.commit_hash}[/dim]  "
            f"files: {result.total_files}  "
            f"LoC: {result.total_lines_of_code:,}  "
            f"docs: {result.total_lines_of_doc:,}"
        )
        if result.errors:
            print_warning(f"    {len(result.errors)} file(s) had scan errors (skipped)")

    console.print()

    # Phase 3: Build the API payload
    payload = _build_payload(audit_uuid, audit_type, scan_results)

    # Phase 4: Summary + confirmation (--show writes & opens the payload file)
    total_files = sum(r.total_files for r in scan_results)
    total_loc = sum(r.total_lines_of_code for r in scan_results)

    console.print(
        f"[bold]Summary:[/bold]  {len(scan_results)} repositor{'y' if len(scan_results) == 1 else 'ies'}  |  "
        f"{total_files:,} files  |  {total_loc:,} lines of code\n"
    )
    print_info(
        "Only [bold]file paths, types, and line counts[/bold] will be sent to CodeDD.  "
        "[bold]No source code.[/bold]"
    )
    console.print()

    if show:
        # --show: write payload to file, open it, ask for confirmation
        confirmed = review_payload(
            payload,
            command_label="Scope Registration",
            context_note="This file contains ONLY metadata (file paths, types, line counts). No source code content is included.",
            confirm_prompt="Register this scope with CodeDD?",
        )
    else:
        confirmed = Confirm.ask("Register this scope with CodeDD?", default=True)

    if not confirmed:
        print_info("Cancelled. Scope was not sent.")
        return

    # Phase 6: Send to CodeDD
    console.print()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        send_task = progress.add_task("Registering scope with CodeDD…", total=None)

        with CodeDDClient(config=cfg) as client:
            resp = client.post(Endpoints.REGISTER_SCOPE, json=payload)

    if resp.status_code == 401:
        print_error("Authentication failed.  Run [bold cyan]codedd auth login[/bold cyan].")
        raise typer.Exit(code=1)

    if resp.status_code not in (200, 201):
        msg = "Failed to register scope"
        try:
            body = resp.json()
            msg = body.get("message", msg)
        except Exception:
            pass
        print_error(msg)
        raise typer.Exit(code=1)

    body = resp.json()
    if body.get("status") != "success":
        print_error(body.get("message", "Unknown error from server"))
        raise typer.Exit(code=1)

    # Mark all scope entries as confirmed
    cfg.mark_scope_confirmed()

    # Display results
    console.print()
    print_success(f"Scope registered — {body.get('message', '')}")

    sub_audits = body.get("sub_audits", [])
    for sa in sub_audits:
        status_icon = f"[bold green]{SYMBOL_OK}[/bold green]" if sa.get("status") == "ok" else f"[bold red]{SYMBOL_FAIL}[/bold red]"
        console.print(
            f"  {status_icon} [bold]{sa.get('repo_name', '—')}[/bold]  "
            f"audit: [dim]{sa.get('audit_uuid', '—')[:12]}…[/dim]  "
            f"files: {sa.get('files_registered', 0)}  "
            f"LoC: {sa.get('total_lines_of_code', 0):,}"
        )

    # Point user to the cloud app to manage scope, pay, etc. (after sub-audit list)
    print_audit_scope_cloud_info(cfg.active_audit_uuid, cfg.active_audit_type)


# ---------------------------------------------------------------------------
# codedd scope sync
# ---------------------------------------------------------------------------

@scope_app.command("sync")
@require_auth
def sync_scope(
    show: bool = typer.Option(
        False,
        "--show",
        "-s",
        help="Write each API request to a file and open it for review before sending.",
    ),
) -> None:
    """
    Compare the local directory state with what is registered in CodeDD.

    This command:
      1. Fetches the registered file list from CodeDD for each CLI sub-audit.
      2. Re-scans each local directory to build the current file list.
      3. Computes a diff (added / removed / changed files) per repository.
      4. If changes are detected, marks the scope as *needs_reconfirm*.
      5. Optionally re-confirms the scope (re-runs the register flow).
    """
    cfg = ConfigManager()
    _require_active_audit(cfg)

    dirs = cfg.scope_directories
    if not dirs:
        print_error("No directories in scope.  Use [bold cyan]codedd scope add <path>[/bold cyan] first.")
        raise typer.Exit(code=1)

    audit_uuid = cfg.active_audit_uuid
    audit_name = cfg.active_audit_name

    console.print(f"\n[bold]Audit:[/bold]  {audit_name}  [dim]({cfg.active_audit_type})[/dim]")
    console.print(f"[bold]Directories:[/bold] {len(dirs)}\n")

    # Phase 1: Fetch remote state from CodeDD
    console.print("[dim]Phase 1 — Fetching registered scope from CodeDD…[/dim]\n")

    if show:
        confirmed = review_request(
            "GET",
            Endpoints.SCOPE_FILES,
            params={"audit_uuid": audit_uuid},
            command_label="Scope Sync — Fetch Files",
            context_note="This request fetches the list of files currently registered for your audit. Only the audit UUID is sent.",
            confirm_prompt="Proceed with this request to CodeDD?",
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
        progress.add_task("Contacting CodeDD…", total=None)

        with CodeDDClient(config=cfg) as client:
            resp = client.get(Endpoints.SCOPE_FILES, params={"audit_uuid": audit_uuid})

    if resp.status_code == 401:
        print_error("Authentication failed.  Run [bold cyan]codedd auth login[/bold cyan].")
        raise typer.Exit(code=1)

    # Parse body for message (even on error) to detect "audit not found"
    try:
        body = resp.json() if resp.content else {}
    except Exception:
        body = {}
    msg = body.get("message", "") or ""

    # Audit itself (group/single) no longer exists on CodeDD — show message and list available audits
    def _is_audit_not_found_response() -> bool:
        if resp.status_code == 404:
            return True
        low = msg.lower()
        return "audit not found" in low or "access denied or audit not found" in low

    if resp.status_code != 200 or body.get("status") != "success":
        if _is_audit_not_found_response():
            print_warning("The audit no longer exists on CodeDD (deleted or not found).")
            console.print()
            _show_available_audits_and_exit(cfg, show=show)
        else:
            print_error(msg or "Failed to fetch scope data from CodeDD")
            raise typer.Exit(code=1)

    remote_sub_audits = body.get("sub_audits", [])

    # Build a lookup: repo_name -> remote file list
    remote_by_name: dict[str, list[dict]] = {}
    for sa in remote_sub_audits:
        remote_by_name[sa.get("repo_name", "")] = sa.get("files", [])

    # Detect repos missing from remote (sub-audit not in API response)
    local_names = {d.get("repo_name") for d in dirs}
    remote_names = set(remote_by_name.keys())
    deleted_remotely = local_names - remote_names

    if deleted_remotely:
        for name in deleted_remotely:
            print_warning(f"  Repository [bold]{name}[/bold] was deleted from CodeDD (no longer in scope)")
        console.print()

    # When audit exists but no sub_audits on remote (scope empty), skip Phase 2 and show panel
    scope_empty = len(remote_sub_audits) == 0

    total_has_diff = False
    repo_diffs: list[tuple[str, dict, int]] = []  # (repo_name, diff, remote_file_count)
    total_loc_sync = 0  # Sum of local LoC across scanned repos
    total_for_audit_files = 0  # Sum of files selected for audit on CodeDD (remote)
    total_for_audit_loc = 0  # Sum of LoC selected for audit on CodeDD (remote)

    if scope_empty:
        repos_deleted_on_codedd = set(local_names)
        console.print()
    else:
        # Phase 2: Re-scan local directories; separate "deleted on remote" from normal diffs
        console.print("[dim]Phase 2 — Scanning local directories…[/dim]\n")

        repos_cleared_on_remote: set[str] = set()  # in remote_by_name but 0 files (audit cleared)

        for entry in dirs:
            repo_path = entry["path"]
            repo_name = entry.get("repo_name", Path(repo_path).name)

            # Skip if not known on the server (it was never confirmed)
            if repo_name not in remote_by_name:
                print_info(f"  [bold]{repo_name}[/bold] — not yet registered; skipping diff")
                continue

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
                transient=True,
            ) as progress:
                progress.add_task(f"Scanning [bold]{repo_name}[/bold]…", total=None)
                scan_result = scan_repository(repo_path)

            # Build local file map: relative_path -> { file_type, lines_of_code, lines_of_doc }
            local_files: dict[str, dict] = {}
            for fm in scan_result.files:
                local_files[fm.relative_path] = {
                    "file_type": fm.file_type,
                    "lines_of_code": fm.lines_of_code,
                    "lines_of_doc": fm.lines_of_doc,
                }

            # Build remote file map
            remote_files: dict[str, dict] = {}
            for rf in remote_by_name[repo_name]:
                remote_files[rf["relative_path"]] = rf

            remote_count = len(remote_files)

            local_loc = scan_result.total_lines_of_code
            remote_list = remote_by_name[repo_name]
            # Selected for audit on CodeDD (what the web UI shows)
            for_audit_files = sum(1 for rf in remote_list if rf.get("selected_for_audit", True))
            for_audit_loc = sum(
                rf.get("lines_of_code", 0)
                for rf in remote_list
                if rf.get("selected_for_audit", True)
            )

            # Audit deleted/cleared on CodeDD (0 files on remote) — do not treat as normal diff
            if remote_count == 0:
                repos_cleared_on_remote.add(repo_name)
                status_label = "[bold yellow]deleted on CodeDD[/bold yellow]"
                console.print(
                    f"  {status_label}  [bold]{repo_name}[/bold]  "
                    f"{len(local_files)} files, {local_loc:,} LoC  [dim](none on CodeDD)[/dim]"
                )
                total_loc_sync += local_loc
                continue

            # Normal diff
            diff = _compute_diff(local_files, remote_files)
            has_diff = bool(diff["added"] or diff["removed"] or diff["changed"])

            if has_diff:
                total_has_diff = True
                repo_diffs.append((repo_name, diff, remote_count))

            status_label = "[bold yellow]changes detected[/bold yellow]" if has_diff else "[bold green]in sync[/bold green]"
            # One clear line: local scope → what's for audit on CodeDD (matches web UI)
            console.print(
                f"  {status_label}  [bold]{repo_name}[/bold]  "
                f"{len(local_files)} files, {local_loc:,} LoC  →  "
                f"[cyan]{for_audit_files} file{'s' if for_audit_files != 1 else ''}, {for_audit_loc:,} LoC for audit[/cyan]"
            )
            total_loc_sync += local_loc
            total_for_audit_files += for_audit_files
            total_for_audit_loc += for_audit_loc

        # All repos that are "deleted" on CodeDD: missing from API or 0 files
        repos_deleted_on_codedd = deleted_remotely | repos_cleared_on_remote

        if total_loc_sync > 0:
            total_line = f"\n  [dim]Total: {total_loc_sync:,} LoC  →  {total_for_audit_files} file{'s' if total_for_audit_files != 1 else ''}, {total_for_audit_loc:,} LoC for audit on CodeDD[/dim]"
            console.print(total_line)
        console.print()

    # Handle deleted-on-CodeDD audits: prompt and act (Option 1 or 2)
    if repos_deleted_on_codedd:
        choice = prompt_deleted_audits_action(sorted(repos_deleted_on_codedd))

        # 1 or 3 = accept deletion (remove from local scope); 2 or 4 = restore to CodeDD
        if choice in (1, 3):
            # Confirm remote audit deletion: remove these repos from local scope
            dirs_list = cfg.scope_directories
            indices_to_remove = [i for i, d in enumerate(dirs_list) if d.get("repo_name") in repos_deleted_on_codedd]
            for i in sorted(indices_to_remove, reverse=True):
                cfg.remove_scope_directory(i)
            print_success(
                f"Removed {len(indices_to_remove)} audit(s) from local scope to match CodeDD."
            )
            console.print()
            # If scope is now empty, show state and stop
            if not cfg.scope_directories:
                render_scope_table_with_sync(cfg.scope_directories, cfg.active_audit_name)
                return
            # Continue to Phase 3/4 for any remaining repos with normal diffs
        else:
            # Restore local audit scope and sync to CodeDD (re-register)
            console.print()
            _run_reconfirm(cfg, show=show)
            # Show final state and exit; no need to run Phase 3/4
            console.print()
            render_scope_table_with_sync(cfg.scope_directories, cfg.active_audit_name)
            return

    # Phase 3: Display diffs (skips full table when remote has 0 files; limits rows otherwise)
    if repo_diffs:
        console.print("[dim]Phase 3 — Diff details[/dim]\n")
        for repo_name, diff, remote_count in repo_diffs:
            render_diff_table(repo_name, diff, remote_file_count=remote_count)
            console.print()

    # Phase 4: Update config and prompt
    now_iso = datetime.now(timezone.utc).isoformat()

    if total_has_diff:
        cfg.mark_scope_needs_reconfirm(last_sync_iso=now_iso)
        print_warning("Local files have changed since last registration.")
        console.print()

        confirmed = Confirm.ask("Re-confirm scope now?", default=False)
        if confirmed:
            # Delegate to the confirm flow
            console.print()
            _run_reconfirm(cfg, show=show)
        else:
            print_info(
                "Scope marked as [bold yellow]needs re-confirm[/bold yellow].  "
                "Run [bold cyan]codedd scope confirm[/bold cyan] when ready."
            )
    else:
        cfg.update_scope_last_sync(now_iso)
        print_success("All registered repositories are in sync with local directories.")

    # Show final state
    console.print()
    render_scope_table_with_sync(cfg.scope_directories, cfg.active_audit_name)


# ---------------------------------------------------------------------------
# Helpers for confirm and sync
# ---------------------------------------------------------------------------

def _show_available_audits_and_exit(cfg: ConfigManager, show: bool = False) -> None:
    """
    Fetch and display the list of audits available on CodeDD, then exit.
    Used when the active audit no longer exists (deleted on server).
    """
    if show:
        confirmed = review_request(
            "GET",
            Endpoints.LIST_AUDITS,
            params={"offset": 0, "limit": 50},
            command_label="Audits List (After Scope Not Found)",
            context_note="This request fetches your list of audits from CodeDD. Only pagination params are sent.",
            confirm_prompt="Proceed with this request to CodeDD?",
        )
        if not confirmed:
            print_info("Cancelled.")
            raise typer.Exit(code=0)

    console.print("[dim]Fetching your audits from CodeDD…[/dim]\n")
    try:
        with CodeDDClient(config=cfg) as client:
            single_audits, group_audits, total = _fetch_audits(client, limit=50)
    except typer.Exit:
        raise
    except Exception as e:
        print_error(str(e))
        raise typer.Exit(code=1)

    if not single_audits and not group_audits:
        print_info("No other audits found. Create an audit at [bold cyan]https://www.codedd.ai[/bold cyan]")
    else:
        active_uuid = (cfg.active_audit_uuid or "").strip() or None
        render_audits_table(single_audits, group_audits, total, active_audit_uuid=active_uuid)
        console.print()
        print_info("Run [bold cyan]codedd audits select[/bold cyan] to choose another audit.")

    raise typer.Exit(code=0)


def _build_payload(audit_uuid: str, audit_type: str, scan_results: list) -> dict:
    """Build the JSON payload for the scope registration endpoint."""
    return {
        "audit_uuid": audit_uuid,
        "audit_type": audit_type,
        "repositories": [r.to_dict() for r in scan_results],
    }


def _compute_diff(
    local_files: dict[str, dict],
    remote_files: dict[str, dict],
) -> dict:
    """
    Compare local scan results against the remote (CodeDD) file list.

    Args:
        local_files:  ``{relative_path: {file_type, lines_of_code, lines_of_doc}}``
        remote_files: ``{relative_path: {file_type, lines_of_code, lines_of_doc, selected_for_audit}}``

    Returns:
        Dict with keys ``added``, ``removed``, ``changed``.
    """
    local_paths = set(local_files.keys())
    remote_paths = set(remote_files.keys())

    added = [
        {"path": p, "lines_of_code": local_files[p].get("lines_of_code", 0)}
        for p in sorted(local_paths - remote_paths)
    ]

    removed = [
        {"path": p, "lines_of_code": remote_files[p].get("lines_of_code", 0)}
        for p in sorted(remote_paths - local_paths)
    ]

    changed = []
    for p in sorted(local_paths & remote_paths):
        old_loc = remote_files[p].get("lines_of_code", 0)
        new_loc = local_files[p].get("lines_of_code", 0)
        old_doc = remote_files[p].get("lines_of_doc", 0)
        new_doc = local_files[p].get("lines_of_doc", 0)
        if old_loc != new_loc or old_doc != new_doc:
            changed.append({"path": p, "old_loc": old_loc, "new_loc": new_loc})

    return {"added": added, "removed": removed, "changed": changed}


def _run_reconfirm(cfg: ConfigManager, show: bool = False) -> bool:
    """
    Execute the scope re-confirmation flow: scan, build payload, send to
    CodeDD.  Reuses the same logic as ``confirm_scope`` but is called
    programmatically after a ``sync`` detects changes.

    When ``show`` is True, writes the registration payload to a file and
    asks for confirmation before sending (same as ``scope confirm --show``).

    Returns True if re-registration succeeded, False otherwise.
    """
    dirs = cfg.scope_directories
    audit_uuid = cfg.active_audit_uuid
    audit_type = cfg.active_audit_type

    # Scan
    scan_results = []
    for entry in dirs:
        repo_path = entry["path"]
        repo_name = entry.get("repo_name", Path(repo_path).name)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task(f"Scanning [bold]{repo_name}[/bold]…", total=None)
            result = scan_repository(repo_path)
            scan_results.append(result)

        console.print(
            f"  [bold green]{SYMBOL_OK}[/bold green] [bold]{result.repo_name}[/bold]  "
            f"files: {result.total_files}  "
            f"LoC: {result.total_lines_of_code:,}"
        )

    console.print()
    payload = _build_payload(audit_uuid, audit_type, scan_results)

    if show:
        confirmed = review_payload(
            payload,
            command_label="Scope Re-registration",
            context_note="This file contains ONLY metadata (file paths, types, line counts). No source code content is included.",
            confirm_prompt="Re-register this scope with CodeDD?",
        )
        if not confirmed:
            print_info("Cancelled. Scope was not re-registered.")
            return False

    # Send
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Re-registering scope with CodeDD…", total=None)
        with CodeDDClient(config=cfg) as client:
            resp = client.post(Endpoints.REGISTER_SCOPE, json=payload)

    if resp.status_code not in (200, 201):
        msg = "Failed to re-register scope"
        try:
            body = resp.json()
            msg = body.get("message", msg)
            # Surface per-repo errors so the user can debug
            sub_audits = body.get("sub_audits", [])
            for sa in sub_audits:
                if sa.get("status") != "ok" and sa.get("error"):
                    print_error(
                        f"  {sa.get('repo_name', '?')}: {sa['error']}"
                    )
        except Exception:
            pass
        print_error(msg)
        return False

    body = resp.json()
    if body.get("status") != "success":
        print_error(body.get("message", "Unknown error from server"))
        return False

    cfg.mark_scope_confirmed()
    print_success(f"Scope re-registered — {body.get('message', '')}")

    # Point user to the cloud app to manage scope, pay, etc.
    print_audit_scope_cloud_info(cfg.active_audit_uuid, cfg.active_audit_type)
    return True
