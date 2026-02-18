"""
Root CLI application.

Defines the top-level ``codedd`` command group and registers sub-command
modules for ``auth``, ``audits``, ``config``.
"""

import click
import typer
from typer.core import TyperGroup

from codedd_cli import __version__
from codedd_cli.utils.display import print_banner


class _CodeDDTyperGroup(TyperGroup):
    """
    Custom Typer group that prints the CodeDD banner before the root help.
    Only top-level ``codedd --help`` (or ``codedd`` with no args) shows the banner.
    """

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if ctx.parent is None:
            print_banner()
        super().format_help(ctx, formatter)


app = typer.Typer(
    name="codedd",
    help="CodeDD CLI — run code audits from your terminal.",
    no_args_is_help=True,
    add_completion=False,
    cls=_CodeDDTyperGroup,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"codedd-cli {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="Show the CLI version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """CodeDD CLI — run code audits from your terminal."""


# ---- Register sub-command groups ----

from codedd_cli.commands.auth_cmd import auth_app  # noqa: E402
from codedd_cli.commands.audits_cmd import audits_app  # noqa: E402
from codedd_cli.commands.config_cmd import config_app  # noqa: E402
from codedd_cli.commands.scope_cmd import scope_app  # noqa: E402
from codedd_cli.commands.audit_cmd import audit_app  # noqa: E402

app.add_typer(auth_app, name="auth", help="Authenticate with the CodeDD platform.")
app.add_typer(audits_app, name="audits", help="List and select audits.")
app.add_typer(scope_app, name="scope", help="Manage local directories for the active audit.")
app.add_typer(audit_app, name="audit", help="Start and manage audits.")
app.add_typer(config_app, name="config", help="View and update CLI configuration.")
