"""Data model for a validated local directory added to an audit scope."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class LocalDirectory:
    """
    Represents a validated local Git repository directory.

    Attributes:
        path:        Absolute path to the directory.
        repo_name:   Short name derived from the directory basename.
        branch:      Currently checked-out branch (or HEAD ref).
        commit_hash: Short hash of the current HEAD commit.
        is_valid:    Whether all validation checks passed.
        error:       Validation error message (empty when valid).
    """

    path: str
    repo_name: str = ""
    branch: str = ""
    commit_hash: str = ""
    is_valid: bool = False
    error: str = ""
