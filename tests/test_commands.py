"""
Tests for CLI commands (auth, audits, config).
"""

import json

import pytest
from typer.testing import CliRunner

from codedd_cli.cli import app
from codedd_cli.config.settings import ConfigManager

runner = CliRunner()


class TestVersionFlag:
    """Test the --version flag on the root command."""

    def test_version_output(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "codedd-cli" in result.output


class TestConfigCommands:
    """Test 'codedd config' subcommands."""

    def test_config_show(self, tmp_config):
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert "api_url" in result.output

    def test_config_set_api_url(self, tmp_config):
        result = runner.invoke(app, ["config", "set", "api_url", "https://custom.example.com"])
        assert result.exit_code == 0
        assert "custom.example.com" in result.output

    def test_config_set_invalid_key(self, tmp_config):
        result = runner.invoke(app, ["config", "set", "nonexistent_key", "value"])
        assert result.exit_code == 1


class TestAuthCommands:
    """Test 'codedd auth' subcommands."""

    def test_auth_logout_when_not_logged_in(self, tmp_config):
        result = runner.invoke(app, ["auth", "logout"])
        assert result.exit_code == 0
        assert "not currently logged in" in result.output.lower() or "logged out" in result.output.lower()

    def test_auth_status_when_not_logged_in(self, tmp_config):
        result = runner.invoke(app, ["auth", "status"])
        assert result.exit_code == 0
        assert "not authenticated" in result.output.lower() or "codedd auth login" in result.output.lower()

    def test_auth_login_invalid_token_format(self, tmp_config):
        result = runner.invoke(app, ["auth", "login", "--token", "bad_token"])
        assert result.exit_code == 1
        assert "invalid" in result.output.lower()


class TestAuditsCommands:
    """Test 'codedd audits' subcommands."""

    def test_audits_list_requires_auth(self, tmp_config):
        """Audits list should fail gracefully when not logged in."""
        result = runner.invoke(app, ["audits", "list"])
        assert result.exit_code == 1
        assert "not authenticated" in result.output.lower() or "login" in result.output.lower()

    def test_audits_select_requires_auth(self, tmp_config):
        """Audits select should fail gracefully when not logged in."""
        result = runner.invoke(app, ["audits", "select"])
        assert result.exit_code == 1

    def test_audits_list_invalid_type(self, tmp_config, monkeypatch):
        """Invalid --type value should be rejected."""
        # Even if authed, bad type should error
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_testtoken123456789012345678901234567890",
        )
        cfg = ConfigManager()
        cfg.token_hint = "codedd_cli_...7890"
        cfg.account_uuid = "test-uuid"
        cfg.account_name = "Test"
        cfg.save()

        result = runner.invoke(app, ["audits", "list", "--type", "invalid"])
        assert result.exit_code == 1
