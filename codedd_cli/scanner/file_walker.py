"""
Local repository scanner.

Walks a Git repository directory, classifies each file, counts lines of code,
and produces a structured metadata payload ready for submission to the CodeDD API.

**No file contents are included** — only paths, types, and line counts.
"""

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from codedd_cli.scanner.file_classifier import (
    get_file_type,
    should_exclude_directory,
    should_exclude_file,
)
from codedd_cli.scanner.line_counter import count_lines

logger = logging.getLogger(__name__)


@dataclass
class FileMetadata:
    """Metadata for a single scanned file (no content)."""
    relative_path: str
    file_type: str
    lines_of_code: int
    lines_of_doc: int
    selected_for_audit: bool = True


@dataclass
class FolderMetadata:
    """Aggregated metadata for a folder."""
    relative_path: str
    lines_of_code: int = 0
    file_count: int = 0


@dataclass
class ScanResult:
    """Complete scan result for a single repository directory."""
    root_path: str
    repo_name: str
    branch: str
    commit_hash: str
    files: List[FileMetadata] = field(default_factory=list)
    folders: List[FolderMetadata] = field(default_factory=list)
    total_files: int = 0
    total_lines_of_code: int = 0
    total_lines_of_doc: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialise to a plain dict suitable for JSON encoding."""
        return {
            "root_path": self.root_path,
            "repo_name": self.repo_name,
            "branch": self.branch,
            "commit_hash": self.commit_hash,
            "total_files": self.total_files,
            "total_lines_of_code": self.total_lines_of_code,
            "total_lines_of_doc": self.total_lines_of_doc,
            "files": [
                {
                    "relative_path": f.relative_path,
                    "file_type": f.file_type,
                    "lines_of_code": f.lines_of_code,
                    "lines_of_doc": f.lines_of_doc,
                    "selected_for_audit": f.selected_for_audit,
                }
                for f in self.files
            ],
            "folders": [
                {
                    "relative_path": fd.relative_path,
                    "lines_of_code": fd.lines_of_code,
                    "file_count": fd.file_count,
                }
                for fd in self.folders
            ],
            "errors": self.errors,
        }


def _git_info(repo_path: str) -> Tuple[str, str]:
    """Return (branch, short_commit_hash) for a repo, or empty strings on error."""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        ).stdout.strip()

        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        ).stdout.strip()

        return branch, commit
    except Exception:
        return "", ""


def scan_repository(
    root_path: str,
    progress_callback: Optional[callable] = None,
) -> ScanResult:
    """
    Walk *root_path*, classify every file, and count lines of code.

    Args:
        root_path:         Absolute path to the repository root.
        progress_callback: Optional callable(current, total, file_path) invoked
                           for each file processed.  Useful for Rich progress bars.

    Returns:
        A ``ScanResult`` containing all file and folder metadata.
    """
    root = Path(root_path).resolve()
    repo_name = root.name
    branch, commit = _git_info(str(root))

    result = ScanResult(
        root_path=str(root),
        repo_name=repo_name,
        branch=branch,
        commit_hash=commit,
    )

    # Phase 1: collect all eligible file paths
    file_paths: List[str] = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        # Filter out excluded directories in-place so os.walk skips them
        dirnames[:] = [
            d for d in dirnames
            if not should_exclude_directory(d)
        ]
        for fname in filenames:
            full_path = os.path.join(dirpath, fname)
            # Skip symlinks
            if os.path.islink(full_path):
                continue
            file_paths.append(full_path)

    total_files = len(file_paths)
    folder_aggregates: Dict[str, FolderMetadata] = {}

    # Phase 2: classify and count
    for idx, full_path in enumerate(file_paths):
        try:
            rel_path = os.path.relpath(full_path, start=str(root)).replace("\\", "/")
            file_type = get_file_type(full_path)

            # Count LoC (returns (0, 0) for excluded files)
            loc, doc = count_lines(full_path)

            fm = FileMetadata(
                relative_path=rel_path,
                file_type=file_type,
                lines_of_code=loc,
                lines_of_doc=doc,
                selected_for_audit=(not should_exclude_file(full_path) and loc > 0),
            )
            result.files.append(fm)

            if fm.selected_for_audit:
                result.total_files += 1
                result.total_lines_of_code += loc
                result.total_lines_of_doc += doc

            # Aggregate into parent folder and ensure all ancestor folders exist.
            # The server-side import (import_file_list.py) discovers ALL directories
            # via os.scandir.  The CLI must produce the same set so the folder
            # hierarchy can be reconstructed on the server when building TypeDB
            # directory_content relations (folder → parent_folder → root).
            parent_rel = os.path.dirname(rel_path).replace("\\", "/")
            if parent_rel and parent_rel != ".":
                # Direct parent gets LoC / file_count
                if parent_rel not in folder_aggregates:
                    folder_aggregates[parent_rel] = FolderMetadata(relative_path=parent_rel)
                folder_aggregates[parent_rel].lines_of_code += loc
                folder_aggregates[parent_rel].file_count += 1

                # Ensure every ancestor folder exists (with 0 direct metrics)
                ancestor = os.path.dirname(parent_rel).replace("\\", "/")
                while ancestor and ancestor != ".":
                    if ancestor not in folder_aggregates:
                        folder_aggregates[ancestor] = FolderMetadata(relative_path=ancestor)
                    ancestor = os.path.dirname(ancestor).replace("\\", "/")

            if progress_callback:
                progress_callback(idx + 1, total_files, rel_path)

        except Exception as exc:
            error_msg = f"Error scanning {full_path}: {exc}"
            logger.warning(error_msg)
            result.errors.append(error_msg)

    # Build sorted folder list
    result.folders = sorted(folder_aggregates.values(), key=lambda f: f.relative_path)

    return result
