"""
Local repository scanner — walks directories, classifies files, and counts LoC.

This package runs **entirely on the user's machine**.  No source code or file
contents are ever sent to CodeDD; only the resulting metadata (file paths,
types, and line counts) is transmitted via the API.
"""

from codedd_cli.scanner.file_classifier import get_file_type, should_exclude_file
from codedd_cli.scanner.line_counter import count_lines
from codedd_cli.scanner.file_walker import scan_repository

__all__ = [
    "get_file_type",
    "should_exclude_file",
    "count_lines",
    "scan_repository",
]
