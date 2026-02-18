"""Data models for audit entities returned by the CLI API."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Audit:
    """A single-repository audit."""

    audit_uuid: str
    audit_name: str
    audit_status: str
    audit_type: str = "single"
    ai_synthesis: Optional[str] = None
    repo_url: str = ""
    number_files: int = 0
    lines_of_code: int = 0


@dataclass(frozen=True)
class GroupAudit:
    """A group audit (portfolio / multi-repo)."""

    audit_uuid: str
    audit_name: str
    audit_status: str
    audit_type: str = "group"
    ai_synthesis: Optional[str] = None
    number_of_sub_audits: int = 0
    number_files: int = 0
    lines_of_code: int = 0
