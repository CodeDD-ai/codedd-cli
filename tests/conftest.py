"""
Shared pytest fixtures for the codedd-cli test suite.
"""

import pytest

from codedd_cli.config.settings import ConfigManager


@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    """
    Provide a ConfigManager that writes to a temporary directory instead
    of the real ``~/.codedd/`` path.  Patches ``Path.home()`` so all
    config I/O is isolated.
    """
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    cfg = ConfigManager()
    return cfg


@pytest.fixture()
def sample_token():
    """Return a well-formed sample CLI token for tests."""
    return "codedd_cli_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789ABCDEFGHIJKLMNOPQR"


@pytest.fixture()
def sample_verify_response():
    """Return a JSON-style dict mimicking the /api/cli/auth/verify/ response."""
    return {
        "status": "success",
        "account_uuid": "12345678-1234-1234-1234-123456789abc",
        "account_name": "Test User",
        "email": "t***@example.com",
        "token_name": "My Laptop",
    }


@pytest.fixture()
def sample_audits_response():
    """Return a JSON-style dict mimicking the /api/cli/audits/ response."""
    return {
        "status": "success",
        "audits_data": [
            {
                "audit_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "audit_type": "single",
                "audit_name": "frontend-app",
                "audit_status": "Audit completed",
                "ai_synthesis": "2026-02-10T08:30:00",
                "repo_url": "https://github.com/org/frontend-app",
                "number_files": 120,
                "lines_of_code": 45000,
            }
        ],
        "group_audits_data": [
            {
                "audit_uuid": "ffffffff-gggg-hhhh-iiii-jjjjjjjjjjjj",
                "audit_type": "group",
                "audit_name": "Platform Audit Q1",
                "audit_status": "Audit completed",
                "ai_synthesis": "2026-02-12T14:00:00",
                "number_of_sub_audits": 5,
                "number_files": 800,
                "lines_of_code": 300000,
            }
        ],
        "total_audits": 2,
    }
