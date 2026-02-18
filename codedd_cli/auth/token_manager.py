"""
Secure token storage and retrieval.

The CLI token is stored in the operating system's credential store via
the ``keyring`` library (Windows Credential Locker, macOS Keychain,
Linux Secret Service / KWallet).  A non-sensitive hint (last 4 chars)
is kept in the TOML config file for display purposes.

Falls back to the ``CODEDD_API_TOKEN`` environment variable when no
keyring entry exists (useful for CI/CD and headless environments).
"""

import os
from typing import Optional

import keyring

from codedd_cli.config.constants import (
    CLI_TOKEN_PREFIX,
    KEYRING_SERVICE,
    KEYRING_TOKEN_KEY,
)


class TokenManager:
    """Manage CLI token persistence in the OS keychain."""

    @staticmethod
    def store(token: str) -> None:
        """
        Persist a CLI token in the OS keychain.

        Args:
            token: The raw ``codedd_cli_...`` token string.

        Raises:
            keyring.errors.KeyringError: If the keychain is unavailable.
        """
        keyring.set_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY, token)

    @staticmethod
    def retrieve() -> Optional[str]:
        """
        Retrieve the CLI token.

        Resolution order:
            1. ``CODEDD_API_TOKEN`` environment variable
            2. OS keychain entry

        Returns:
            The token string, or None if not found.
        """
        # Environment variable takes precedence (CI/CD support)
        env_token = os.environ.get("CODEDD_API_TOKEN")
        if env_token and env_token.startswith(CLI_TOKEN_PREFIX):
            return env_token

        try:
            token = keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY)
            return token if token else None
        except Exception:
            return None

    @staticmethod
    def delete() -> None:
        """
        Remove the CLI token from the OS keychain.

        Silently succeeds if no entry exists.
        """
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_TOKEN_KEY)
        except keyring.errors.PasswordDeleteError:
            pass  # Already absent
        except Exception:
            pass

    @staticmethod
    def token_hint(token: str) -> str:
        """
        Derive a non-sensitive hint from a token for display.

        Example: ``codedd_cli_...xYz9``
        """
        if len(token) > 8:
            return f"{CLI_TOKEN_PREFIX}...{token[-4:]}"
        return "***"
