"""
Server endpoint path constants.

All paths are relative to the ``api_url`` stored in the CLI config
"""


class Endpoints:
    """Centralised registry of API paths consumed by the CLI."""

    # Authentication
    VERIFY_TOKEN = "/api/cli/auth/verify/"

    # Audits
    LIST_AUDITS = "/api/cli/audits/"

    # Scope
    REGISTER_SCOPE = "/api/cli/scope/register/"
    SCOPE_FILES = "/api/cli/scope/files/"

    # Audit lifecycle
    AUDIT_CAN_START = "/api/cli/audit/can-start/"
    AUDIT_START = "/api/cli/audit/start/"
    AUDIT_CHECKOUT = "/api/cli/audit/checkout/"
    AUDIT_PAYMENT_STATUS = "/api/cli/audit/payment-status/"

    # Local audit execution (CLI-side file auditing)
    AUDIT_PLAN = "/api/cli/audit/plan/"
    AUDIT_RESULTS = "/api/cli/audit/results/"
    AUDIT_COMPLETE = "/api/cli/audit/complete/"

    # Local complexity analysis results
    AUDIT_COMPLEXITY = "/api/cli/audit/complexity/"

    # Local dependency scanning
    AUDIT_DEPENDENCY_CONFIG = "/api/cli/audit/dependency-config/"
    AUDIT_DEPENDENCIES = "/api/cli/audit/dependencies/"

    # Local git statistics (for CLI-driven audits; enables dashboards)
    AUDIT_GIT_STATISTICS = "/api/cli/audit/git-statistics/"
    AUDIT_VULNERABILITY_VALIDATION = "/api/cli/audit/vulnerability-validation/"

    # Architecture analysis (Phase 1+2 run locally; server runs Phase 3 + storage)
    AUDIT_ARCHITECTURE = "/api/cli/audit/architecture/"
