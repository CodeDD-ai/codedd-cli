"""
Input validation helpers for the CLI.
"""

import re

# Standard UUID v4 pattern
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# CLI token format: codedd_cli_ followed by base64url characters (A-Za-z0-9_-)
# Base64url encoding can produce 64+ character strings after the prefix
_TOKEN_RE = re.compile(r"^codedd_cli_[A-Za-z0-9_\-]{32,}$")


def is_valid_uuid(value: str) -> bool:
    """Return True when *value* looks like a valid UUID v4."""
    if not value:
        return False
    return bool(_UUID_RE.match(value.strip()))


def is_valid_cli_token(value: str) -> bool:
    """
    Return True when *value* matches the expected CLI token format.
    
    Tokens are base64url-encoded (48 bytes = ~64 chars after encoding),
    so we require at least 32 characters after the prefix to ensure
    sufficient entropy. Handles whitespace stripping automatically.
    """
    if not value:
        return False
    # Strip whitespace (handles copy-paste issues)
    cleaned = value.strip()
    return bool(_TOKEN_RE.match(cleaned))
