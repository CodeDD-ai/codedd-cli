"""
Tests for the local repository scanner package.

Covers:
    - File classifier (get_file_type, should_exclude_file, should_exclude_directory)
    - Line counter (count_lines)
    - File walker (scan_repository)
"""

import os
import subprocess

import pytest

from codedd_cli.scanner.file_classifier import (
    get_file_type,
    should_exclude_directory,
    should_exclude_file,
)
from codedd_cli.scanner.line_counter import count_lines
from codedd_cli.scanner.file_walker import scan_repository, ScanResult


# ===================================================================
# File classifier tests
# ===================================================================

class TestGetFileType:
    """Test file type classification."""

    def test_python_source(self):
        assert get_file_type("main.py") == "Source Code"

    def test_javascript_source(self):
        assert get_file_type("app.js") == "Source Code"

    def test_typescript_source(self):
        assert get_file_type("component.tsx") == "Source Code"

    def test_configuration_file(self):
        assert get_file_type("Dockerfile") == "Configuration"

    def test_yaml_config(self):
        assert get_file_type("docker-compose.yml") == "Configuration"

    def test_markdown_documentation(self):
        assert get_file_type("README.md") == "Documentation"

    def test_binary_file(self):
        assert get_file_type("program.exe") == "Binary"

    def test_media_file(self):
        assert get_file_type("photo.png") == "Media"

    def test_archive_file(self):
        assert get_file_type("backup.zip") == "Archive"

    def test_unknown_extension(self):
        assert get_file_type("strange.xyzabc") == "Other"

    def test_dotfile_extensions(self):
        assert get_file_type(".gitignore") == "Configuration"

    def test_compound_extension(self):
        # .d.ts should be recognised as source code
        assert get_file_type("types.d.ts") == "Source Code"


class TestShouldExcludeFile:
    """Test file exclusion logic."""

    def test_git_internal_file(self):
        assert should_exclude_file("/repo/.git/config") is True

    def test_configuration_excluded(self):
        assert should_exclude_file("/repo/Dockerfile") is True

    def test_python_source_not_excluded(self):
        assert should_exclude_file("/repo/main.py") is False

    def test_documentation_excluded(self):
        assert should_exclude_file("/repo/README.md") is True

    def test_binary_excluded(self):
        assert should_exclude_file("/repo/app.exe") is True


class TestShouldExcludeDirectory:
    """Test directory exclusion."""

    def test_git_excluded(self):
        assert should_exclude_directory(".git") is True

    def test_node_modules_excluded(self):
        assert should_exclude_directory("node_modules") is True

    def test_pycache_excluded(self):
        assert should_exclude_directory("__pycache__") is True

    def test_venv_excluded(self):
        assert should_exclude_directory("venv") is True

    def test_src_not_excluded(self):
        assert should_exclude_directory("src") is False

    def test_lib_not_excluded(self):
        assert should_exclude_directory("lib") is False


# ===================================================================
# Line counter tests
# ===================================================================

class TestCountLines:
    """Test the line counter."""

    def test_count_python_file(self, tmp_path):
        f = tmp_path / "example.py"
        f.write_text("# comment\ndef hello():\n    print('hi')\n\n")
        loc, doc = count_lines(str(f))
        assert loc >= 2  # At least the non-comment, non-empty lines
        assert doc >= 1  # The comment

    def test_count_empty_file(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_text("")
        loc, doc = count_lines(str(f))
        assert loc == 0
        assert doc == 0

    def test_excluded_file_returns_zero(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"\x89PNG\r\n")
        loc, doc = count_lines(str(f))
        assert loc == 0
        assert doc == 0

    def test_configuration_excluded(self, tmp_path):
        f = tmp_path / "Dockerfile"
        f.write_text("FROM python:3.12\nRUN pip install flask\n")
        loc, doc = count_lines(str(f))
        # Dockerfile is in CONFIGURATION_FILES → excluded from LoC
        assert loc == 0
        assert doc == 0

    def test_javascript_file(self, tmp_path):
        f = tmp_path / "app.js"
        f.write_text("// main entry\nconst x = 1;\nconsole.log(x);\n")
        loc, doc = count_lines(str(f))
        assert loc >= 2
        assert doc >= 1


# ===================================================================
# File walker / scan_repository tests
# ===================================================================

def _create_test_repo(base, name="scan_repo"):
    """Create a small git repository for scan tests."""
    repo = base / name
    repo.mkdir()

    # Create some files
    (repo / "main.py").write_text("def hello():\n    print('world')\n")
    (repo / "README.md").write_text("# Readme\nSome docs\n")
    src = repo / "src"
    src.mkdir()
    (src / "util.py").write_text("# utility\ndef add(a, b):\n    return a + b\n")
    (src / "style.css").write_text("body {\n  color: red;\n}\n")

    # Excluded directories
    nm = repo / "node_modules"
    nm.mkdir()
    (nm / "dep.js").write_text("module.exports = {};")

    # Init git
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, check=True)

    return repo


class TestScanRepository:
    """Test the full scan_repository function."""

    def test_scan_basic_repo(self, tmp_path):
        repo = _create_test_repo(tmp_path)
        result = scan_repository(str(repo))

        assert isinstance(result, ScanResult)
        assert result.repo_name == "scan_repo"
        assert result.branch  # main or master
        assert result.commit_hash
        assert result.total_files > 0
        assert result.total_lines_of_code > 0

    def test_node_modules_excluded(self, tmp_path):
        repo = _create_test_repo(tmp_path)
        result = scan_repository(str(repo))

        # No file from node_modules should appear
        for f in result.files:
            assert "node_modules" not in f.relative_path

    def test_files_have_relative_paths(self, tmp_path):
        repo = _create_test_repo(tmp_path)
        result = scan_repository(str(repo))

        for f in result.files:
            assert not os.path.isabs(f.relative_path)
            assert "\\" not in f.relative_path  # Forward slashes only

    def test_to_dict_serialisation(self, tmp_path):
        repo = _create_test_repo(tmp_path)
        result = scan_repository(str(repo))

        d = result.to_dict()
        assert "repo_name" in d
        assert "files" in d
        assert "folders" in d
        assert isinstance(d["files"], list)
        assert isinstance(d["folders"], list)

        # Verify no absolute paths in the file entries
        for entry in d["files"]:
            assert not os.path.isabs(entry["relative_path"])

    def test_progress_callback_invoked(self, tmp_path):
        repo = _create_test_repo(tmp_path)
        calls = []

        def cb(current, total, path):
            calls.append((current, total, path))

        scan_repository(str(repo), progress_callback=cb)
        assert len(calls) > 0
        # The last call's current should equal total
        assert calls[-1][0] == calls[-1][1]

    def test_folder_aggregation(self, tmp_path):
        repo = _create_test_repo(tmp_path)
        result = scan_repository(str(repo))

        # There should be at least a "src" folder
        folder_paths = [f.relative_path for f in result.folders]
        assert "src" in folder_paths

    def test_scan_result_no_errors(self, tmp_path):
        repo = _create_test_repo(tmp_path)
        result = scan_repository(str(repo))
        assert len(result.errors) == 0
