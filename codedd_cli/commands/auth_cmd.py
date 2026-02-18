"""
``codedd auth`` sub-commands: login, logout, status.
"""

from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel

from codedd_cli.api.client import CodeDDClient
from codedd_cli.api.endpoints import Endpoints
from codedd_cli.auth.token_manager import TokenManager
from codedd_cli.config.settings import ConfigManager
from codedd_cli.models.account import AccountInfo
from codedd_cli.utils.display import print_error, print_info, print_success
from codedd_cli.utils.payload_inspector import review_request
from codedd_cli.utils.validators import is_valid_cli_token

console = Console()
auth_app = typer.Typer(no_args_is_help=True)


@auth_app.command("login")
def login(
    token: Optional[str] = typer.Option(
        None,
        "--token",
        "-t",
        help="CLI token (omit to be prompted interactively).",
        prompt=False,
    ),
    show: bool = typer.Option(
        False,
        "--show",
        "-s",
        help="Write the verify request to a file and open it for review before sending.",
    ),
) -> None:
    """
    Authenticate with the CodeDD platform using a CLI token.

    Generate a token from your account settings at https://codedd.ai
    (Account → CLI Access → Generate Token), then run:

        codedd auth login --token <your_token>

    Or simply run ``codedd auth login`` to be prompted.
    """
    # Interactive prompt if --token not given
    if not token:
        token = typer.prompt("Paste your CLI token", hide_input=True)

    # Strip whitespace and newlines (handles copy-paste issues)
    token = token.strip().replace("\n", "").replace("\r", "")

    # Local format check
    if not is_valid_cli_token(token):
        # Provide more helpful error message
        if not token:
            print_error("Token cannot be empty.")
        elif not token.startswith("codedd_cli_"):
            print_error(
                f"Invalid token format. Token must start with [bold]codedd_cli_[/bold].\n"
                f"Received: [dim]{token[:20]}...[/dim]"
            )
        else:
            print_error(
                f"Invalid token format. Token appears too short or contains invalid characters.\n"
                f"Expected: [bold]codedd_cli_[/bold] followed by at least 32 base64url characters."
            )
        raise typer.Exit(code=1)

    # Verify against server
    console.print("[dim]Verifying token with CodeDD server…[/dim]")
    cfg = ConfigManager()

    # Temporarily store token so the client can use it
    TokenManager.store(token)

    try:
        if show:
            confirmed = review_request(
                "POST",
                Endpoints.VERIFY_TOKEN,
                command_label="Auth Verify (Login)",
                context_note="This request verifies your CLI token with CodeDD. The token is sent in the X-CLI-Token header (value not shown in this file).",
                confirm_prompt="Proceed with sending this request to CodeDD?",
            )
            if not confirmed:
                TokenManager.delete()
                print_info("Cancelled.")
                raise typer.Exit(code=0)

        with CodeDDClient(config=cfg) as client:
            resp = client.post(Endpoints.VERIFY_TOKEN)

        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                account = AccountInfo(
                    account_uuid=data["account_uuid"],
                    account_name=data.get("account_name", ""),
                    email=data.get("email", ""),
                    token_name=data.get("token_name", ""),
                )

                # Persist session metadata
                cfg.account_uuid = account.account_uuid
                cfg.account_name = account.account_name
                cfg.token_hint = TokenManager.token_hint(token)
                cfg.save()

                print_success(f"Authenticated as [bold]{account.account_name}[/bold] ({account.email})")
                if account.token_name:
                    print_info(f"Token: {account.token_name}")
                return

        # Auth failed — clean up
        TokenManager.delete()
        error_msg = "Authentication failed"
        try:
            error_msg = resp.json().get("message", error_msg)
        except Exception:
            pass
        print_error(error_msg)
        raise typer.Exit(code=1)

    except Exception as exc:
        TokenManager.delete()
        if isinstance(exc, typer.Exit):
            raise
        print_error(f"Connection error: {exc}")
        raise typer.Exit(code=1)


@auth_app.command("logout")
def logout() -> None:
    """
    Remove stored credentials and end the current CLI session.
    """
    cfg = ConfigManager()

    if not cfg.is_authenticated:
        print_info("You are not currently logged in.")
        return

    account_name = cfg.account_name or "user"
    TokenManager.delete()
    cfg.clear_session()

    print_success(f"Logged out ([dim]{account_name}[/dim]).")


@auth_app.command("status")
def status(
    show: bool = typer.Option(
        False,
        "--show",
        "-s",
        help="Write the verify request to a file and open it for review before sending.",
    ),
) -> None:
    """
    Show the current authentication state.
    """
    cfg = ConfigManager()
    token = TokenManager.retrieve()

    if not cfg.is_authenticated or not token:
        console.print(
            Panel(
                "[bold red]Not authenticated[/bold red]\n\n"
                "Run [bold cyan]codedd auth login[/bold cyan] to connect.",
                title="Auth Status",
                expand=False,
            )
        )
        return

    # Optionally verify token is still valid server-side
    if show:
        confirmed = review_request(
            "POST",
            Endpoints.VERIFY_TOKEN,
            command_label="Auth Verify (Status)",
            context_note="This request verifies your CLI token with CodeDD. The token is sent in the X-CLI-Token header (value not shown in this file).",
            confirm_prompt="Proceed with sending this request to CodeDD?",
        )
        if not confirmed:
            print_info("Cancelled. Skipping server verification.")
            # Still show local state
            panel_text = (
                f"[bold green]Authenticated (local)[/bold green]\n\n"
                f"[bold]Account:[/bold]  {cfg.account_name}\n"
                f"[bold]Token:[/bold]    {cfg.token_hint}\n"
                f"[bold]API URL:[/bold] {cfg.api_url}\n\n"
                "[dim]Server verification was skipped (--show cancelled).[/dim]"
            )
            console.print(Panel(panel_text, title="Auth Status", expand=False))
            return

    with CodeDDClient(config=cfg) as client:
        try:
            resp = client.post(Endpoints.VERIFY_TOKEN)
            if resp.status_code == 200 and resp.json().get("status") == "success":
                server_info = resp.json()
                panel_text = (
                    f"[bold green]Authenticated[/bold green]\n\n"
                    f"[bold]Account:[/bold]  {server_info.get('account_name', cfg.account_name)}\n"
                    f"[bold]Email:[/bold]    {server_info.get('email', '—')}\n"
                    f"[bold]Token:[/bold]    {cfg.token_hint}\n"
                    f"[bold]API URL:[/bold] {cfg.api_url}"
                )
            else:
                panel_text = (
                    f"[bold yellow]Token invalid or expired[/bold yellow]\n\n"
                    f"[bold]Account:[/bold]  {cfg.account_name}\n"
                    f"[bold]Token:[/bold]    {cfg.token_hint}\n\n"
                    "Run [bold cyan]codedd auth login[/bold cyan] with a new token."
                )
        except Exception:
            panel_text = (
                f"[bold yellow]Could not reach server[/bold yellow]\n\n"
                f"[bold]Account:[/bold]  {cfg.account_name}\n"
                f"[bold]Token:[/bold]    {cfg.token_hint}\n"
                f"[bold]API URL:[/bold] {cfg.api_url}"
            )

    console.print(Panel(panel_text, title="Auth Status", expand=False))
