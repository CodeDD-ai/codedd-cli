"""
HTTP client for communicating with the CodeDD API.

Wraps ``httpx`` with:
    - Automatic ``X-CLI-Token`` header injection
    - Configurable base URL from ``~/.codedd/config.toml``
    - Retry logic with exponential back-off
    - TLS certificate verification (enforced)
    - Descriptive User-Agent header
"""

import time
from typing import Any, Optional

import httpx

from codedd_cli import __version__
from codedd_cli.api.exceptions import CodeDDConnectionError
from codedd_cli.auth.token_manager import TokenManager
from codedd_cli.config.constants import (
    MAX_RETRIES,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT_PREFIX,
)
from codedd_cli.config.settings import ConfigManager


class CodeDDClient:
    """
    Thin HTTP client that authenticates via the ``X-CLI-Token`` header.

    Usage::

        client = CodeDDClient()
        resp = client.get("/api/cli/audits/", params={"limit": 20})
        data = resp.json()
    """

    def __init__(self, config: Optional[ConfigManager] = None) -> None:
        self._config = config or ConfigManager()
        self._base_url = self._config.api_url.rstrip("/")
        self._token = TokenManager.retrieve()
        
        # For localhost HTTP, disable TLS verification (not applicable anyway)
        # For HTTPS, always verify certificates
        verify_tls = not self._base_url.startswith("http://localhost") and not self._base_url.startswith("http://127.0.0.1")
        
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=self._build_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
            verify=verify_tls,
            follow_redirects=True,
        )

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": f"{USER_AGENT_PREFIX}/{__version__}",
            "Accept": "application/json",
        }
        if self._token:
            headers["X-CLI-Token"] = self._token
        return headers

    # ---- HTTP verbs with retry ----

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("POST", path, **kwargs)

    # ---- internal ----

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """
        Execute an HTTP request with automatic retries on transient errors.

        Retries on 5xx responses and connection errors using exponential
        back-off (1s, 2s, 4s …).
        """
        last_exc: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._client.request(method, path, **kwargs)

                # Don't retry client errors (4xx)
                if resp.status_code < 500:
                    return resp

                # Retry on server errors
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** (attempt - 1))
                    continue

                return resp

            except httpx.RequestError as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** (attempt - 1))
                    continue
                raise CodeDDConnectionError() from None

        # Should not normally reach here, but satisfy the type checker
        if last_exc:
            raise CodeDDConnectionError() from None
        raise RuntimeError("Request failed after retries")  # pragma: no cover

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
