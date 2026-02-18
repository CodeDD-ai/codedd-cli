"""
``codedd audits`` sub-commands: list, select.
"""

from typing import Optional

import typer
from rich.console import Console
from rich.prompt import IntPrompt

from codedd_cli.api.client import CodeDDClient
from codedd_cli.api.endpoints import Endpoints
from codedd_cli.auth.session import require_auth
from codedd_cli.config.settings import ConfigManager
from codedd_cli.models.audit import Audit, GroupAudit
from codedd_cli.utils.display import (
    print_error,
    print_info,
    print_success,
    render_audit_detail,
    render_audits_table,
)
from codedd_cli.utils.payload_inspector import review_request
from codedd_cli.utils.validators import is_valid_uuid

console = Console()
audits_app = typer.Typer(no_args_is_help=True)


def _fetch_audits(
    client: CodeDDClient,
    offset: int = 0,
    limit: int = 20,
    audit_type: Optional[str] = None,
) -> tuple[list[Audit], list[GroupAudit], int]:
    """
    Call the CLI audits endpoint and parse the response into model objects.

    Returns:
        Tuple of (single_audits, group_audits, total_count).
    """
    params: dict[str, str | int] = {"offset": offset, "limit": limit}
    if audit_type:
        params["type"] = audit_type

    resp = client.get(Endpoints.LIST_AUDITS, params=params)

    if resp.status_code == 401:
        print_error("Authentication failed.  Run [bold cyan]codedd auth login[/bold cyan].")
        raise typer.Exit(code=1)

    if resp.status_code != 200:
        msg = "Failed to fetch audits"
        try:
            msg = resp.json().get("message", msg)
        except Exception:
            pass
        print_error(msg)
        raise typer.Exit(code=1)

    data = resp.json()
    if data.get("status") != "success":
        print_error(data.get("message", "Unknown error from server"))
        raise typer.Exit(code=1)

    single_audits = [
        Audit(
            audit_uuid=a["audit_uuid"],
            audit_name=a.get("audit_name", ""),
            audit_status=a.get("audit_status", ""),
            audit_type=a.get("audit_type", "single"),
            ai_synthesis=a.get("ai_synthesis"),
            repo_url=a.get("repo_url", ""),
            number_files=a.get("number_files", 0),
            lines_of_code=a.get("lines_of_code", 0),
        )
        for a in data.get("audits_data", [])
    ]

    group_audits = [
        GroupAudit(
            audit_uuid=g["audit_uuid"],
            audit_name=g.get("audit_name", ""),
            audit_status=g.get("audit_status", ""),
            audit_type=g.get("audit_type", "group"),
            ai_synthesis=g.get("ai_synthesis"),
            number_of_sub_audits=g.get("number_of_sub_audits", 0),
            number_files=g.get("number_files", 0),
            lines_of_code=g.get("lines_of_code", 0),
        )
        for g in data.get("group_audits_data", [])
    ]

    total = data.get("total_audits", len(single_audits) + len(group_audits))
    return single_audits, group_audits, total


@audits_app.command("list")
@require_auth
def list_audits(
    audit_type: Optional[str] = typer.Option(
        None,
        "--type",
        "-t",
        help="Filter by audit type: 'single' or 'group'.",
    ),
    limit: int = typer.Option(10, "--limit", "-l", help="Number of audits per page (default 10, max 100)."),
    page: int = typer.Option(1, "--page", "-p", help="Page number (1-indexed)."),
    show: bool = typer.Option(
        False,
        "--show",
        "-s",
        help="Write the API request to a file and open it for review before sending.",
    ),
) -> None:
    """
    List your audits on the CodeDD platform.

    Displays a table with audit name, status, file count, lines of code,
    and date.  Use --type to filter by single or group audits.
    """
    if audit_type and audit_type not in ("single", "group"):
        print_error("--type must be 'single' or 'group'")
        raise typer.Exit(code=1)

    if limit < 1 or limit > 100:
        print_error("--limit must be between 1 and 100")
        raise typer.Exit(code=1)

    offset = (page - 1) * limit

    cfg = ConfigManager()

    params: dict[str, str | int] = {"offset": offset, "limit": limit}
    if audit_type:
        params["type"] = audit_type

    if show:
        confirmed = review_request(
            "GET",
            Endpoints.LIST_AUDITS,
            params=params,
            command_label="Audits List",
            context_note="This request fetches your list of audits from CodeDD. Only pagination and optional type filter are sent.",
            confirm_prompt="Proceed with this request to CodeDD?",
        )
        if not confirmed:
            print_info("Cancelled.")
            raise typer.Exit(code=0)

    with CodeDDClient(config=cfg) as client:
        single_audits, group_audits, total = _fetch_audits(
            client, offset=offset, limit=limit, audit_type=audit_type
        )

    if not single_audits and not group_audits:
        print_info("No audits found. Create an audit at [bold cyan]https://codedd.ai[/bold cyan]")
        return

    active_uuid = (cfg.active_audit_uuid or "").strip() or None
    render_audits_table(single_audits, group_audits, total, active_audit_uuid=active_uuid)

    # Pagination info
    total_pages = max(1, (total + limit - 1) // limit)
    if total_pages > 1:
        print_info(f"Page {page}/{total_pages}  (use --page to navigate)")


@audits_app.command("select")
@require_auth
def select_audit(
    audit_uuid: Optional[str] = typer.Argument(
        None,
        help="List index (1, 2, …) or UUID of the audit to select (omit for interactive).",
    ),
    show: bool = typer.Option(
        False,
        "--show",
        "-s",
        help="Write the API request to a file and open it for review before sending.",
    ),
) -> None:
    """
    Select an audit as the active audit for subsequent CLI commands.

    Pass the row number from [bold]codedd audits list[/bold] (e.g. 1, 2) or a
    full UUID. If omitted, an interactive list is shown.
    """
    cfg = ConfigManager()

    list_params: dict[str, str | int] = {"offset": 0, "limit": 50}
    if show:
        confirmed = review_request(
            "GET",
            Endpoints.LIST_AUDITS,
            params=list_params,
            command_label="Audits Select (Fetch List)",
            context_note="This request fetches your list of audits from CodeDD so you can select one. Only pagination params are sent.",
            confirm_prompt="Proceed with this request to CodeDD?",
        )
        if not confirmed:
            print_info("Cancelled.")
            raise typer.Exit(code=0)

    with CodeDDClient(config=cfg) as client:
        single_audits, group_audits, total = _fetch_audits(client, limit=50)

    # Build a combined ordered list: group audits first, then single
    all_audits: list[Audit | GroupAudit] = list(group_audits) + list(single_audits)

    if not all_audits:
        print_info("No audits available.")
        raise typer.Exit()

    if audit_uuid:
        audit_uuid = audit_uuid.strip()

        # Selection by 1-based list index (e.g. "1", "2" from "codedd audits list")
        try:
            index = int(audit_uuid)
            if 1 <= index <= len(all_audits):
                _set_active(cfg, all_audits[index - 1])
                return
            print_error(f"Index must be between 1 and {len(all_audits)}.")
            raise typer.Exit(code=1)
        except ValueError:
            pass  # Not an integer; treat as UUID below

        # Direct selection by UUID
        if not is_valid_uuid(audit_uuid):
            print_error("Invalid UUID format. Use a list index (e.g. 1) or a full UUID.")
            raise typer.Exit(code=1)

        match = next(
            (a for a in all_audits if a.audit_uuid == audit_uuid),
            None,
        )
        if not match:
            print_error(f"Audit {audit_uuid} not found in your audits.")
            raise typer.Exit(code=1)

        _set_active(cfg, match)
        return

    # Interactive selection
    active_uuid = (cfg.active_audit_uuid or "").strip() or None
    render_audits_table(
        [a for a in all_audits if isinstance(a, Audit)],
        [a for a in all_audits if isinstance(a, GroupAudit)],
        total,
        active_audit_uuid=active_uuid,
    )

    console.print()
    choice = IntPrompt.ask(
        "Select an audit by number",
        default=1,
        choices=[str(i) for i in range(1, len(all_audits) + 1)],
    )

    selected = all_audits[choice - 1]
    _set_active(cfg, selected)


def _set_active(cfg: ConfigManager, audit: Audit | GroupAudit) -> None:
    """Persist the selected audit in config and render its details."""
    cfg.set_active_audit(
        audit_uuid=audit.audit_uuid,
        audit_type=audit.audit_type,
        audit_name=audit.audit_name,
    )
    print_success(f"Active audit set to [bold]{audit.audit_name}[/bold]")
    render_audit_detail(audit)
