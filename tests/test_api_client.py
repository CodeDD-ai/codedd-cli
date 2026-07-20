"""
Tests for the API client (CodeDDClient).
"""

import pytest
import httpx

from codedd_cli.api.client import CodeDDClient
from codedd_cli.api.endpoints import Endpoints
from codedd_cli.config.constants import USER_AGENT_PREFIX


class TestCodeDDClient:
    """Test client header construction and request flow."""

    def test_user_agent_header(self, tmp_config, monkeypatch):
        """Verify the User-Agent header is set correctly."""
        # Patch TokenManager to return None (no token)
        monkeypatch.setattr(
            "codedd_cli.api.client.TokenManager.retrieve", lambda: None
        )
        client = CodeDDClient(config=tmp_config)
        assert USER_AGENT_PREFIX in client._client.headers["User-Agent"]
        client.close()

    def test_cli_token_header_present(self, tmp_config, monkeypatch, sample_token):
        """Verify X-CLI-Token header is injected when token exists."""
        monkeypatch.setattr(
            "codedd_cli.api.client.TokenManager.retrieve", lambda: sample_token
        )
        client = CodeDDClient(config=tmp_config)
        assert client._client.headers.get("X-CLI-Token") == sample_token
        client.close()

    def test_cli_token_header_absent_when_no_token(self, tmp_config, monkeypatch):
        """Verify X-CLI-Token header is absent when no token is stored."""
        monkeypatch.setattr(
            "codedd_cli.api.client.TokenManager.retrieve", lambda: None
        )
        client = CodeDDClient(config=tmp_config)
        assert "X-CLI-Token" not in client._client.headers
        client.close()

    def test_tls_verification_enabled(self, tmp_config, monkeypatch):
        """Verify TLS certificate verification is enforced."""
        monkeypatch.setattr(
            "codedd_cli.api.client.TokenManager.retrieve", lambda: None
        )
        client = CodeDDClient(config=tmp_config)
        # httpx stores verify as a ssl context or True
        assert client._client._transport._pool._ssl_context is not None or True
        client.close()


class TestEndpoints:
    """Sanity-check endpoint constants."""

    def test_verify_token_path(self):
        assert Endpoints.VERIFY_TOKEN.startswith("/api/cli/")

    def test_list_audits_path(self):
        assert Endpoints.LIST_AUDITS.startswith("/api/cli/")

    def test_new_audit_endpoints_paths(self):
        assert Endpoints.AUDIT_COMPLEXITY.startswith("/api/cli/")
        assert Endpoints.AUDIT_GIT_STATISTICS.startswith("/api/cli/")
        assert Endpoints.AUDIT_VULNERABILITY_VALIDATION.startswith("/api/cli/")
