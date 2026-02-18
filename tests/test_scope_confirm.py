"""
Tests for the ``codedd scope confirm`` command.

Covers:
    - Payload building logic
    - Payload file writing (--show)
    - Confirm requires active audit and non-empty scope
"""

import json
import os
import subprocess

import pytest
from typer.testing import CliRunner

from codedd_cli.cli import app
from codedd_cli.commands.scope_cmd import _build_payload
from codedd_cli.config.settings import ConfigManager
from codedd_cli.scanner.file_walker import ScanResult, FileMetadata, FolderMetadata
from codedd_cli.utils.payload_inspector import write_payload_file

runner = CliRunner()


def _setup_authenticated_session(cfg: ConfigManager, audit_type: str = "group") -> None:
    cfg.account_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    cfg.account_name = "Test User"
    cfg.token_hint = "codedd_cli_...test"
    cfg.set_active_audit(
        audit_uuid="11111111-2222-3333-4444-555555555555",
        audit_type=audit_type,
        audit_name="Test Audit",
    )
    cfg.save()


def _create_git_repo(base_path, name: str = "test_repo"):
    repo = base_path / name
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, check=True)
    (repo / "main.py").write_text("def hello():\n    print('world')\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True)
    return repo


class TestBuildPayload:
    """Test the _build_payload helper."""

    def test_produces_correct_structure(self):
        scan = ScanResult(
            root_path="/fake/repo",
            repo_name="repo",
            branch="main",
            commit_hash="abc1234",
            files=[
                FileMetadata("src/main.py", "Source Code", 100, 20, True),
            ],
            folders=[
                FolderMetadata("src", 100, 1),
            ],
            total_files=1,
            total_lines_of_code=100,
            total_lines_of_doc=20,
        )

        payload = _build_payload("uuid-123", "group", [scan])

        assert payload["audit_uuid"] == "uuid-123"
        assert payload["audit_type"] == "group"
        assert len(payload["repositories"]) == 1

        repo = payload["repositories"][0]
        assert repo["repo_name"] == "repo"
        assert repo["total_files"] == 1
        assert repo["total_lines_of_code"] == 100
        assert len(repo["files"]) == 1
        assert repo["files"][0]["relative_path"] == "src/main.py"

    def test_multiple_repositories(self):
        scans = [
            ScanResult("/a", "repo-a", "main", "aaa", total_files=5, total_lines_of_code=500),
            ScanResult("/b", "repo-b", "dev", "bbb", total_files=10, total_lines_of_code=1000),
        ]

        payload = _build_payload("uuid-456", "group", scans)
        assert len(payload["repositories"]) == 2


class TestWritePayloadFile:
    """Test the general-purpose payload file writer."""

    def test_creates_file_with_json(self):
        payload = {"audit_uuid": "test", "repositories": []}
        filepath = write_payload_file(payload, command_label="My Audit")

        assert os.path.exists(filepath)
        assert filepath.endswith(".txt")

        with open(filepath, "r", encoding="utf-8") as fh:
            content = fh.read()

        # Should contain the header
        assert "CodeDD CLI" in content
        assert "metadata" in content.lower()
        # Should contain valid JSON somewhere
        assert '"audit_uuid"' in content
        assert '"test"' in content

        # Clean up
        os.unlink(filepath)

    def test_filename_sanitised(self):
        payload = {"audit_uuid": "x", "repositories": []}
        filepath = write_payload_file(payload, command_label="Test / Audit <Name>")

        filename = os.path.basename(filepath)
        assert "/" not in filename
        assert "<" not in filename

        os.unlink(filepath)

    def test_custom_context_note(self):
        payload = {"data": "value"}
        filepath = write_payload_file(
            payload,
            command_label="Custom",
            context_note="Custom transparency note here.",
        )

        with open(filepath, "r", encoding="utf-8") as fh:
            content = fh.read()

        assert "Custom transparency note here." in content
        os.unlink(filepath)


class TestConfirmCommand:
    """Test the CLI confirm command integration."""

    def test_confirm_fails_without_active_audit(self, tmp_config, monkeypatch):
        tmp_config.account_uuid = "test"
        tmp_config.account_name = "Test"
        tmp_config.token_hint = "codedd_cli_...test"
        tmp_config.save()
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        result = runner.invoke(app, ["scope", "confirm"])
        assert result.exit_code == 1

    def test_confirm_fails_with_empty_scope(self, tmp_config, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config)

        result = runner.invoke(app, ["scope", "confirm"])
        assert result.exit_code == 1
        assert "no directories" in result.output.lower() or "scope add" in result.output.lower()

    def test_confirm_scans_and_asks_confirmation(self, tmp_config, tmp_path, monkeypatch):
        """With a valid scope, confirm should scan and then ask for confirmation.
        We answer 'n' to avoid actually calling the API."""
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config)

        # Add a real repo to scope
        repo = _create_git_repo(tmp_path)
        runner.invoke(app, ["scope", "add", str(repo)])

        # Run confirm but answer 'n' to cancel
        result = runner.invoke(app, ["scope", "confirm"], input="n\n")
        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()
        # Should show scan summary before asking
        assert "lines of code" in result.output.lower() or "loc" in result.output.lower()
