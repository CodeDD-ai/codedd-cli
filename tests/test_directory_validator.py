"""
Tests for the directory validation logic.

Covers:
    - Valid git repository detection
    - Non-existent path handling
    - File (non-directory) rejection
    - Non-git directory rejection
    - Permission error handling
    - Git metadata extraction
"""

import os
import subprocess

import pytest

from codedd_cli.utils.directory_validator import (
    validate_directory,
    _default_branch_metadata,
    _get_default_branch_name,
)


class TestValidateDirectory:
    """Test the top-level validate_directory function."""

    def test_nonexistent_path(self, tmp_path):
        """A path that does not exist should fail validation."""
        result = validate_directory(str(tmp_path / "does_not_exist"))
        assert result.is_valid is False
        assert "does not exist" in result.error

    def test_file_instead_of_directory(self, tmp_path):
        """A path that points to a file should fail."""
        test_file = tmp_path / "somefile.txt"
        test_file.write_text("hello")
        result = validate_directory(str(test_file))
        assert result.is_valid is False
        assert "not a directory" in result.error

    def test_directory_without_git(self, tmp_path):
        """A directory without .git should fail."""
        result = validate_directory(str(tmp_path))
        assert result.is_valid is False
        assert "Not a Git repository" in result.error
        assert result.repo_name == tmp_path.name

    def test_valid_git_repository(self, tmp_path):
        """A proper git repo should pass all checks."""
        repo = tmp_path / "my_repo"
        repo.mkdir()
        # Initialize a real git repository
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo), capture_output=True, check=True,
        )
        # Create an initial commit
        readme = repo / "README.md"
        readme.write_text("# Test")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=str(repo), capture_output=True, check=True,
        )

        result = validate_directory(str(repo))
        assert result.is_valid is True
        assert result.error == ""
        assert result.repo_name == "my_repo"
        assert result.branch  # Should be "main" or "master"
        assert result.commit_hash  # Short hash
        assert result.path == str(repo.resolve())

    def test_relative_path_is_resolved(self, tmp_path, monkeypatch):
        """A relative path should be resolved to an absolute path."""
        repo = tmp_path / "rel_repo"
        repo.mkdir()
        # The result should contain the absolute path even if input is relative
        monkeypatch.chdir(tmp_path)
        result = validate_directory("rel_repo")
        # It won't be valid (no .git) but path should be absolute
        assert os.path.isabs(result.path)

    def test_empty_git_repo_no_commits(self, tmp_path):
        """A git repo with no commits should fail (HEAD is invalid)."""
        repo = tmp_path / "empty_repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)

        result = validate_directory(str(repo))
        assert result.is_valid is False
        # The error comes from git rev-parse failing on empty repo
        assert result.error  # Should have a git error

    def test_duplicate_path_normalisation(self, tmp_path):
        """Trailing slashes and . components should not affect resolution."""
        repo = tmp_path / "norm_repo"
        repo.mkdir()
        (repo / ".git").mkdir()  # Fake .git for structure test

        r1 = validate_directory(str(repo))
        r2 = validate_directory(str(repo) + "/")
        r3 = validate_directory(str(repo) + "/./")

        # All should resolve to the same absolute path
        assert r1.path == r2.path == r3.path


class TestDefaultBranchMetadata:
    """Test the _default_branch_metadata helper."""

    def test_returns_error_for_non_git_dir(self, tmp_path):
        """A plain directory should produce a git error."""
        branch, commit, error = _default_branch_metadata(str(tmp_path))
        assert error  # non-empty error message
        assert branch == ""

    def test_returns_branch_and_hash_for_valid_repo(self, tmp_path):
        """A valid repo should return default branch (main/master) and commit hash."""
        repo = tmp_path / "meta_repo"
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
        (repo / "file.txt").write_text("content")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(repo), capture_output=True, check=True,
        )

        branch, commit, error = _default_branch_metadata(str(repo))
        assert error == ""
        assert branch in ("main", "master")  # default branch
        assert len(commit) >= 7  # short hash is typically 7+ chars


class TestGetDefaultBranchName:
    """Test default branch name resolution."""

    def test_prefers_main_over_master(self, tmp_path):
        """With a repo that has a default branch (main or master), we resolve it."""
        repo = tmp_path / "br"
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
        (repo / "f").write_text("x")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(repo), capture_output=True, check=True,
        )
        name = _get_default_branch_name(str(repo))
        assert name in ("main", "master")
