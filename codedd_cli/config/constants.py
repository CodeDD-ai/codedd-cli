"""
Application-wide constants for the CodeDD CLI.
"""

# Application identity
APP_NAME = "codedd-cli"
CONFIG_DIR_NAME = ".codedd"

# Default API endpoint (production)
DEFAULT_API_URL = "https://api.codedd.ai/django_codedd"

# Keyring service name used to store the CLI token in the OS credential store
KEYRING_SERVICE = "codedd-cli"
KEYRING_TOKEN_KEY = "cli_token"

# CLI token prefix expected by the server
CLI_TOKEN_PREFIX = "codedd_cli_"

# HTTP client settings
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3
USER_AGENT_PREFIX = "codedd-cli"
