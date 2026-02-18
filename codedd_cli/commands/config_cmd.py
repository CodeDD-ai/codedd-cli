"""
``codedd config`` sub-commands.

General configuration:
    show        – display the current CLI configuration
    set         – update a configuration value (e.g. ``api_url``)

LLM key management:
    set-key     – store an Anthropic or OpenAI API key in the OS keychain
    show-keys   – show which LLM providers have keys configured
    remove-key  – remove a stored LLM API key
    provider    – set the preferred LLM provider (anthropic / openai / both)
"""

import getpass
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt
from rich.table import Table

from codedd_cli.config.settings import ConfigManager
from codedd_cli.llm.key_manager import (
    PROVIDER_MODELS,
    VALID_PROVIDERS,
    LLMKeyManager,
)
from codedd_cli.utils.display import SYMBOL_FAIL, SYMBOL_INFO, SYMBOL_OK, SYMBOL_WARN

console = Console()
config_app = typer.Typer(no_args_is_help=True)

# Keys that users are allowed to modify
_WRITABLE_KEYS = {
    "api_url": ("server", "api_url"),
}


@config_app.command("show")
def show() -> None:
    """
    Display the current CLI configuration.
    """
    cfg = ConfigManager()

    lines = [
        f"[bold]Config file:[/bold]  {cfg.config_path}",
        "",
        "[bold underline]Server[/bold underline]",
        f"  api_url       = {cfg.api_url}",
        "",
        "[bold underline]Session[/bold underline]",
        f"  authenticated = {'[green]yes[/green]' if cfg.is_authenticated else '[red]no[/red]'}",
        f"  account_name  = {cfg.account_name or '—'}",
        f"  account_uuid  = {cfg.account_uuid[:8] + '…' if cfg.account_uuid else '—'}",
        f"  token_hint    = {cfg.token_hint or '—'}",
        "",
        "[bold underline]Active Audit[/bold underline]",
        f"  audit_uuid    = {cfg.active_audit_uuid[:8] + '…' if cfg.active_audit_uuid else '—'}",
        f"  audit_type    = {cfg.active_audit_type or '—'}",
        f"  audit_name    = {cfg.active_audit_name or '—'}",
        "",
        "[bold underline]LLM[/bold underline]",
        f"  provider      = {cfg.llm_provider}",
        f"  concurrency   = {cfg.llm_concurrency}",
    ]

    console.print(Panel("\n".join(lines), title="CodeDD CLI Configuration", expand=False))


@config_app.command("set")
def set_value(
    key: str = typer.Argument(help="Configuration key to set (e.g. api_url)."),
    value: str = typer.Argument(help="Value to assign to the key."),
) -> None:
    """
    Update a configuration value.

    Currently supported keys: ``api_url``

    Examples:
        codedd config set api_url http://localhost:8000/django_codedd
        codedd config set api_url https://api.codedd.ai/django_codedd
    """
    if key not in _WRITABLE_KEYS:
        console.print(
            f"[red]Unknown key:[/red] {key}\n"
            f"Supported keys: {', '.join(_WRITABLE_KEYS.keys())}"
        )
        raise typer.Exit(code=1)

    # Validate api_url format
    if key == "api_url":
        value = value.rstrip("/")
        if not (value.startswith("http://") or value.startswith("https://")):
            console.print(
                "[red]Invalid URL format.[/red] "
                "URL must start with http:// or https://\n"
                "Example: [bold]http://localhost:8000/django_codedd[/bold]"
            )
            raise typer.Exit(code=1)
        
        # Warn if /django_codedd path is missing
        if "/django_codedd" not in value:
            console.print(
                "[yellow]Warning:[/yellow] URL doesn't include '/django_codedd' path.\n"
                "Did you mean: [bold]" + value + "/django_codedd[/bold] ?\n"
                "[dim]Continuing with the provided URL...[/dim]"
            )

    section, config_key = _WRITABLE_KEYS[key]
    cfg = ConfigManager()
    cfg.set(section, config_key, value)

    console.print(f"[green]{SYMBOL_OK}[/green] Set [bold]{key}[/bold] = {value}")
    
    if key == "api_url":
        console.print(
            "[dim]Tip:[/dim] Use [bold]codedd config show[/bold] to verify your configuration."
        )


# ---------------------------------------------------------------------------
# codedd config set-key
# ---------------------------------------------------------------------------

@config_app.command("set-key")
def set_key(
    provider: Optional[str] = typer.Argument(
        None,
        help="LLM provider: 'anthropic' or 'openai'. Prompted if omitted.",
    ),
    skip_validation: bool = typer.Option(
        False,
        "--skip-validation",
        help="Store the key without making a validation API call.",
    ),
) -> None:
    """
    Store an LLM API key securely in the OS keychain.

    The key is validated with a lightweight API call before storage
    (unless ``--skip-validation`` is passed).  If validation fails the
    user is asked whether to store the key anyway.

    Examples::

        codedd config set-key anthropic
        codedd config set-key openai
        codedd config set-key              # interactive provider prompt
    """
    # -- Provider selection --
    if provider is None:
        provider = _prompt_provider_choice()

    provider = provider.lower().strip()
    if provider not in ("anthropic", "openai"):
        console.print(
            f"[red]{SYMBOL_FAIL}[/red] Unknown provider [bold]{provider}[/bold].  "
            "Use [bold]anthropic[/bold] or [bold]openai[/bold]."
        )
        raise typer.Exit(code=1)

    model = PROVIDER_MODELS.get(provider, "")
    console.print(
        f"\n[bold]Provider:[/bold] {provider}  "
        f"[dim](model: {model})[/dim]\n"
    )

    # -- If key already exists, show preview and prompt Keep / Update / Delete --
    existing_key = LLMKeyManager.retrieve_key(provider)
    if existing_key:
        preview = LLMKeyManager.mask_key_preview(existing_key)
        console.print(
            f"  [dim]A key for [bold]{provider}[/bold] is already stored:[/dim] "
            f"[bold]{preview}[/bold]\n"
        )
        choice = _prompt_keep_update_delete()
        if choice == 1:
            console.print(f"  [green]{SYMBOL_OK}[/green] Keeping existing {provider} key.")
            return
        if choice == 3:
            LLMKeyManager.remove_key(provider)
            console.print(f"  [green]{SYMBOL_OK}[/green] {provider} API key removed.")
            return
        # choice == 2: Update — fall through to paste/validate/store

    # -- Key input (masked) --
    try:
        api_key = getpass.getpass(f"  Paste your {provider} API key: ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[dim]Cancelled.[/dim]")
        raise typer.Exit()

    if not api_key:
        console.print(f"[red]{SYMBOL_FAIL}[/red] No key entered.")
        raise typer.Exit(code=1)

    # -- Validation --
    if not skip_validation:
        console.print(f"\n[dim]Validating key with {provider} API...[/dim]")
        is_valid, message = LLMKeyManager.validate_key(provider, api_key)

        if is_valid:
            console.print(f"  [green]{SYMBOL_OK}[/green] {message}")
        else:
            console.print(f"  [yellow]{SYMBOL_WARN}[/yellow] {message}")
            if not Confirm.ask("  Store the key anyway?", default=False):
                console.print("[dim]Key not stored.[/dim]")
                raise typer.Exit()

    # -- Store --
    try:
        LLMKeyManager.store_key(provider, api_key)
    except Exception as exc:
        console.print(
            f"[red]{SYMBOL_FAIL}[/red] Failed to store key in OS keychain: {exc}"
        )
        raise typer.Exit(code=1)

    masked = LLMKeyManager.mask_key(api_key)
    console.print(
        f"\n[green]{SYMBOL_OK}[/green] {provider} API key stored "
        f"[dim]({masked})[/dim]"
    )

    # -- Auto-set provider preference if this is the first key --
    cfg = ConfigManager()
    configured = LLMKeyManager.get_configured_providers()
    if len(configured) == 1:
        cfg.llm_provider = configured[0]
        cfg.save()
        console.print(
            f"[dim]{SYMBOL_INFO}[/dim] LLM provider set to "
            f"[bold]{configured[0]}[/bold]."
        )
    elif len(configured) == 2 and cfg.llm_provider not in VALID_PROVIDERS:
        cfg.llm_provider = "both"
        cfg.save()
        console.print(
            f"[dim]{SYMBOL_INFO}[/dim] Both providers configured — "
            "using Anthropic (primary) + OpenAI (fallback)."
        )


# ---------------------------------------------------------------------------
# codedd config show-keys
# ---------------------------------------------------------------------------

@config_app.command("show-keys")
def show_keys() -> None:
    """
    Show which LLM providers have API keys stored in the OS keychain.

    Keys are displayed in masked form (first 8 characters only).
    """
    cfg = ConfigManager()
    preference = cfg.llm_provider

    table = Table(
        title="LLM API Keys",
        show_header=True,
        padding=(0, 1),
    )
    table.add_column("Provider", style="bold", width=12)
    table.add_column("Key", width=20)
    table.add_column("Model", width=24)
    table.add_column("Status", width=14)

    for provider in ("anthropic", "openai"):
        key = LLMKeyManager.retrieve_key(provider)
        if key:
            masked = LLMKeyManager.mask_key(key)
            model = PROVIDER_MODELS.get(provider, "")
            # Determine if this provider is active based on preference
            if preference == "both" or preference == provider:
                status = "[green]active[/green]"
            else:
                status = "[dim]stored[/dim]"
            table.add_row(provider, masked, model, status)
        else:
            table.add_row(provider, "[dim]not set[/dim]", "", "[dim]—[/dim]")

    console.print(table)
    console.print(
        f"\n[dim]Provider preference:[/dim] [bold]{preference}[/bold]"
    )
    console.print(
        "[dim]Use [bold]codedd config set-key[/bold] to add a key, "
        "[bold]codedd config provider[/bold] to change preference.[/dim]"
    )


# ---------------------------------------------------------------------------
# codedd config remove-key
# ---------------------------------------------------------------------------

@config_app.command("remove-key")
def remove_key(
    provider: str = typer.Argument(
        help="LLM provider whose key to remove: 'anthropic' or 'openai'.",
    ),
) -> None:
    """
    Remove an LLM API key from the OS keychain.
    """
    provider = provider.lower().strip()
    if provider not in ("anthropic", "openai"):
        console.print(
            f"[red]{SYMBOL_FAIL}[/red] Unknown provider [bold]{provider}[/bold].  "
            "Use [bold]anthropic[/bold] or [bold]openai[/bold]."
        )
        raise typer.Exit(code=1)

    if not LLMKeyManager.has_key(provider):
        console.print(
            f"[dim]{SYMBOL_INFO}[/dim] No {provider} key is currently stored."
        )
        raise typer.Exit()

    if not Confirm.ask(
        f"Remove the stored [bold]{provider}[/bold] API key?", default=False
    ):
        console.print("[dim]Cancelled.[/dim]")
        raise typer.Exit()

    removed = LLMKeyManager.remove_key(provider)
    if removed:
        console.print(
            f"[green]{SYMBOL_OK}[/green] {provider} API key removed."
        )
    else:
        console.print(
            f"[red]{SYMBOL_FAIL}[/red] Failed to remove {provider} key."
        )


# ---------------------------------------------------------------------------
# codedd config provider
# ---------------------------------------------------------------------------

@config_app.command("provider")
def set_provider(
    value: str = typer.Argument(
        help="Preferred LLM provider: 'anthropic', 'openai', or 'both'.",
    ),
) -> None:
    """
    Set the preferred LLM provider for audits.

    - ``anthropic``  — use Anthropic only (claude-sonnet-4-6)
    - ``openai``     — use OpenAI only (gpt-5.2)
    - ``both``       — Anthropic primary, OpenAI fallback (recommended)
    """
    value = value.lower().strip()
    if value not in VALID_PROVIDERS:
        console.print(
            f"[red]{SYMBOL_FAIL}[/red] Invalid provider [bold]{value}[/bold].  "
            f"Choose one of: {', '.join(VALID_PROVIDERS)}"
        )
        raise typer.Exit(code=1)

    # Warn if the selected provider has no key stored
    if value in ("anthropic", "openai") and not LLMKeyManager.has_key(value):
        console.print(
            f"[yellow]{SYMBOL_WARN}[/yellow] No {value} API key stored.  "
            f"Run [bold cyan]codedd config set-key {value}[/bold cyan] first."
        )
    elif value == "both":
        missing = [p for p in ("anthropic", "openai") if not LLMKeyManager.has_key(p)]
        if missing:
            console.print(
                f"[yellow]{SYMBOL_WARN}[/yellow] Missing key(s) for: "
                f"{', '.join(missing)}.  "
                "Add them with [bold cyan]codedd config set-key[/bold cyan]."
            )

    cfg = ConfigManager()
    cfg.llm_provider = value
    cfg.save()
    console.print(
        f"[green]{SYMBOL_OK}[/green] LLM provider preference set to "
        f"[bold]{value}[/bold]."
    )


# ---------------------------------------------------------------------------
# codedd config concurrency
# ---------------------------------------------------------------------------

@config_app.command("concurrency")
def set_concurrency(
    value: int = typer.Argument(
        help="Max parallel LLM calls during file auditing (1–32).",
    ),
) -> None:
    """
    Set the maximum number of concurrent LLM API calls during audits.

    Higher values speed up audits but may trigger rate-limit errors
    depending on your API tier:

    \b
      1–2    Free / starter tier (conservative)
      4      Default — works well for most paid tiers
      8–16   High-throughput tiers (Anthropic Scale, OpenAI Tier 4+)
      32     Maximum — only if your provider contract allows it
    """
    if value < 1 or value > 32:
        console.print(
            f"[red]{SYMBOL_FAIL}[/red] Value must be between 1 and 32 (got {value})."
        )
        raise typer.Exit(code=1)

    cfg = ConfigManager()
    cfg.llm_concurrency = value
    cfg.save()
    console.print(
        f"[green]{SYMBOL_OK}[/green] LLM concurrency set to "
        f"[bold]{value}[/bold] parallel calls."
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _prompt_keep_update_delete() -> int:
    """Prompt user to Keep (1), Update (2), or Delete (3) the existing key."""
    console.print(
        "  [bold cyan]1[/bold cyan]  Keep existing key\n"
        "  [bold cyan]2[/bold cyan]  Update with a new key\n"
        "  [bold cyan]3[/bold cyan]  Delete stored key\n"
    )
    return IntPrompt.ask("  Enter 1, 2, or 3", choices=["1", "2", "3"], default=1)


def _prompt_provider_choice() -> str:
    """Interactive prompt to choose a provider when none was supplied."""
    console.print(
        "\n[bold]Select LLM provider:[/bold]\n"
        "[dim]CodeDD uses LLMs to analyze your source code during the audit. "
        "Your API key is stored in your system keychain (or equivalent) and is "
        "never written to config files.[/dim]\n"
    )
    console.print("  [bold cyan]1[/bold cyan]  Anthropic  [dim](claude-sonnet-4-6)[/dim]")
    console.print("  [bold cyan]2[/bold cyan]  OpenAI     [dim](gpt-5.2)[/dim]")
    console.print()
    choice = typer.prompt("  Enter 1 or 2", type=int, default=1)
    if choice == 2:
        return "openai"
    return "anthropic"
