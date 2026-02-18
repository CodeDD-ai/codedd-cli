"""
Secure LLM API key management for the CodeDD CLI.

Keys are stored in the operating system's credential store via the
``keyring`` library — the same mechanism used for the CLI auth token.
No keys are ever written to plain-text configuration files.

Supported providers:
    - ``anthropic``  (primary, claude-sonnet-4-6 / claude-haiku-4-5)
    - ``openai``     (fallback, gpt-5.2)

Provider preference (``anthropic``, ``openai``, or ``both``) is stored in
``~/.codedd/config.toml`` under the ``[llm]`` section.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx
import keyring

from codedd_cli.config.constants import KEYRING_SERVICE

logger = logging.getLogger(__name__)

# Keyring key names (same service as the CLI auth token)
_KEYRING_KEY_ANTHROPIC = "anthropic_api_key"
_KEYRING_KEY_OPENAI = "openai_api_key"

# Map of provider name → keyring key
_PROVIDER_KEYRING_MAP: dict[str, str] = {
    "anthropic": _KEYRING_KEY_ANTHROPIC,
    "openai": _KEYRING_KEY_OPENAI,
}

# Valid provider preference values for the [llm] section in config.toml
VALID_PROVIDERS = ("anthropic", "openai", "both")

# Models used by the CodeDD audit pipeline (mirrors server-side defaults)
PROVIDER_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5.2",
}

# Lightweight validation endpoints
_ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"


class LLMKeyManager:
    """
    Manage LLM API keys in the OS keychain.

    Usage::

        mgr = LLMKeyManager()
        mgr.store_key("anthropic", "sk-ant-...")
        key = mgr.retrieve_key("anthropic")
    """

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    @staticmethod
    def store_key(provider: str, api_key: str) -> None:
        """
        Persist an LLM API key in the OS keychain.

        Args:
            provider: ``"anthropic"`` or ``"openai"``.
            api_key:  The raw API key string.

        Raises:
            ValueError: If the provider name is not recognised.
            keyring.errors.KeyringError: If the keychain is unavailable.
        """
        keyring_key = _PROVIDER_KEYRING_MAP.get(provider)
        if not keyring_key:
            raise ValueError(
                f"Unknown provider '{provider}'. "
                f"Supported: {', '.join(_PROVIDER_KEYRING_MAP)}"
            )
        keyring.set_password(KEYRING_SERVICE, keyring_key, api_key)

    @staticmethod
    def retrieve_key(provider: str) -> Optional[str]:
        """
        Retrieve an LLM API key from the OS keychain.

        Returns:
            The key string, or ``None`` if no key is stored.
        """
        keyring_key = _PROVIDER_KEYRING_MAP.get(provider)
        if not keyring_key:
            return None
        try:
            key = keyring.get_password(KEYRING_SERVICE, keyring_key)
            return key if key else None
        except Exception:
            return None

    @staticmethod
    def remove_key(provider: str) -> bool:
        """
        Delete an LLM API key from the OS keychain.

        Returns:
            ``True`` if deleted, ``False`` if it did not exist or failed.
        """
        keyring_key = _PROVIDER_KEYRING_MAP.get(provider)
        if not keyring_key:
            return False
        try:
            keyring.delete_password(KEYRING_SERVICE, keyring_key)
            return True
        except keyring.errors.PasswordDeleteError:
            return False  # already absent
        except Exception:
            return False

    @staticmethod
    def has_key(provider: str) -> bool:
        """Return ``True`` if a key is stored for the given provider."""
        return LLMKeyManager.retrieve_key(provider) is not None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @staticmethod
    def get_configured_providers() -> list[str]:
        """
        Return a list of provider names that have keys stored.

        Example: ``["anthropic", "openai"]`` or ``["openai"]``.
        """
        return [p for p in _PROVIDER_KEYRING_MAP if LLMKeyManager.has_key(p)]

    @staticmethod
    def mask_key(api_key: str) -> str:
        """
        Return a masked representation of an API key for display.

        Shows the first 8 characters followed by ``...``.
        """
        if len(api_key) > 12:
            return api_key[:8] + "..."
        return "***"

    @staticmethod
    def mask_key_preview(api_key: str) -> str:
        """
        Return a short preview of an existing key: first 8 chars + "...." + last 4.

        Used when prompting whether to keep, update, or delete an existing key.
        """
        if len(api_key) <= 12:
            return "***"
        return api_key[:8] + "...." + api_key[-4:]

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_key(provider: str, api_key: str) -> tuple[bool, str]:
        """
        Validate an LLM API key by making a minimal API call.

        Uses raw ``httpx`` calls so the CLI does not require the full
        ``anthropic`` / ``openai`` SDK packages.

        Args:
            provider: ``"anthropic"`` or ``"openai"``.
            api_key:  The key to validate.

        Returns:
            Tuple of ``(is_valid, message)``.  ``is_valid`` is ``True``
            when the API accepted the key (the response may still contain
            a model error, but auth succeeded).
        """
        if provider == "anthropic":
            return LLMKeyManager._validate_anthropic(api_key)
        elif provider == "openai":
            return LLMKeyManager._validate_openai(api_key)
        return False, f"Unknown provider: {provider}"

    # -- Anthropic --

    @staticmethod
    def _validate_anthropic(api_key: str) -> tuple[bool, str]:
        """
        Send a minimal request to the Anthropic Messages API.

        A 200/400 with a model-related error means the key is valid;
        a 401 means it is rejected.
        """
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": "claude-haiku-4-5",
            "max_tokens": 5,
            "messages": [{"role": "user", "content": "ping"}],
        }
        try:
            resp = httpx.post(
                _ANTHROPIC_MESSAGES_URL,
                headers=headers,
                json=payload,
                timeout=15.0,
            )
            if resp.status_code == 200:
                return True, "Key is valid (Anthropic)."
            if resp.status_code == 401:
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                msg = body.get("error", {}).get("message", "Invalid API key.")
                return False, f"Authentication failed: {msg}"
            # Only 200 means valid. 400/404/429/etc. are not accepted as proof the key works.
            return False, f"Validation failed (HTTP {resp.status_code}). Key could not be verified."
        except httpx.TimeoutException:
            return False, "Validation timed out — could not reach Anthropic API."
        except httpx.ConnectError:
            return False, "Could not connect to Anthropic API."
        except Exception as exc:
            return False, f"Validation error: {exc}"

    # -- OpenAI --

    @staticmethod
    def _validate_openai(api_key: str) -> tuple[bool, str]:
        """
        Send a minimal request to the OpenAI Chat Completions API.

        A 200 means valid; a 401 means invalid key.
        """
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "gpt-5.2",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }
        try:
            resp = httpx.post(
                _OPENAI_CHAT_URL,
                headers=headers,
                json=payload,
                timeout=15.0,
            )
            if resp.status_code == 200:
                return True, "Key is valid (OpenAI)."
            if resp.status_code == 401:
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                msg = body.get("error", {}).get("message", "Invalid API key.")
                return False, f"Authentication failed: {msg}"
            # Only 200 means valid. 400/404/429/etc. are not accepted as proof the key works.
            return False, f"Validation failed (HTTP {resp.status_code}). Key could not be verified."
        except httpx.TimeoutException:
            return False, "Validation timed out — could not reach OpenAI API."
        except httpx.ConnectError:
            return False, "Could not connect to OpenAI API."
        except Exception as exc:
            return False, f"Validation error: {exc}"
