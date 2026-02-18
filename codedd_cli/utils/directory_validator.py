"""
Local directory validation for audit scope.

Validates that a user-provided path:
    1. Exists on the filesystem
    2. Is a directory (not a file)
    3. Is readable / accessible by the current user
    4. Is the root of a Git repository (contains ``.git/``)
    5. Has at least one commit (valid HEAD)

Uses the **default branch** (main or master) for the audit, not the current
checkout. Fetches the default branch from origin when a remote exists so
the scope is tied to the canonical branch state.
"""

import os
import subprocess
from pathlib import Path
from typing import Tuple

from codedd_cli.models.local_directory import LocalDirectory

# Default branch names to try, in order of preference.
DEFAULT_BRANCH_ORDER = ("main", "master")


def validate_directory(raw_path: str) -> LocalDirectory:
    """
    Run all validation checks on *raw_path* and return a ``LocalDirectory``.

    The returned object's ``is_valid`` flag indicates whether all checks
    passed.  When ``is_valid`` is False, the ``error`` field explains why.

    Args:
        raw_path: A path string as entered by the user.  May be relative;
                  it is resolved to an absolute path internally.

    Returns:
        A populated ``LocalDirectory`` instance.
    """
    # Normalise and resolve to an absolute path
    try:
        resolved = Path(raw_path).expanduser().resolve()
        abs_path = str(resolved)
    except (OSError, ValueError) as exc:
        return LocalDirectory(
            path=raw_path,
            is_valid=False,
            error=f"Invalid path: {exc}",
        )

    # 1. Existence check
    if not resolved.exists():
        return LocalDirectory(
            path=abs_path,
            is_valid=False,
            error="Directory does not exist",
        )

    # 2. Must be a directory (not a file or symlink to file)
    if not resolved.is_dir():
        return LocalDirectory(
            path=abs_path,
            is_valid=False,
            error="Path is not a directory",
        )

    # 3. Accessibility — try listing the directory contents
    if not os.access(abs_path, os.R_OK):
        return LocalDirectory(
            path=abs_path,
            is_valid=False,
            error="Directory is not readable (permission denied)",
        )

    # 4. Must be a Git repository root
    git_dir = resolved / ".git"
    if not git_dir.exists():
        return LocalDirectory(
            path=abs_path,
            repo_name=resolved.name,
            is_valid=False,
            error="Not a Git repository (no .git directory found)",
        )

    # 5. Use default branch (main/master) and its commit for the audit
    branch, commit_hash, git_error = _default_branch_metadata(abs_path)
    if git_error:
        return LocalDirectory(
            path=abs_path,
            repo_name=resolved.name,
            is_valid=False,
            error=git_error,
        )

    return LocalDirectory(
        path=abs_path,
        repo_name=resolved.name,
        branch=branch,
        commit_hash=commit_hash,
        is_valid=True,
        error="",
    )


def _run_git(repo_path: str, args: list[str], timeout: int = 15) -> Tuple[int, str, str]:
    """Run a git command; returns (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return -1, "", "Git is not installed or not in PATH"
    except subprocess.TimeoutExpired:
        return -1, "", "Git command timed out"
    except Exception as exc:
        return -1, "", str(exc)


def _get_default_branch_name(repo_path: str) -> str:
    """
    Determine the default branch: origin/HEAD, then local main, then local master.

    Returns the branch name (e.g. "main") or empty string if none found.
    """
    # Prefer remote default (e.g. origin/HEAD -> origin/main)
    code, out, _ = _run_git(repo_path, ["rev-parse", "--abbrev-ref", "origin/HEAD"], timeout=5)
    if code == 0 and out and out != "origin/HEAD":
        # "origin/HEAD" -> "origin/main" -> we want "main"
        if out.startswith("origin/"):
            return out[7:]
        return out

    # Fallback: which of main/master exists (local or remote)
    for branch in DEFAULT_BRANCH_ORDER:
        code, _, _ = _run_git(repo_path, ["rev-parse", "--verify", branch], timeout=5)
        if code == 0:
            return branch
        code, _, _ = _run_git(repo_path, ["rev-parse", "--verify", f"origin/{branch}"], timeout=5)
        if code == 0:
            return branch
    return ""


def _default_branch_metadata(repo_path: str) -> Tuple[str, str, str]:
    """
    Resolve the default branch (main/master), fetch it from origin if possible,
    and return its name and short commit hash for use in the audit scope.

    Args:
        repo_path: Absolute path to the repository root.

    Returns:
        Tuple of (branch_name, short_commit_hash, error_message).
        On success ``error_message`` is empty.
    """
    branch_name = _get_default_branch_name(repo_path)
    if not branch_name:
        # Last resort: use current HEAD so we don't fail local-only repos
        code, head_branch, err = _run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
        if code != 0:
            return "", "", f"Git error: no main/master branch and {err or 'unable to read HEAD'}"
        branch_name = head_branch

    # Fetch the default branch from origin (best-effort; ignore failures for offline/local repos)
    _run_git(repo_path, ["fetch", "origin", branch_name], timeout=30)

    # Resolve commit: prefer origin/<branch>, then local <branch>
    for ref in (f"origin/{branch_name}", branch_name):
        code, commit, err = _run_git(repo_path, ["rev-parse", "--short", ref])
        if code == 0 and commit:
            return branch_name, commit, ""

    return branch_name, "", f"Git error: could not resolve commit for branch '{branch_name}'"
