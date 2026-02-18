"""Data model for account information returned by the API."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountInfo:
    """Minimal account information returned after token verification."""

    account_uuid: str
    account_name: str
    email: str  # Masked by the server (e.g. j***@example.com)
    token_name: str = ""
