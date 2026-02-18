"""
Rich console output helpers for consistent terminal formatting.

On Windows, Unicode symbols (e.g. checkmark) often render as "?" in PowerShell,
so we use ASCII fallbacks there for reliable display.
"""

import sys
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt
from rich.table import Table

from codedd_cli import __version__
from codedd_cli.models.audit import Audit, GroupAudit
from codedd_cli.models.local_directory import LocalDirectory

# Use ASCII symbols on Windows so PowerShell/legacy consoles don't show "?"
if sys.platform == "win32":
    SYMBOL_OK = "[+]"
    SYMBOL_FAIL = "[x]"
    SYMBOL_WARN = "[!]"
    SYMBOL_INFO = "[i]"
    SYMBOL_DIR = ">"
else:
    SYMBOL_OK = "\u2713"   # ✓
    SYMBOL_FAIL = "\u2717"  # ✗
    SYMBOL_WARN = "!"
    SYMBOL_INFO = "\u2139"  # ℹ
    SYMBOL_DIR = "\u25b8"   # ▸ (right-pointing triangle, folder-like)

console = Console()

# Brand teal colour used in the CodeDD logo (for Rich inline styles).
BRAND_TEAL = "#2BBBC0"

# Style for audit progress debug logs (light grey so they don't compete with main progress).
STYLE_DEBUG_LOG = "grey74"


def print_banner() -> None:
    """
    Print the CodeDD CLI banner with an ASCII logo and welcome message.

    The logo mirrors the CodeDD brand mark: a teal C-bracket with three
    coloured dots (red, amber, green) inside the opening, followed by the
    product name in spaced letters.

    Intended for top-level ``codedd --help`` output only.
    """
    t = BRAND_TEAL
    logo = (
        f"  [bold {t}]  ██████╗[/]\n"
        f"  [bold {t}] ██╔════╝[/]\n" 
        f"  [bold {t}] ██║[/] [bold #E84D4D]●[/]  [bold #E8A838]●[/]  [bold #4DB84D]●[/]" + "    [bold white]C o d e D D[/]" + f"  |  [dim]v{__version__}[/dim]  [bold]Welcome to CodeDD CLI![/bold] \n"
        f"  [bold {t}] ██╚════╗[/]\n"
        f"  [bold {t}]  ██████╝[/]"
    )
    console.print()
    console.print(logo, highlight=False)
    console.print()

def print_success(message: str) -> None:
    console.print(f"[bold green]{SYMBOL_OK}[/bold green] {message}")


def print_error(message: str) -> None:
    console.print(f"[bold red]{SYMBOL_FAIL}[/bold red] {message}")


def print_warning(message: str) -> None:
    console.print(f"[bold yellow]{SYMBOL_WARN}[/bold yellow] {message}")


def print_info(message: str) -> None:
    console.print(f"[dim]{SYMBOL_INFO}[/dim] {message}")


def _format_date(iso_str: Optional[str]) -> str:
    """Convert an ISO datetime string to a human-friendly short date."""
    if not iso_str:
        return "-" if sys.platform == "win32" else "\u2014"  # em dash
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_str[:16] if len(iso_str) >= 16 else iso_str


def _status_color(status: str) -> str:
    """Map audit status strings to Rich color markup."""
    lower = status.lower() if status else ""
    if "completed" in lower or "complete" in lower:
        return f"[green]{status}[/green]"
    if "progress" in lower or "running" in lower or "processing" in lower:
        return f"[yellow]{status}[/yellow]"
    if "error" in lower or "failed" in lower:
        return f"[red]{status}[/red]"
    if "cancelled" in lower or "canceled" in lower:
        return f"[dim]{status}[/dim]"
    return status


def _format_number(n: int) -> str:
    """Format an integer with thousand separators."""
    return f"{n:,}"


def _type_cell(audit_type: str) -> str:
    """
    Format audit type for the table. On Windows (e.g. PowerShell), Rich
    markup in table cells can render poorly, so use plain text there.
    """
    if sys.platform == "win32":
        return audit_type  # "group" or "single", no markup
    if audit_type == "group":
        return "[magenta]group[/magenta]"
    return "[cyan]single[/cyan]"


def render_audits_table(
    single_audits: list[Audit],
    group_audits: list[GroupAudit],
    total: int,
    active_audit_uuid: Optional[str] = None,
) -> None:
    """
    Render a Rich table combining single and group audits.

    Each row shows: index, type, name, status, repos/files, LoC, date.
    If active_audit_uuid is set, the matching row is marked as the current selection.
    """
    table = Table(
        title=f"Your Audits ({total} total)",
        show_lines=False,
        padding=(0, 1),
        expand=True,
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Type", width=16)  # e.g. "group -> active"
    table.add_column("Name", min_width=20, max_width=40, no_wrap=True)
    table.add_column("Status", min_width=12)
    table.add_column("Files", justify="right", width=8)
    table.add_column("LoC", justify="right", width=10)
    table.add_column("Date", width=18)
    table.add_column("UUID", style="dim", width=12)

    idx = 1

    def _type_cell_with_active(audit_type: str, uuid: str) -> str:
        """Type cell; show 'type -> active' when this audit is the active one."""
        base = _type_cell(audit_type)
        if active_audit_uuid and uuid == active_audit_uuid:
            suffix = " -> active"
            if sys.platform == "win32":
                return audit_type + suffix
            return base + "[bold green]" + suffix + "[/bold green]"
        return base

    # Group audits first
    for ga in group_audits:
        table.add_row(
            str(idx),
            _type_cell_with_active("group", ga.audit_uuid),
            ga.audit_name or "—",
            _status_color(ga.audit_status),
            _format_number(ga.number_files),
            _format_number(ga.lines_of_code),
            _format_date(ga.ai_synthesis),
            ga.audit_uuid[:8] + "…",
        )
        idx += 1

    # Single audits
    for a in single_audits:
        table.add_row(
            str(idx),
            _type_cell_with_active("single", a.audit_uuid),
            a.audit_name or "—",
            _status_color(a.audit_status),
            _format_number(a.number_files),
            _format_number(a.lines_of_code),
            _format_date(a.ai_synthesis),
            a.audit_uuid[:8] + "…",
        )
        idx += 1

    console.print(table)


def render_audit_detail(audit) -> None:
    """Render a detailed panel for a single audit or group audit."""
    if isinstance(audit, GroupAudit):
        content = (
            f"[bold]Name:[/bold]       {audit.audit_name}\n"
            f"[bold]Type:[/bold]       Group Audit\n"
            f"[bold]Status:[/bold]     {_status_color(audit.audit_status)}\n"
            f"[bold]Sub-audits:[/bold] {audit.number_of_sub_audits}\n"
            f"[bold]Files:[/bold]      {_format_number(audit.number_files)}\n"
            f"[bold]LoC:[/bold]        {_format_number(audit.lines_of_code)}\n"
            f"[bold]Date:[/bold]       {_format_date(audit.ai_synthesis)}\n"
            f"[bold]UUID:[/bold]       {audit.audit_uuid}"
        )
    elif isinstance(audit, Audit):
        content = (
            f"[bold]Name:[/bold]   {audit.audit_name}\n"
            f"[bold]Type:[/bold]   Single Audit\n"
            f"[bold]Status:[/bold] {_status_color(audit.audit_status)}\n"
            f"[bold]Repo:[/bold]   {audit.repo_url or '—'}\n"
            f"[bold]Files:[/bold]  {_format_number(audit.number_files)}\n"
            f"[bold]LoC:[/bold]    {_format_number(audit.lines_of_code)}\n"
            f"[bold]Date:[/bold]   {_format_date(audit.ai_synthesis)}\n"
            f"[bold]UUID:[/bold]   {audit.audit_uuid}"
        )
    else:
        content = str(audit)

    console.print(Panel(content, title="Audit Details", expand=False))


def render_scope_table(directories: list[dict[str, str]], audit_name: str = "") -> None:
    """
    Render a Rich table showing the directories currently in the audit scope.

    Args:
        directories: List of dicts from ``ConfigManager.scope_directories``.
        audit_name: Active audit name for the table title.
    """
    title = f"Audit Scope — {audit_name}" if audit_name else "Audit Scope"
    table = Table(title=title, show_lines=False, padding=(0, 1), expand=True)
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Repository", min_width=16, max_width=30, no_wrap=True)
    table.add_column("Branch", width=18)
    table.add_column("Commit", width=10)
    table.add_column("Path", min_width=30)

    for idx, entry in enumerate(directories, start=1):
        table.add_row(
            str(idx),
            f"[bold]{entry.get('repo_name', '—')}[/bold]",
            entry.get("branch", "—"),
            f"[dim]{entry.get('commit_hash', '—')}[/dim]",
            entry.get("path", "—"),
        )

    console.print(table)

    if not directories:
        console.print("[dim]  No directories added yet.  Use [bold cyan]codedd scope add <path>[/bold cyan] to add one.[/dim]")


def render_scope_table_with_sync(directories: list[dict[str, str]], audit_name: str = "") -> None:
    """
    Render the scope table with additional columns showing sync state
    (confirmed, needs_reconfirm, last_sync).
    """
    title = f"Audit Scope — {audit_name}" if audit_name else "Audit Scope"
    table = Table(title=title, show_lines=False, padding=(0, 1), expand=True)
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Repository", min_width=16, max_width=25, no_wrap=True)
    table.add_column("Branch", width=14)
    table.add_column("Confirmed", width=10)
    table.add_column("Sync", width=16)
    table.add_column("Path", min_width=25)

    for idx, entry in enumerate(directories, start=1):
        confirmed = entry.get("confirmed", False)
        needs_reconfirm = entry.get("needs_reconfirm", False)
        last_sync = entry.get("last_sync", "")

        if needs_reconfirm:
            status_str = "[bold yellow]dirty[/bold yellow]"
        elif confirmed:
            status_str = "[bold green]yes[/bold green]"
        else:
            status_str = "[dim]no[/dim]"

        sync_str = last_sync[:16] if last_sync else "[dim]never[/dim]"

        table.add_row(
            str(idx),
            f"[bold]{entry.get('repo_name', '-')}[/bold]",
            entry.get("branch", "-"),
            status_str,
            sync_str,
            entry.get("path", "-"),
        )

    console.print(table)

    if not directories:
        console.print("[dim]  No directories added yet.  Use [bold cyan]codedd scope add <path>[/bold cyan] to add one.[/dim]")


# Maximum number of file rows to show in the diff table; rest are summarized.
DIFF_TABLE_MAX_ROWS = 10


def render_diff_table(
    repo_name: str,
    diff: dict,
    remote_file_count: Optional[int] = None,
    max_rows: int = DIFF_TABLE_MAX_ROWS,
) -> None:
    """
    Render a Rich table showing file-level differences between the
    local scan and the remote (CodeDD) state.

    When remote has 0 files (audit removed in CodeDD), only a short message
    is shown and no table is rendered. Otherwise a one-line summary, up to
    max_rows table rows, and a "+ N more files" footer are shown.

    Args:
        repo_name: Human-readable repository name.
        diff: Dict with keys ``added``, ``removed``, ``changed``.
            - added:   list of ``{"path", "lines_of_code"}``
            - removed: list of ``{"path", "lines_of_code"}``
            - changed: list of ``{"path", "old_loc", "new_loc"}``
        remote_file_count: Number of files on remote. If 0, show "audit removed" message only.
        max_rows: Maximum table rows to show; remaining count shown as "+ N more files".
    """
    added = diff.get("added", [])
    removed = diff.get("removed", [])
    changed = diff.get("changed", [])

    if not added and not removed and not changed:
        return

    # Remote has no files: audit was removed or no files selected in CodeDD.
    if remote_file_count is not None and remote_file_count == 0:
        console.print(
            f"  [dim]{SYMBOL_INFO}[/dim] [bold]{repo_name}[/bold]:  "
            "Audit has been removed from CodeDD or no files are selected for audit."
        )
        return

    dash = "-" if sys.platform == "win32" else "\u2014"

    # Build unified row list: (status_label, path, old_loc, new_loc)
    rows: list[tuple[str, str, str, str]] = []
    for f in added:
        rows.append(("[green]+ Added[/green]", f["path"], dash, str(f.get("lines_of_code", 0))))
    for f in removed:
        rows.append(("[red]- Removed[/red]", f["path"], str(f.get("lines_of_code", 0)), dash))
    for f in changed:
        rows.append(
            (
                "[yellow]~ Changed[/yellow]",
                f["path"],
                str(f.get("old_loc", 0)),
                str(f.get("new_loc", 0)),
            )
        )

    total = len(rows)
    n_added, n_removed, n_changed = len(added), len(removed), len(changed)
    loc_old = sum(f.get("lines_of_code", 0) for f in removed) + sum(f.get("old_loc", 0) for f in changed)
    loc_new = sum(f.get("lines_of_code", 0) for f in added) + sum(f.get("new_loc", 0) for f in changed)
    loc_delta_added = max(0, loc_new - loc_old)
    loc_delta_removed = max(0, loc_old - loc_new)

    # Summary line: file counts and total LoC added/removed
    summary_parts = [f"{n_added} added", f"{n_removed} removed", f"{n_changed} changed"]
    summary_parts.append(f"+{loc_delta_added:,} LoC added, -{loc_delta_removed:,} LoC removed")
    console.print(f"  [bold]{repo_name}[/bold]:  {', '.join(summary_parts)}")

    # Table: at most max_rows
    shown = rows[:max_rows]
    table = Table(
        title=f"Diff: {repo_name}",
        show_lines=False,
        padding=(0, 1),
        expand=True,
    )
    table.add_column("Status", width=10)
    table.add_column("File", min_width=30)
    table.add_column("Old LoC", justify="right", width=10)
    table.add_column("New LoC", justify="right", width=10)

    for status_label, path, old_loc, new_loc in shown:
        table.add_row(status_label, path, old_loc, new_loc)

    console.print(table)

    if total > max_rows:
        console.print(f"  [dim]+ {total - max_rows} more file{'s' if total - max_rows != 1 else ''} have changed[/dim]")


def prompt_deleted_audits_action(repo_names: list[str]) -> int:
    """
    Display a panel listing audits that have been deleted on CodeDD and prompt
    the user to choose an action.

    When more than one audit is deleted, options 3 and 4 are shown to make
    "accept all" and "restore all" explicit.

    Returns:
        1 = Confirm remote audit deletion (remove from local scope).
        2 = Restore local audit scope and sync to CodeDD (re-register).
        3 = Accept all deletions locally (same as 1; only when len(repo_names) > 1).
        4 = Restore all audits to CodeDD (same as 2; only when len(repo_names) > 1).
    """
    # Local directories (repos) with icon and color
    repo_list = "\n".join(
        f"  [cyan]{SYMBOL_DIR}[/cyan] [bold cyan]{name}[/bold cyan]"
        for name in sorted(repo_names)
    )
    multiple = len(repo_names) > 1

    # Options 1/3 = accept deletion (yellow); 2/4 = restore (green); descriptions dim
    options_text = (
        "  [bold yellow]1[/bold yellow]  Confirm remote audit deletion\n"
        "      [dim]Remove these from your local scope so it matches CodeDD.[/dim]\n\n"
        "  [bold green]2[/bold green]  Restore local audit scope and sync to CodeDD\n"
        "      [dim]Re-register the scope to recreate these audits on CodeDD.[/dim]"
    )
    choices_list = ["1", "2"]

    if multiple:
        options_text += (
            "\n\n"
            "  [bold yellow]3[/bold yellow]  Accept all deletions locally\n"
            "      [dim]Same as (1): remove all listed audits from local scope.[/dim]\n\n"
            "  [bold green]4[/bold green]  Restore all audits to CodeDD\n"
            "      [dim]Same as (2): re-register scope to recreate all listed audits on CodeDD.[/dim]"
        )
        choices_list = ["1", "2", "3", "4"]

    content = (
        "[bold]The following audit(s) no longer exist on CodeDD (deleted or cleared):[/bold]\n\n"
        f"{repo_list}\n\n"
        "[bold]What do you want to do?[/bold]\n\n"
        f"{options_text}"
    )
    console.print()
    console.print(Panel(content, title="[bold yellow]Audits deleted on CodeDD[/bold yellow]", border_style="yellow", padding=(1, 2)))
    console.print()
    choice = IntPrompt.ask("Enter your choice", choices=choices_list, default=1)
    return choice


# Base URL for the CodeDD cloud application (no trailing slash).
CODEDD_BASE_URL = "https://www.codedd.ai"


def print_audit_scope_cloud_info(audit_uuid: str, audit_type: str) -> None:
    """
    Display an info message after scope confirm/re-confirm with the URL to manage
    the audit scope in the CodeDD cloud application.

    Args:
        audit_uuid: The active audit UUID (group or single).
        audit_type: Either "group" or "single" to build the correct path.
    """
    if audit_type == "group":
        path = "audit-invitation"
    else:
        path = "audit-scope-selection"
    url = f"{CODEDD_BASE_URL}/{audit_uuid}/{path}"

    content = (
        "You can manage this audit in the CodeDD cloud application:\n\n"
        "  • Adapt which files are selected for the audit\n"
        "  • Delete scoped directories from the audit\n"
        "  • Pay for the audit and start the analysis\n\n"
        f"[bold cyan]{url}[/bold cyan]"
    )
    console.print()
    console.print(Panel(content, title="[bold]Audit scope at CodeDD[/bold]", border_style="blue", padding=(1, 2)))
    console.print()


def render_validation_result(dir_info: LocalDirectory) -> None:
    """
    Display a single directory validation result (success or error).

    Args:
        dir_info: A validated ``LocalDirectory`` instance.
    """
    if dir_info.is_valid:
        console.print(
            f"  [bold green]{SYMBOL_OK}[/bold green] [bold]{dir_info.repo_name}[/bold]  "
            f"[dim]({dir_info.branch} @ {dir_info.commit_hash})[/dim]  "
            f"{dir_info.path}"
        )
    else:
        console.print(
            f"  [bold red]{SYMBOL_FAIL}[/bold red] {dir_info.path}\n"
            f"    [red]{dir_info.error}[/red]"
        )
