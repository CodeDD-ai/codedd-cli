"""
API-layer exceptions with user-facing messages.

Raised when the CLI cannot reach the CodeDD server (connection refused,
timeout, server disconnected, etc.). Handled at the CLI entry point to
display a single, clear message instead of a full traceback.
"""


# Pre-defined message shown when the remote is not responding.
CONNECTION_ERROR_MESSAGE = (
    "Unable to reach CodeDD. "
    "Check that the server is running and your network connection is available, "
    "or try again later."
)


class CodeDDConnectionError(Exception):
    """
    Raised when a request to the CodeDD API fails due to connection
    or transport errors (e.g. server not responding, timeout, disconnect).
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or CONNECTION_ERROR_MESSAGE)
