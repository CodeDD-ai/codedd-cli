"""
Tests for the ``codedd scope`` commands.

Covers:
    - scope list (empty, with entries)
    - scope add (valid, invalid, duplicate, single-audit limit)
    - scope remove (valid index, invalid index)
    - scope clear
    - scope status
    - scope sync (diff detection, dirty marking, in-sync)
    - require_active_audit guard
    - _compute_diff helper
    - config sync state helpers
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codedd_cli.cli import app
from codedd_cli.commands.scope_cmd import _compute_diff
from codedd_cli.config.settings import ConfigManager

runner = CliRunner()


def _setup_authenticated_session(tmp_config: ConfigManager, audit_type: str = "group") -> None:
    """Helper: write a fake authenticated session and active audit to config."""
    tmp_config.account_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    tmp_config.account_name = "Test User"
    tmp_config.token_hint = "codedd_cli_...test"
    tmp_config.set_active_audit(
        audit_uuid="11111111-2222-3333-4444-555555555555",
        audit_type=audit_type,
        audit_name="Test Audit",
    )
    tmp_config.save()


def _create_git_repo(base_path, name: str = "test_repo"):
    """Helper: create a minimal git repo with one commit and return its path."""
    repo = base_path / name
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo), capture_output=True, check=True,
    )
    (repo / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), capture_output=True, check=True,
    )
    return repo


class TestScopeRequiresActiveAudit:
    """Scope commands should fail when no audit is selected."""

    def test_scope_list_without_active_audit(self, tmp_config, monkeypatch):
        # Authenticated but no active audit
        tmp_config.account_uuid = "test"
        tmp_config.account_name = "Test"
        tmp_config.token_hint = "codedd_cli_...test"
        tmp_config.save()
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        result = runner.invoke(app, ["scope", "list"])
        assert result.exit_code == 1
        assert "no active audit" in result.output.lower() or "select" in result.output.lower()


class TestScopeList:
    """Test ``codedd scope list``."""

    def test_empty_scope(self, tmp_config, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config)
        result = runner.invoke(app, ["scope", "list"])
        assert result.exit_code == 0
        assert "no directories" in result.output.lower() or "scope add" in result.output.lower()


class TestScopeAdd:
    """Test ``codedd scope add``."""

    def test_add_valid_git_repo(self, tmp_config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config)
        repo = _create_git_repo(tmp_path)

        result = runner.invoke(app, ["scope", "add", str(repo)])
        assert result.exit_code == 0
        assert "added" in result.output.lower()

        # Re-read config from disk (the CLI command wrote via its own ConfigManager)
        reloaded = ConfigManager()
        dirs = reloaded.scope_directories
        assert len(dirs) == 1
        assert dirs[0]["repo_name"] == "test_repo"

    def test_add_nonexistent_path(self, tmp_config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config)

        result = runner.invoke(app, ["scope", "add", str(tmp_path / "no_such_dir")])
        assert result.exit_code == 0  # command succeeds but prints error for the path
        assert "does not exist" in result.output.lower()
        assert len(ConfigManager().scope_directories) == 0

    def test_add_non_git_directory(self, tmp_config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config)
        plain_dir = tmp_path / "not_a_repo"
        plain_dir.mkdir()

        result = runner.invoke(app, ["scope", "add", str(plain_dir)])
        assert result.exit_code == 0
        assert "not a git repository" in result.output.lower()
        assert len(ConfigManager().scope_directories) == 0

    def test_add_duplicate_is_skipped(self, tmp_config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config)
        repo = _create_git_repo(tmp_path)

        runner.invoke(app, ["scope", "add", str(repo)])
        result = runner.invoke(app, ["scope", "add", str(repo)])
        assert "skipped" in result.output.lower() or "already" in result.output.lower()
        assert len(ConfigManager().scope_directories) == 1

    def test_same_path_can_be_used_for_different_audits(self, tmp_config, tmp_path, monkeypatch):
        """The same directory can be in scope for audit A and audit B separately."""
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config, audit_type="group")
        repo = _create_git_repo(tmp_path, "shared_repo")

        # Add to first audit
        runner.invoke(app, ["scope", "add", str(repo)])
        assert len(ConfigManager().scope_directories) == 1

        # Switch to a different audit (new active audit)
        cfg = ConfigManager()
        cfg.set_active_audit(
            audit_uuid="99999999-aaaa-bbbb-cccc-dddddddddddd",
            audit_type="group",
            audit_name="Other Audit",
        )

        # Same path should be addable for this audit (not "already in scope")
        result = runner.invoke(app, ["scope", "add", str(repo)])
        assert result.exit_code == 0
        assert "added" in result.output.lower()
        assert len(ConfigManager().scope_directories) == 1  # one for current (Other) audit

        # Switch back to first audit: should still have 1 directory
        cfg2 = ConfigManager()
        cfg2.set_active_audit(
            audit_uuid="11111111-2222-3333-4444-555555555555",
            audit_type="group",
            audit_name="Test Audit",
        )
        assert len(ConfigManager().scope_directories) == 1

    def test_add_multiple_repos_group_audit(self, tmp_config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config, audit_type="group")
        repo1 = _create_git_repo(tmp_path, "repo_one")
        repo2 = _create_git_repo(tmp_path, "repo_two")

        result = runner.invoke(app, ["scope", "add", str(repo1), str(repo2)])
        assert result.exit_code == 0
        assert len(ConfigManager().scope_directories) == 2

    def test_single_audit_blocks_second_directory(self, tmp_config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config, audit_type="single")
        repo1 = _create_git_repo(tmp_path, "repo_a")
        repo2 = _create_git_repo(tmp_path, "repo_b")

        runner.invoke(app, ["scope", "add", str(repo1)])
        assert len(ConfigManager().scope_directories) == 1

        result = runner.invoke(app, ["scope", "add", str(repo2)])
        assert "exactly" in result.output.lower() or "single" in result.output.lower()
        assert len(ConfigManager().scope_directories) == 1


class TestScopeRemove:
    """Test ``codedd scope remove``."""

    def test_remove_by_number(self, tmp_config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config)
        repo = _create_git_repo(tmp_path)
        runner.invoke(app, ["scope", "add", str(repo)])
        assert len(ConfigManager().scope_directories) == 1

        result = runner.invoke(app, ["scope", "remove", "1"])
        assert result.exit_code == 0
        assert "removed" in result.output.lower()
        assert len(ConfigManager().scope_directories) == 0

    def test_remove_invalid_number(self, tmp_config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config)
        repo = _create_git_repo(tmp_path)
        runner.invoke(app, ["scope", "add", str(repo)])

        result = runner.invoke(app, ["scope", "remove", "99"])
        assert result.exit_code == 1


class TestScopeClear:
    """Test ``codedd scope clear``."""

    def test_clear_with_confirmation(self, tmp_config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config)
        repo = _create_git_repo(tmp_path)
        runner.invoke(app, ["scope", "add", str(repo)])

        result = runner.invoke(app, ["scope", "clear"], input="y\n")
        assert result.exit_code == 0
        assert "cleared" in result.output.lower()
        assert len(ConfigManager().scope_directories) == 0


class TestScopeStatus:
    """Test ``codedd scope status``."""

    def test_status_empty(self, tmp_config, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config)

        result = runner.invoke(app, ["scope", "status"])
        assert result.exit_code == 0
        assert "no directories" in result.output.lower() or "scope add" in result.output.lower()

    def test_status_with_valid_repo(self, tmp_config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config)
        repo = _create_git_repo(tmp_path)
        runner.invoke(app, ["scope", "add", str(repo)])

        result = runner.invoke(app, ["scope", "status"])
        assert result.exit_code == 0
        assert "ready" in result.output.lower() or "valid" in result.output.lower()


# =========================================================================
# Diff computation tests
# =========================================================================

class TestComputeDiff:
    """Unit tests for ``_compute_diff``."""

    def test_no_changes(self):
        local = {
            "src/main.py": {"file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10},
            "README.md": {"file_type": "Markdown", "lines_of_code": 5, "lines_of_doc": 0},
        }
        remote = {
            "src/main.py": {"file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10, "selected_for_audit": True},
            "README.md": {"file_type": "Markdown", "lines_of_code": 5, "lines_of_doc": 0, "selected_for_audit": True},
        }
        diff = _compute_diff(local, remote)
        assert diff["added"] == []
        assert diff["removed"] == []
        assert diff["changed"] == []

    def test_detects_added_files(self):
        local = {
            "src/main.py": {"file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10},
            "src/new.py": {"file_type": "Python", "lines_of_code": 50, "lines_of_doc": 5},
        }
        remote = {
            "src/main.py": {"file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10, "selected_for_audit": True},
        }
        diff = _compute_diff(local, remote)
        assert len(diff["added"]) == 1
        assert diff["added"][0]["path"] == "src/new.py"
        assert diff["added"][0]["lines_of_code"] == 50
        assert diff["removed"] == []
        assert diff["changed"] == []

    def test_detects_removed_files(self):
        local = {
            "src/main.py": {"file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10},
        }
        remote = {
            "src/main.py": {"file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10, "selected_for_audit": True},
            "src/old.py": {"file_type": "Python", "lines_of_code": 30, "lines_of_doc": 2, "selected_for_audit": True},
        }
        diff = _compute_diff(local, remote)
        assert diff["added"] == []
        assert len(diff["removed"]) == 1
        assert diff["removed"][0]["path"] == "src/old.py"
        assert diff["changed"] == []

    def test_detects_changed_loc(self):
        local = {
            "src/main.py": {"file_type": "Python", "lines_of_code": 150, "lines_of_doc": 10},
        }
        remote = {
            "src/main.py": {"file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10, "selected_for_audit": True},
        }
        diff = _compute_diff(local, remote)
        assert diff["added"] == []
        assert diff["removed"] == []
        assert len(diff["changed"]) == 1
        assert diff["changed"][0]["old_loc"] == 100
        assert diff["changed"][0]["new_loc"] == 150

    def test_detects_changed_doc_lines(self):
        local = {
            "src/main.py": {"file_type": "Python", "lines_of_code": 100, "lines_of_doc": 20},
        }
        remote = {
            "src/main.py": {"file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10, "selected_for_audit": True},
        }
        diff = _compute_diff(local, remote)
        assert len(diff["changed"]) == 1

    def test_combined_diff(self):
        local = {
            "src/main.py": {"file_type": "Python", "lines_of_code": 150, "lines_of_doc": 10},
            "src/new.py": {"file_type": "Python", "lines_of_code": 50, "lines_of_doc": 0},
        }
        remote = {
            "src/main.py": {"file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10, "selected_for_audit": True},
            "src/old.py": {"file_type": "Python", "lines_of_code": 30, "lines_of_doc": 0, "selected_for_audit": True},
        }
        diff = _compute_diff(local, remote)
        assert len(diff["added"]) == 1
        assert len(diff["removed"]) == 1
        assert len(diff["changed"]) == 1


# =========================================================================
# Config sync state tests
# =========================================================================

class TestConfigSyncState:
    """Test the ``confirmed`` / ``needs_reconfirm`` / ``last_sync`` config fields."""

    def test_add_directory_starts_unconfirmed(self, tmp_config, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        _setup_authenticated_session(tmp_config)
        repo = _create_git_repo(tmp_path)
        runner.invoke(app, ["scope", "add", str(repo)])

        cfg = ConfigManager()
        dirs = cfg.scope_directories
        assert len(dirs) == 1
        assert dirs[0]["confirmed"] is False
        assert dirs[0]["needs_reconfirm"] is False
        assert dirs[0]["last_sync"] == ""

    def test_mark_scope_confirmed(self, tmp_config):
        _setup_authenticated_session(tmp_config)
        # Manually add a scope entry
        tmp_config._data.setdefault("scope_entries", []).append({
            "audit_uuid": "11111111-2222-3333-4444-555555555555",
            "path": "/tmp/test_repo",
            "repo_name": "test_repo",
            "branch": "main",
            "commit_hash": "abc1234",
            "confirmed": False,
            "needs_reconfirm": False,
            "last_sync": "",
        })
        tmp_config.save()

        tmp_config.mark_scope_confirmed()
        reloaded = ConfigManager()
        dirs = reloaded.scope_directories
        assert dirs[0]["confirmed"] is True
        assert dirs[0]["needs_reconfirm"] is False

    def test_mark_scope_needs_reconfirm(self, tmp_config):
        _setup_authenticated_session(tmp_config)
        tmp_config._data.setdefault("scope_entries", []).append({
            "audit_uuid": "11111111-2222-3333-4444-555555555555",
            "path": "/tmp/test_repo",
            "repo_name": "test_repo",
            "branch": "main",
            "commit_hash": "abc1234",
            "confirmed": True,
            "needs_reconfirm": False,
            "last_sync": "",
        })
        tmp_config.save()

        tmp_config.mark_scope_needs_reconfirm(last_sync_iso="2026-02-13T12:00:00+00:00")
        reloaded = ConfigManager()
        dirs = reloaded.scope_directories
        assert dirs[0]["needs_reconfirm"] is True
        assert dirs[0]["last_sync"] == "2026-02-13T12:00:00+00:00"

    def test_update_scope_last_sync(self, tmp_config):
        _setup_authenticated_session(tmp_config)
        tmp_config._data.setdefault("scope_entries", []).append({
            "audit_uuid": "11111111-2222-3333-4444-555555555555",
            "path": "/tmp/test_repo",
            "repo_name": "test_repo",
            "branch": "main",
            "commit_hash": "abc1234",
            "confirmed": True,
            "needs_reconfirm": False,
            "last_sync": "",
        })
        tmp_config.save()

        tmp_config.update_scope_last_sync("2026-02-13T14:00:00+00:00")
        reloaded = ConfigManager()
        dirs = reloaded.scope_directories
        assert dirs[0]["last_sync"] == "2026-02-13T14:00:00+00:00"


# =========================================================================
# Scope sync command tests
# =========================================================================

def _mock_scan_result(repo_name: str, files: list[dict]):
    """Create a mock ScanResult-like object with the given files."""
    result = MagicMock()
    result.repo_name = repo_name
    result.total_files = len(files)
    result.total_lines_of_code = sum(f.get("lines_of_code", 0) for f in files)
    result.total_lines_of_doc = sum(f.get("lines_of_doc", 0) for f in files)
    result.errors = []
    result.branch = "main"
    result.commit_hash = "abc1234"

    file_mocks = []
    for f in files:
        fm = MagicMock()
        fm.relative_path = f["relative_path"]
        fm.file_type = f.get("file_type", "Python")
        fm.lines_of_code = f.get("lines_of_code", 0)
        fm.lines_of_doc = f.get("lines_of_doc", 0)
        fm.selected_for_audit = f.get("selected_for_audit", True)
        file_mocks.append(fm)
    result.files = file_mocks

    result.to_dict.return_value = {
        "repo_name": repo_name,
        "files": files,
        "folders": [],
        "total_files": result.total_files,
        "total_lines_of_code": result.total_lines_of_code,
        "total_lines_of_doc": result.total_lines_of_doc,
    }
    return result


class TestScopeSync:
    """Test ``codedd scope sync`` with mocked API and scanner."""

    def _setup_scope_entry(self, tmp_config, repo_name="test_repo", path="/tmp/test_repo"):
        """Add a confirmed scope entry for the active audit."""
        _setup_authenticated_session(tmp_config)
        tmp_config._data.setdefault("scope_entries", []).append({
            "audit_uuid": "11111111-2222-3333-4444-555555555555",
            "path": path,
            "repo_name": repo_name,
            "branch": "main",
            "commit_hash": "abc1234",
            "confirmed": True,
            "needs_reconfirm": False,
            "last_sync": "",
        })
        tmp_config.save()

    def test_sync_no_changes(self, tmp_config, monkeypatch):
        """When local and remote match, 'in sync' message is shown."""
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        self._setup_scope_entry(tmp_config)

        # Mock API response
        remote_response = MagicMock()
        remote_response.status_code = 200
        remote_response.json.return_value = {
            "status": "success",
            "group_audit_uuid": "11111111-2222-3333-4444-555555555555",
            "sub_audits": [
                {
                    "audit_uuid": "sub-1",
                    "repo_name": "test_repo",
                    "audit_status": "Scope registered",
                    "is_cli": True,
                    "files": [
                        {"relative_path": "src/main.py", "file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10, "selected_for_audit": True},
                    ],
                    "total_files": 1,
                    "total_lines_of_code": 100,
                }
            ],
        }

        # Mock scanner with matching files
        scan_result = _mock_scan_result("test_repo", [
            {"relative_path": "src/main.py", "file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10},
        ])

        with patch("codedd_cli.commands.scope_cmd.CodeDDClient") as mock_client_cls, \
             patch("codedd_cli.commands.scope_cmd.scan_repository", return_value=scan_result):
            mock_client = MagicMock()
            mock_client.get.return_value = remote_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = runner.invoke(app, ["scope", "sync"])

        assert result.exit_code == 0
        assert "in sync" in result.output.lower()

    def test_sync_detects_added_files(self, tmp_config, monkeypatch):
        """New local files should appear as 'added' in the diff."""
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        self._setup_scope_entry(tmp_config)

        remote_response = MagicMock()
        remote_response.status_code = 200
        remote_response.json.return_value = {
            "status": "success",
            "group_audit_uuid": "11111111-2222-3333-4444-555555555555",
            "sub_audits": [
                {
                    "audit_uuid": "sub-1",
                    "repo_name": "test_repo",
                    "audit_status": "Scope registered",
                    "is_cli": True,
                    "files": [
                        {"relative_path": "src/main.py", "file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10, "selected_for_audit": True},
                    ],
                    "total_files": 1,
                    "total_lines_of_code": 100,
                }
            ],
        }

        # Local scan has an extra file
        scan_result = _mock_scan_result("test_repo", [
            {"relative_path": "src/main.py", "file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10},
            {"relative_path": "src/new_feature.py", "file_type": "Python", "lines_of_code": 85, "lines_of_doc": 5},
        ])

        with patch("codedd_cli.commands.scope_cmd.CodeDDClient") as mock_client_cls, \
             patch("codedd_cli.commands.scope_cmd.scan_repository", return_value=scan_result):
            mock_client = MagicMock()
            mock_client.get.return_value = remote_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = runner.invoke(app, ["scope", "sync"], input="n\n")

        assert result.exit_code == 0
        assert "added" in result.output.lower()
        assert "new_feature.py" in result.output

    def test_sync_detects_removed_files(self, tmp_config, monkeypatch):
        """Files present remotely but not locally should appear as 'removed'."""
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        self._setup_scope_entry(tmp_config)

        remote_response = MagicMock()
        remote_response.status_code = 200
        remote_response.json.return_value = {
            "status": "success",
            "group_audit_uuid": "11111111-2222-3333-4444-555555555555",
            "sub_audits": [
                {
                    "audit_uuid": "sub-1",
                    "repo_name": "test_repo",
                    "audit_status": "Scope registered",
                    "is_cli": True,
                    "files": [
                        {"relative_path": "src/main.py", "file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10, "selected_for_audit": True},
                        {"relative_path": "src/old_module.py", "file_type": "Python", "lines_of_code": 40, "lines_of_doc": 5, "selected_for_audit": True},
                    ],
                    "total_files": 2,
                    "total_lines_of_code": 140,
                }
            ],
        }

        # Local scan is missing old_module.py
        scan_result = _mock_scan_result("test_repo", [
            {"relative_path": "src/main.py", "file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10},
        ])

        with patch("codedd_cli.commands.scope_cmd.CodeDDClient") as mock_client_cls, \
             patch("codedd_cli.commands.scope_cmd.scan_repository", return_value=scan_result):
            mock_client = MagicMock()
            mock_client.get.return_value = remote_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = runner.invoke(app, ["scope", "sync"], input="n\n")

        assert result.exit_code == 0
        assert "removed" in result.output.lower()
        assert "old_module.py" in result.output

    def test_sync_detects_changed_loc(self, tmp_config, monkeypatch):
        """LoC changes should appear as 'changed' in the diff."""
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        self._setup_scope_entry(tmp_config)

        remote_response = MagicMock()
        remote_response.status_code = 200
        remote_response.json.return_value = {
            "status": "success",
            "group_audit_uuid": "11111111-2222-3333-4444-555555555555",
            "sub_audits": [
                {
                    "audit_uuid": "sub-1",
                    "repo_name": "test_repo",
                    "audit_status": "Scope registered",
                    "is_cli": True,
                    "files": [
                        {"relative_path": "src/main.py", "file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10, "selected_for_audit": True},
                    ],
                    "total_files": 1,
                    "total_lines_of_code": 100,
                }
            ],
        }

        # LoC changed from 100 to 135
        scan_result = _mock_scan_result("test_repo", [
            {"relative_path": "src/main.py", "file_type": "Python", "lines_of_code": 135, "lines_of_doc": 10},
        ])

        with patch("codedd_cli.commands.scope_cmd.CodeDDClient") as mock_client_cls, \
             patch("codedd_cli.commands.scope_cmd.scan_repository", return_value=scan_result):
            mock_client = MagicMock()
            mock_client.get.return_value = remote_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = runner.invoke(app, ["scope", "sync"], input="n\n")

        assert result.exit_code == 0
        assert "changed" in result.output.lower()

    def test_sync_marks_needs_reconfirm(self, tmp_config, monkeypatch):
        """After detecting changes and declining re-confirm, scope should be marked dirty."""
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        self._setup_scope_entry(tmp_config)

        remote_response = MagicMock()
        remote_response.status_code = 200
        remote_response.json.return_value = {
            "status": "success",
            "group_audit_uuid": "11111111-2222-3333-4444-555555555555",
            "sub_audits": [
                {
                    "audit_uuid": "sub-1",
                    "repo_name": "test_repo",
                    "audit_status": "Scope registered",
                    "is_cli": True,
                    "files": [
                        {"relative_path": "src/main.py", "file_type": "Python", "lines_of_code": 100, "lines_of_doc": 10, "selected_for_audit": True},
                    ],
                    "total_files": 1,
                    "total_lines_of_code": 100,
                }
            ],
        }

        scan_result = _mock_scan_result("test_repo", [
            {"relative_path": "src/main.py", "file_type": "Python", "lines_of_code": 200, "lines_of_doc": 10},
        ])

        with patch("codedd_cli.commands.scope_cmd.CodeDDClient") as mock_client_cls, \
             patch("codedd_cli.commands.scope_cmd.scan_repository", return_value=scan_result):
            mock_client = MagicMock()
            mock_client.get.return_value = remote_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = runner.invoke(app, ["scope", "sync"], input="n\n")

        assert result.exit_code == 0

        # Re-read config and check needs_reconfirm is set
        reloaded = ConfigManager()
        dirs = reloaded.scope_directories
        assert len(dirs) == 1
        assert dirs[0]["needs_reconfirm"] is True
        assert dirs[0]["last_sync"] != ""

    def test_sync_remote_repo_deleted(self, tmp_config, monkeypatch):
        """Repos deleted from CodeDD should produce a warning."""
        monkeypatch.setattr(
            "codedd_cli.auth.token_manager.TokenManager.retrieve",
            lambda: "codedd_cli_faketokenfaketokenfaketokenfaketoken123456",
        )
        self._setup_scope_entry(tmp_config)

        # Server returns no sub-audits (repo was deleted via frontend)
        remote_response = MagicMock()
        remote_response.status_code = 200
        remote_response.json.return_value = {
            "status": "success",
            "group_audit_uuid": "11111111-2222-3333-4444-555555555555",
            "sub_audits": [],
        }

        with patch("codedd_cli.commands.scope_cmd.CodeDDClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.get.return_value = remote_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # Prompt asks to accept deletion (1) or restore (2); send "1" to match CodeDD
            result = runner.invoke(app, ["scope", "sync"], input="1\n")

        assert result.exit_code == 0
        # Should warn about not-yet-registered or deleted
        assert "not yet registered" in result.output.lower() or "deleted" in result.output.lower() or "confirm" in result.output.lower()
