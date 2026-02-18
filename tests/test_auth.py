"""
Tests for the auth module: token storage, retrieval, and deletion.
"""

import os

import pytest

from codedd_cli.auth.token_manager import TokenManager
from codedd_cli.utils.validators import is_valid_cli_token


class TestTokenValidation:
    """Test CLI token format validation."""

    def test_valid_token(self, sample_token):
        assert is_valid_cli_token(sample_token) is True

    def test_invalid_prefix(self):
        assert is_valid_cli_token("invalid_prefix_abc123") is False

    def test_too_short(self):
        assert is_valid_cli_token("codedd_cli_short") is False

    def test_empty_string(self):
        assert is_valid_cli_token("") is False

    def test_valid_with_hyphens_underscores(self):
        token = "codedd_cli_" + "a-b_c" * 10
        assert is_valid_cli_token(token) is True


class TestTokenHint:
    """Test the token_hint helper."""

    def test_hint_shows_last_four(self, sample_token):
        hint = TokenManager.token_hint(sample_token)
        assert hint.startswith("codedd_cli_...")
        assert hint.endswith(sample_token[-4:])

    def test_hint_for_short_string(self):
        assert TokenManager.token_hint("short") == "***"

    def test_hint_for_empty(self):
        assert TokenManager.token_hint("") == "***"


class TestTokenManagerEnvFallback:
    """Test that CODEDD_API_TOKEN environment variable is respected."""

    def test_env_variable_takes_precedence(self, monkeypatch, sample_token):
        monkeypatch.setenv("CODEDD_API_TOKEN", sample_token)
        result = TokenManager.retrieve()
        assert result == sample_token

    def test_env_variable_ignored_if_wrong_prefix(self, monkeypatch):
        monkeypatch.setenv("CODEDD_API_TOKEN", "wrong_prefix_token")
        # Should not return the env var since prefix is wrong
        # It will fall through to keyring (which may or may not have a value)
        result = TokenManager.retrieve()
        assert result != "wrong_prefix_token"
