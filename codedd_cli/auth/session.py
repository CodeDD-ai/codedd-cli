"""
Session helpers for ensuring the user is authenticated before running
commands that require a token.
"""

import functools
from typing import Callable

import typer
from rich.console import Console

from codedd_cli.auth.token_manager import TokenManager
from codedd_cli.config.settings import ConfigManager

console = Console()


def require_auth(func: Callable) -> Callable:
    """
    Decorator that aborts a CLI command when the user is not authenticated.

    It checks both the config file (for session metadata) and the keyring
    (for the actual token).  If either is missing it prints a helpful
    message and exits with code 1.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        cfg = ConfigManager()
        token = TokenManager.retrieve()

        if not cfg.is_authenticated or not token:
            console.print(
                "[bold red]Not authenticated.[/bold red]  "
                "Run [bold cyan]codedd auth login[/bold cyan] first."
            )
            raise typer.Exit(code=1)

        return func(*args, **kwargs)

    return wrapper
