"""
Collect git statistics from a local repository for CLI-driven audits.

Produces the same structure as the server's GitStatisticsCollector so that
POST /api/cli/audit/git-statistics/ can persist data for dashboards, velocity
aggregates, and developer expertise. Used when the repo is local (git directory)
and the server has no clone (cli:// root_path).
"""

import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional


def _run_git(repo_path: str, cmd: list[str], timeout: int = 300) -> Optional[str]:
    """Run a git command in the repository; returns stdout or None on failure."""
    try:
        full_cmd = ["git", "-C", repo_path] + cmd
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _default_branch(repo_path: str) -> str:
    """Return default branch (main or master)."""
    branch = _run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch and branch != "HEAD":
        return branch
    for name in ("main", "master"):
        if _run_git(repo_path, ["rev-parse", "--verify", name]):
            return name
    return "main"


def collect_git_statistics(
    repo_path: str,
    repository_name: Optional[str] = None,
    repository_url: Optional[str] = None,
    on_debug: Optional[Callable[[str], None]] = None,
) -> Optional[dict[str, Any]]:
    """
    Collect git statistics from a local repository path.

    Returns a dict compatible with the server's GitStatistics model:
    commit_history, author_stats, merge_stats, branch_stats, meta_info,
    time_based_stats, release_stats, code_churn_stats, collaboration_stats.
    Missing or unsupported metrics are returned as empty dicts.

    Returns None if repo_path is not a git repo or collection fails.
    """
    repo_path = os.path.abspath(repo_path)
    git_dir = os.path.join(repo_path, ".git")
    if not os.path.isdir(git_dir):
        if on_debug:
            on_debug(f"Not a git repository: {repo_path}")
        return None

    def dbg(msg: str) -> None:
        if on_debug:
            on_debug(msg)

    default_branch = _default_branch(repo_path)
    dbg(f"Collecting git stats for branch {default_branch}")

    # Commit history with file changes (for velocity and developer expertise)
    commit_history = _collect_commit_history(repo_path, default_branch, dbg)
    if commit_history is None:
        commit_history = {"total_commits": 0, "commits": []}

    # Author statistics (counts per author)
    author_stats = _collect_author_stats(repo_path, default_branch, dbg)

    return {
        "commit_history": commit_history,
        "author_stats": author_stats or {},
        "merge_stats": {},
        "branch_stats": _collect_branch_stats(repo_path, dbg) or {},
        "meta_info": _collect_meta_info(repo_path, default_branch) or {},
        "time_based_stats": {},
        "release_stats": {},
        "code_churn_stats": {},
        "collaboration_stats": {},
        "repository_name": repository_name,
        "repository_url": repository_url,
    }


def _collect_commit_history(repo_path: str, default_branch: str, dbg: Callable[[str], None]) -> Optional[dict]:
    """Get commit history with file changes (same shape as server)."""
    # One commit per block; format: HASH|||AUTHOR|||DATE|||SUBJECT|||PARENTS
    fmt = "COMMIT_START\n%H|||%an <%ae>|||%aI|||%s|||%P"
    out = _run_git(
        repo_path,
        ["log", default_branch, "--numstat", "--date=iso-strict", f"--pretty=format:{fmt}"],
        timeout=120,
    )
    if not out:
        return {"total_commits": 0, "commits": []}

    blocks = [b for b in out.split("COMMIT_START\n") if b.strip()]
    commits = []
    for block in blocks:
        lines = block.strip().split("\n")
        if not lines:
            continue
        parts = lines[0].split("|||")
        if len(parts) < 4:
            continue
        hash_val = parts[0]
        author = parts[1]
        commit_date = parts[2]
        message = parts[3]
        file_changes = []
        for line in lines[1:]:
            if not line.strip():
                continue
            try:
                added, deleted, filename = line.split("\t", 2)
                added = int(added) if added != "-" else 0
                deleted = int(deleted) if deleted != "-" else 0
                file_changes.append({"filename": filename, "added": added, "deleted": deleted})
            except (ValueError, TypeError):
                continue
        commits.append({
            "hash": hash_val,
            "author": author,
            "date": commit_date,
            "message": message,
            "is_merge": len(parts) > 4 and bool(parts[4].strip()),
            "file_changes": file_changes,
        })

    return {"total_commits": len(commits), "commits": commits}


def _collect_author_stats(repo_path: str, default_branch: str, dbg: Callable[[str], None]) -> dict:
    """Get author commit counts (shortlog)."""
    out = _run_git(repo_path, ["shortlog", "-sne", "--no-merges", default_branch])
    if not out:
        return {}
    authors = {}
    for line in out.split("\n"):
        if not line.strip():
            continue
        try:
            count, author = line.strip().split("\t", 1)
            authors[author] = {"commits": int(count)}
        except (ValueError, TypeError):
            continue
    return {"authors": authors} if authors else {}


def _collect_branch_stats(repo_path: str, dbg: Callable[[str], None]) -> dict:
    """Basic branch list (local)."""
    out = _run_git(repo_path, ["branch", "--format=%(refname:short)"])
    if not out:
        return {}
    names = [n.strip() for n in out.split("\n") if n.strip()]
    return {"total_branches": len(names), "branches": [{"name": n} for n in names]}


def _collect_meta_info(repo_path: str, default_branch: str) -> dict:
    """Minimal repo meta."""
    first = _run_git(repo_path, ["log", "--reverse", "--format=%aI", default_branch, "-1"])
    latest = _run_git(repo_path, ["log", "--format=%aI", default_branch, "-1"])
    return {
        "first_commit_date": first,
        "latest_commit_date": latest,
        "default_branch": default_branch,
    }
