"""
Configuration file management for the CodeDD CLI.

Reads and writes ``~/.codedd/config.toml`` using the TOML format.
The config file stores non-sensitive session metadata; the actual
CLI token is kept in the OS keychain via the ``keyring`` library.
"""

import os
import stat
import sys
from pathlib import Path
from typing import Any, Optional

import tomli_w

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from codedd_cli.config.constants import CONFIG_DIR_NAME, DEFAULT_API_URL


def _config_dir() -> Path:
    """Return the path to ``~/.codedd/``, creating it if necessary."""
    path = Path.home() / CONFIG_DIR_NAME
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        # Restrict directory permissions to owner only (Unix)
        try:
            path.chmod(stat.S_IRWXU)
        except OSError:
            pass  # Windows does not support Unix-style permissions
    return path


def _config_file() -> Path:
    """Return the path to ``~/.codedd/config.toml``."""
    return _config_dir() / "config.toml"


class ConfigManager:
    """
    Read/write helper for the TOML configuration file.

    Sections:
        [server]   – ``api_url``
        [session]  – ``account_uuid``, ``account_name``, ``token_hint``
        [active_audit] – ``audit_uuid``, ``audit_type``, ``audit_name``
    """

    def __init__(self) -> None:
        self._path = _config_file()
        self._data: dict[str, Any] = self._load()

    # ---- persistence ----

    def _load(self) -> dict[str, Any]:
        """Load config from disk; return defaults if file is missing."""
        if not self._path.exists():
            return self._defaults()
        with open(self._path, "rb") as fh:
            data = tomllib.load(fh)
        # Migrate legacy scope_directories (single list) to per-audit: add audit_uuid to each entry
        if "scope_directories" in data and isinstance(data["scope_directories"], list):
            active = data.get("active_audit", {}).get("audit_uuid", "")
            for entry in data["scope_directories"]:
                if isinstance(entry, dict) and "audit_uuid" not in entry and active:
                    entry["audit_uuid"] = active
            data["scope_entries"] = data.pop("scope_directories", [])
        return data

    def save(self) -> None:
        """Write current config state to disk with restricted permissions."""
        with open(self._path, "wb") as fh:
            tomli_w.dump(self._data, fh)
        # Restrict file permissions to owner only
        try:
            self._path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "server": {"api_url": DEFAULT_API_URL},
            "session": {"account_uuid": "", "account_name": "", "token_hint": ""},
            "active_audit": {"audit_uuid": "", "audit_type": "", "audit_name": ""},
            "scope_entries": [],  # per-audit: list of { "audit_uuid", "path", "repo_name", "branch", "commit_hash", "confirmed", "needs_reconfirm", "last_sync" }
        }

    # ---- server section ----

    @property
    def api_url(self) -> str:
        return self._data.get("server", {}).get("api_url", DEFAULT_API_URL)

    @api_url.setter
    def api_url(self, value: str) -> None:
        self._data.setdefault("server", {})["api_url"] = value

    # ---- session section ----

    @property
    def account_uuid(self) -> str:
        return self._data.get("session", {}).get("account_uuid", "")

    @account_uuid.setter
    def account_uuid(self, value: str) -> None:
        self._data.setdefault("session", {})["account_uuid"] = value

    @property
    def account_name(self) -> str:
        return self._data.get("session", {}).get("account_name", "")

    @account_name.setter
    def account_name(self, value: str) -> None:
        self._data.setdefault("session", {})["account_name"] = value

    @property
    def token_hint(self) -> str:
        return self._data.get("session", {}).get("token_hint", "")

    @token_hint.setter
    def token_hint(self, value: str) -> None:
        self._data.setdefault("session", {})["token_hint"] = value

    @property
    def is_authenticated(self) -> bool:
        """Return True when a session is stored (token_hint is non-empty)."""
        return bool(self.token_hint)

    # ---- active_audit section ----

    @property
    def active_audit_uuid(self) -> str:
        return self._data.get("active_audit", {}).get("audit_uuid", "")

    @active_audit_uuid.setter
    def active_audit_uuid(self, value: str) -> None:
        self._data.setdefault("active_audit", {})["audit_uuid"] = value

    @property
    def active_audit_type(self) -> str:
        return self._data.get("active_audit", {}).get("audit_type", "")

    @active_audit_type.setter
    def active_audit_type(self, value: str) -> None:
        self._data.setdefault("active_audit", {})["audit_type"] = value

    @property
    def active_audit_name(self) -> str:
        return self._data.get("active_audit", {}).get("audit_name", "")

    @active_audit_name.setter
    def active_audit_name(self, value: str) -> None:
        self._data.setdefault("active_audit", {})["audit_name"] = value

    # ---- llm section ----

    @property
    def llm_provider(self) -> str:
        """
        Return the preferred LLM provider: ``"anthropic"``, ``"openai"``,
        or ``"both"`` (default).

        When ``"both"`` is selected the pipeline uses Anthropic as primary
        with OpenAI as fallback, mirroring the server-side behaviour.
        """
        return self._data.get("llm", {}).get("provider", "both")

    @llm_provider.setter
    def llm_provider(self, value: str) -> None:
        """
        Set the LLM provider preference.

        Args:
            value: One of ``"anthropic"``, ``"openai"``, ``"both"``.
        """
        self._data.setdefault("llm", {})["provider"] = value

    @property
    def llm_concurrency(self) -> int:
        """
        Maximum number of concurrent LLM API calls during file auditing.

        Depends on your API tier / rate-limit contract with the LLM provider.
        Higher values speed up audits but may trigger rate-limit errors.
        Default is ``4``; server plan may override this.

        Stored under ``[llm].concurrency`` in ``config.toml``.
        """
        return int(self._data.get("llm", {}).get("concurrency", 4))

    @llm_concurrency.setter
    def llm_concurrency(self, value: int) -> None:
        """Set the LLM concurrency limit (1–32)."""
        clamped = max(1, min(32, int(value)))
        self._data.setdefault("llm", {})["concurrency"] = clamped

    # ---- convenience ----

    def get(self, section: str, key: str, default: Any = "") -> Any:
        """Generic getter."""
        return self._data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value: Any) -> None:
        """Generic setter (auto-saves)."""
        self._data.setdefault(section, {})[key] = value
        self.save()

    def clear_session(self) -> None:
        """Wipe all session, active audit, and scope data (used on logout)."""
        self._data["session"] = {"account_uuid": "", "account_name": "", "token_hint": ""}
        self._data["active_audit"] = {"audit_uuid": "", "audit_type": "", "audit_name": ""}
        self._data["scope_entries"] = []
        self.save()

    def set_active_audit(self, audit_uuid: str, audit_type: str, audit_name: str) -> None:
        """Set the currently selected audit."""
        self.active_audit_uuid = audit_uuid
        self.active_audit_type = audit_type
        self.active_audit_name = audit_name
        self.save()

    # ---- scope (per-audit) section ----

    def _scope_entries_for_active_audit(self) -> list[dict[str, Any]]:
        """Return scope entries that belong to the currently active audit."""
        active = self.active_audit_uuid
        if not active:
            return []
        all_entries = self._data.get("scope_entries", [])
        return [e for e in all_entries if isinstance(e, dict) and e.get("audit_uuid") == active]

    @property
    def scope_directories(self) -> list[dict[str, str]]:
        """
        Return the list of directories in the **current audit's** scope.

        Each entry is a dict with keys: ``path``, ``repo_name``, ``branch``,
        ``commit_hash``, ``confirmed``, ``needs_reconfirm``, ``last_sync``.
        Scope is stored per audit so the same path can be used in different audits.
        """
        entries = self._scope_entries_for_active_audit()
        return [
            {
                "path": e["path"],
                "repo_name": e["repo_name"],
                "branch": e["branch"],
                "commit_hash": e["commit_hash"],
                "confirmed": e.get("confirmed", False),
                "needs_reconfirm": e.get("needs_reconfirm", False),
                "last_sync": e.get("last_sync", ""),
            }
            for e in entries
        ]

    def add_scope_directory(self, path: str, repo_name: str, branch: str, commit_hash: str) -> bool:
        """
        Add a validated directory to the **current audit's** scope.

        Returns False if the path is already present for this audit (duplicate).
        The same path can be in scope for a different audit.
        """
        active = self.active_audit_uuid
        if not active:
            return False
        entries = self._data.setdefault("scope_entries", [])
        normalised = str(Path(path).resolve())
        for entry in entries:
            if isinstance(entry, dict) and entry.get("audit_uuid") == active:
                if str(Path(entry.get("path", "")).resolve()) == normalised:
                    return False  # duplicate for this audit
        entries.append({
            "audit_uuid": active,
            "path": normalised,
            "repo_name": repo_name,
            "branch": branch,
            "commit_hash": commit_hash,
            "confirmed": False,
            "needs_reconfirm": False,
            "last_sync": "",
        })
        self.save()
        return True

    def remove_scope_directory(self, index: int) -> bool:
        """
        Remove a directory from the **current audit's** scope by 0-based index.

        Returns False if the index is out of range.
        """
        current = self._scope_entries_for_active_audit()
        if index < 0 or index >= len(current):
            return False
        to_remove = current[index]
        all_entries = self._data.get("scope_entries", [])
        # Remove the entry that matches this audit and is at this position in the filtered list
        path_to_remove = to_remove.get("path")
        audit_uuid = self.active_audit_uuid
        removed = False
        for i, e in enumerate(all_entries):
            if isinstance(e, dict) and e.get("audit_uuid") == audit_uuid and e.get("path") == path_to_remove:
                all_entries.pop(i)
                removed = True
                break
        if removed:
            self.save()
        return removed

    def clear_scope_directories(self) -> None:
        """Remove all directories from the **current audit's** scope."""
        active = self.active_audit_uuid
        if not active:
            return
        all_entries = self._data.get("scope_entries", [])
        self._data["scope_entries"] = [e for e in all_entries if not (isinstance(e, dict) and e.get("audit_uuid") == active)]
        self.save()

    # ---- sync state helpers ----

    def mark_scope_confirmed(self) -> None:
        """
        Set ``confirmed=True`` and ``needs_reconfirm=False`` on **all**
        scope entries for the active audit (called after ``scope confirm``).
        """
        active = self.active_audit_uuid
        if not active:
            return
        for entry in self._data.get("scope_entries", []):
            if isinstance(entry, dict) and entry.get("audit_uuid") == active:
                entry["confirmed"] = True
                entry["needs_reconfirm"] = False
        self.save()

    def mark_scope_needs_reconfirm(self, last_sync_iso: str = "") -> None:
        """
        Flag that the active audit's scope has local changes and must be
        re-confirmed before the audit can be started.

        Args:
            last_sync_iso: ISO-8601 timestamp of when the sync was performed.
        """
        active = self.active_audit_uuid
        if not active:
            return
        for entry in self._data.get("scope_entries", []):
            if isinstance(entry, dict) and entry.get("audit_uuid") == active:
                entry["needs_reconfirm"] = True
                if last_sync_iso:
                    entry["last_sync"] = last_sync_iso
        self.save()

    def update_scope_last_sync(self, last_sync_iso: str) -> None:
        """Update the ``last_sync`` timestamp for all active-audit scope entries."""
        active = self.active_audit_uuid
        if not active:
            return
        for entry in self._data.get("scope_entries", []):
            if isinstance(entry, dict) and entry.get("audit_uuid") == active:
                entry["last_sync"] = last_sync_iso
        self.save()

    @property
    def config_path(self) -> Path:
        """Return the path to the config file (for display purposes)."""
        return self._path
