"""
Local line-of-code counter.

Counts non-empty lines of code and comment/doc lines for a single file.
This is a pure-Python implementation that mirrors the server-side
``get_code_and_doc_lines`` fallback logic — no external ``linecounter``
binary is required.
"""

import os
from typing import Tuple

from codedd_cli.scanner.file_classifier import (
    get_file_type,
    should_exclude_file,
)

# File types eligible for LoC counting
_COUNTABLE_TYPES = frozenset({
    'Source Code', 'Configuration', 'Documentation',
    'System', 'Security', 'Other',
})

# Single-line comment prefixes by convention
_COMMENT_PREFIXES = ('#', '//', '/*', '*', '*/', '--', ';', '%', 'REM ')

# Maximum file size (in bytes) to attempt reading — skip very large binaries
_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def count_lines(file_path: str) -> Tuple[int, int]:
    """
    Count lines of code and lines of documentation/comments in *file_path*.

    Args:
        file_path: Absolute path to the file.

    Returns:
        Tuple of (lines_of_code, lines_of_doc).
        Returns ``(0, 0)`` for excluded or unreadable files.
    """
    if should_exclude_file(file_path):
        return 0, 0

    file_type = get_file_type(file_path)
    if file_type not in _COUNTABLE_TYPES:
        return 0, 0

    # Safety: skip extremely large files
    try:
        size = os.path.getsize(file_path)
        if size > _MAX_FILE_SIZE:
            return 0, 0
    except OSError:
        return 0, 0

    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
    except (OSError, PermissionError):
        return 1, 0  # Minimum fallback

    non_empty = 0
    comment_lines = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        non_empty += 1
        if stripped.startswith(_COMMENT_PREFIXES):
            comment_lines += 1

    code_lines = max(non_empty - comment_lines, 1) if non_empty > 0 else 0
    return code_lines, comment_lines
