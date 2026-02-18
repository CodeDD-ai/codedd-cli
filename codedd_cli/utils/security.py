"""
Security utilities for the CLI.
"""


def mask_token(token: str) -> str:
    """
    Return a masked representation of a CLI token suitable for display.

    Example: ``codedd_cli_...xYz9``
    """
    if not token or len(token) < 16:
        return "***"
    return f"codedd_cli_...{token[-4:]}"
