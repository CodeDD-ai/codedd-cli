"""
Local file auditor for the CodeDD CLI.

Reads source files from disk, sends them to the configured LLM provider
via raw ``httpx`` calls (no SDK dependency), parses the structured
response, and returns a typed audit-data dictionary ready for submission
to CodeDD.

The retry/fallback strategy mirrors the server-side ``AI_Auditor``:
    - Anthropic is the primary provider.
    - OpenAI is the fallback.
    - 3 outer retries, up to 2 attempts per provider per retry cycle.
    - Exponential backoff with jitter between outer retries.

**Security invariants**
    - The system prompt is kept in memory only — never written to disk.
    - File content is never logged at any verbosity level.
    - LLM API keys are retrieved from the OS keyring at runtime.
"""

from __future__ import annotations

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional

# Callback for debug dump: (full_prompt, response_text, audit_data, none_count)
OnDumpLLMCallback = Callable[[str, str, dict | None, int], None]

import httpx

from codedd_cli.auditor.response_parser import (
    is_audit_data_valid,
    is_response_complete,
    parse_audit_response,
)
from codedd_cli.llm.key_manager import LLMKeyManager, PROVIDER_MODELS

logger = logging.getLogger(__name__)

# LLM API endpoints (same as key_manager uses for validation)
_ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

# Request timeouts (seconds) — audit calls are larger than validation pings
_LLM_TIMEOUT = 180.0

# Content size limit (chars) — matches server-side max_content_chars
_MAX_CONTENT_CHARS = 250_000


class AuditFileResult:
    """Container for a single file's audit outcome."""

    __slots__ = (
        "file_path",
        "relative_path",
        "audit_data",
        "provider_used",
        "error",
    )

    def __init__(
        self,
        file_path: str,
        relative_path: str,
        audit_data: dict | None = None,
        provider_used: str | None = None,
        error: str | None = None,
    ) -> None:
        self.file_path = file_path
        self.relative_path = relative_path
        self.audit_data = audit_data
        self.provider_used = provider_used
        self.error = error

    @property
    def success(self) -> bool:
        return self.audit_data is not None and self.error is None


class LocalFileAuditor:
    """
    Read local source files, send them to an LLM with the server-provided
    system prompt, parse the structured response, and return audit data.

    Usage::

        auditor = LocalFileAuditor(
            anthropic_key="sk-ant-...",
            openai_key="sk-...",
            system_prompt="...",
            provider_preference="both",
            max_concurrent=8,
        )
        results = auditor.audit_batch(files, on_progress=callback)
    """

    def __init__(
        self,
        anthropic_key: str | None,
        openai_key: str | None,
        system_prompt: str,
        provider_preference: str = "both",
        max_concurrent: int = 8,
        on_debug: Callable[[str], None] | None = None,
        on_dump_llm: OnDumpLLMCallback | None = None,
    ) -> None:
        """
        Initialise the auditor with API keys and the system prompt.

        Args:
            anthropic_key:       Anthropic API key (may be None).
            openai_key:          OpenAI API key (may be None).
            system_prompt:       The file-audit system prompt from the server.
            provider_preference: ``"anthropic"``, ``"openai"``, or ``"both"``.
            max_concurrent:      Maximum concurrent LLM calls.
            on_debug:            Optional callback for real-time debug messages.
            on_dump_llm:         Optional callback for full prompt/response/parse dump (debug).
        """
        self._anthropic_key = anthropic_key
        self._openai_key = openai_key
        self._system_prompt = system_prompt
        self._preference = provider_preference
        self._max_concurrent = max_concurrent
        self._on_debug = on_debug
        self._on_dump_llm = on_dump_llm

        # Determine provider order
        self._primary, self._fallback = self._resolve_providers()

        # Shared httpx clients (connection pooling)
        self._anthropic_client: httpx.Client | None = None
        self._openai_client: httpx.Client | None = None

        if self._anthropic_key:
            self._anthropic_client = httpx.Client(
                base_url="https://api.anthropic.com",
                headers={
                    "x-api-key": self._anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                timeout=_LLM_TIMEOUT,
            )

        if self._openai_key:
            self._openai_client = httpx.Client(
                base_url="https://api.openai.com",
                headers={
                    "Authorization": f"Bearer {self._openai_key}",
                    "Content-Type": "application/json",
                },
                timeout=_LLM_TIMEOUT,
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release HTTP connection pools."""
        if self._anthropic_client:
            self._anthropic_client.close()
        if self._openai_client:
            self._openai_client.close()

    def __enter__(self) -> LocalFileAuditor:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Provider resolution
    # ------------------------------------------------------------------

    def _resolve_providers(self) -> tuple[str | None, str | None]:
        """
        Determine the primary and fallback provider based on preference
        and available keys.
        """
        has_anthropic = self._anthropic_key is not None
        has_openai = self._openai_key is not None

        if self._preference == "anthropic":
            primary = "anthropic" if has_anthropic else ("openai" if has_openai else None)
            fallback = "openai" if has_openai and primary == "anthropic" else None
        elif self._preference == "openai":
            primary = "openai" if has_openai else ("anthropic" if has_anthropic else None)
            fallback = "anthropic" if has_anthropic and primary == "openai" else None
        else:  # "both"
            primary = "anthropic" if has_anthropic else ("openai" if has_openai else None)
            fallback = "openai" if has_openai and primary == "anthropic" else (
                "anthropic" if has_anthropic and primary == "openai" else None
            )

        return primary, fallback

    # ------------------------------------------------------------------
    # Single-file audit
    # ------------------------------------------------------------------

    def audit_file(
        self,
        local_path: str,
        file_path: str,
        relative_path: str,
    ) -> AuditFileResult:
        """
        Audit a single file: read from disk, call LLM, parse response.

        Args:
            local_path:    Absolute path on disk to read.
            file_path:     The ``cli://...`` path registered with CodeDD.
            relative_path: Repo-relative path for display.

        Returns:
            ``AuditFileResult`` with ``audit_data`` on success or ``error`` on failure.
        """
        # --- Read file content ---
        try:
            content = Path(local_path).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return AuditFileResult(
                file_path=file_path,
                relative_path=relative_path,
                error=f"Cannot read file: {exc}",
            )

        if not content or not content.strip():
            return AuditFileResult(
                file_path=file_path,
                relative_path=relative_path,
                error="File is empty",
            )

        # Truncate oversized files (same threshold as server)
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS]

        # --- Construct prompts ---
        intro = "--- Following the Code to be audited based on the system prompt ---"
        user_prompt = f"{intro}\n\nCode Content:\n{content}"
        combined_prompt = f"{self._system_prompt}\n\n{user_prompt}"

        # --- Retry loop (mirrors server-side ai_auditor.audit_content) ---
        retry_limit = 3
        max_retries_per_provider = 2
        anthropic_retries = 0
        openai_retries = 0

        self._debug(
            f"[{relative_path}] Starting audit — "
            f"primary={self._primary}, fallback={self._fallback}, "
            f"content_len={len(content):,} chars"
        )

        for retry_count in range(retry_limit):
            audit_data: dict | None = None

            self._debug(f"[{relative_path}] Retry cycle {retry_count + 1}/{retry_limit}")

            # --- Try primary provider ---
            result = self._try_provider(
                self._primary, user_prompt, combined_prompt,
                anthropic_retries, openai_retries, max_retries_per_provider,
                file_path, relative_path,
            )
            if result is not None:
                if isinstance(result, AuditFileResult):
                    return result
                # Unpack updated retry counters + audit_data
                anthropic_retries, openai_retries, audit_data = result

            # --- Try fallback provider ---
            result = self._try_provider(
                self._fallback, user_prompt, combined_prompt,
                anthropic_retries, openai_retries, max_retries_per_provider,
                file_path, relative_path,
            )
            if result is not None:
                if isinstance(result, AuditFileResult):
                    return result
                anthropic_retries, openai_retries, audit_data = result

            # If we got a "not a script" in any attempt, accept it
            if audit_data and str(audit_data.get("is_script", "")).lower() == "no":
                self._debug(f"[{relative_path}] Accepted as non-script")
                return AuditFileResult(
                    file_path=file_path,
                    relative_path=relative_path,
                    audit_data=audit_data,
                    provider_used=self._primary or "unknown",
                )

            # Exponential backoff with jitter before next outer retry
            if retry_count < retry_limit - 1:
                sleep_time = min(60, (2 ** retry_count) * 5 + random.uniform(0, 2))
                self._debug(f"[{relative_path}] Backing off {sleep_time:.1f}s before next retry")
                time.sleep(sleep_time)

        self._debug(f"[{relative_path}] FAILED — all LLM attempts exhausted")
        return AuditFileResult(
            file_path=file_path,
            relative_path=relative_path,
            error="All LLM attempts exhausted",
        )

    def _try_provider(
        self,
        provider: str | None,
        user_prompt: str,
        combined_prompt: str,
        anthropic_retries: int,
        openai_retries: int,
        max_retries: int,
        file_path: str,
        relative_path: str,
    ) -> AuditFileResult | tuple[int, int, dict | None] | None:
        """
        Attempt one LLM call for the given provider.

        Returns:
            ``AuditFileResult`` on definitive success,
            ``(anthropic_retries, openai_retries, audit_data)`` if the call
            was made but the response needs retry,
            or ``None`` if the provider was skipped (not configured / exhausted).
        """
        if provider is None:
            return None

        if provider == "anthropic" and anthropic_retries >= max_retries:
            self._debug(f"[{relative_path}] Anthropic: retries exhausted ({anthropic_retries}/{max_retries})")
            return None
        if provider == "openai" and openai_retries >= max_retries:
            self._debug(f"[{relative_path}] OpenAI: retries exhausted ({openai_retries}/{max_retries})")
            return None

        if provider == "anthropic":
            success, response_text = self._call_anthropic(user_prompt)
            anthropic_retries += 1
        else:
            success, response_text = self._call_openai(combined_prompt)
            openai_retries += 1

        if not success or not response_text:
            self._debug(f"[{relative_path}] {provider}: call failed or empty response")
            return (anthropic_retries, openai_retries, None)

        audit_data, none_count = parse_audit_response(response_text)

        # Debug dump: full prompt, raw response, and parse result
        if self._on_dump_llm:
            self._on_dump_llm(combined_prompt, response_text, audit_data, none_count)

        if not audit_data:
            self._debug(f"[{relative_path}] {provider}: parse returned None")
            return (anthropic_retries, openai_retries, None)

        # Early return for non-script content
        if str(audit_data.get("is_script", "")).lower() == "no":
            self._debug(f"[{relative_path}] {provider}: non-script content — accepted")
            return AuditFileResult(
                file_path=file_path,
                relative_path=relative_path,
                audit_data=audit_data,
                provider_used=provider,
            )

        is_valid = is_audit_data_valid(audit_data)
        is_complete = is_response_complete(response_text, audit_data)

        self._debug(
            f"[{relative_path}] {provider}: parsed — "
            f"valid={is_valid}, complete={is_complete}, none_count={none_count}"
        )

        if is_valid and is_complete and none_count < 10:
            self._debug(f"[{relative_path}] {provider}: SUCCESS")
            return AuditFileResult(
                file_path=file_path,
                relative_path=relative_path,
                audit_data=audit_data,
                provider_used=provider,
            )

        return (anthropic_retries, openai_retries, audit_data)

    # ------------------------------------------------------------------
    # Batch audit
    # ------------------------------------------------------------------

    def audit_batch(
        self,
        files: list[dict],
        scope_dirs: dict[str, str],
        on_progress: Callable[[AuditFileResult], None] | None = None,
    ) -> list[AuditFileResult]:
        """
        Audit a list of files concurrently using a thread pool.

        Args:
            files:        List of file dicts from the audit plan.  Each must have
                          ``file_path`` (cli:// path), ``relative_path``, and the
                          sub-audit's ``root_path``.
            scope_dirs:   Mapping of ``repo_name`` → local directory path.
            on_progress:  Optional callback invoked after each file completes.

        Returns:
            List of ``AuditFileResult`` objects (one per file).
        """
        results: list[AuditFileResult] = []

        with ThreadPoolExecutor(max_workers=self._max_concurrent) as pool:
            future_map = {}
            for f in files:
                file_path = f["file_path"]
                relative_path = f["relative_path"]
                repo_name = f.get("repo_name", "")
                local_dir = scope_dirs.get(repo_name, "")

                # Build local disk path from scope directory + relative path
                local_path = str(Path(local_dir) / relative_path) if local_dir else ""

                future = pool.submit(
                    self.audit_file,
                    local_path=local_path,
                    file_path=file_path,
                    relative_path=relative_path,
                )
                future_map[future] = f

            for future in as_completed(future_map):
                try:
                    result = future.result()
                except Exception as exc:
                    info = future_map[future]
                    result = AuditFileResult(
                        file_path=info["file_path"],
                        relative_path=info["relative_path"],
                        error=str(exc),
                    )
                results.append(result)
                if on_progress:
                    on_progress(result)

        return results

    # ------------------------------------------------------------------
    # LLM HTTP calls
    # ------------------------------------------------------------------

    def _call_anthropic(self, user_prompt: str) -> tuple[bool, str | None]:
        """
        Call the Anthropic Messages API with the cached system prompt.

        Returns ``(success, response_text)``.
        """
        if not self._anthropic_client:
            self._debug("Anthropic: no client configured — skipping")
            return False, None

        model = PROVIDER_MODELS.get("anthropic", "claude-sonnet-4-6")
        prompt_len = len(user_prompt)
        self._debug(f"Anthropic: calling model={model}, prompt_len={prompt_len:,} chars")

        payload = {
            "model": model,
            "max_tokens": 8192,
            "system": self._system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }

        try:
            resp = self._anthropic_client.post("/v1/messages", json=payload)
            if resp.status_code == 200:
                body = resp.json()
                content_blocks = body.get("content", [])
                text_parts = [
                    b["text"] for b in content_blocks if b.get("type") == "text"
                ]
                result_text = "\n".join(text_parts) if text_parts else None
                resp_len = len(result_text) if result_text else 0
                self._debug(f"Anthropic: OK — response_len={resp_len:,} chars")
                return True, result_text

            # Non-200 — log the error body for diagnostics
            error_detail = self._extract_error_body(resp)
            self._debug(
                f"Anthropic: HTTP {resp.status_code} — {error_detail}"
            )
            return False, None
        except httpx.TimeoutException:
            self._debug("Anthropic: request timed out")
            return False, None
        except Exception as exc:
            self._debug(f"Anthropic: exception — {type(exc).__name__}: {exc}")
            return False, None

    def _call_openai(self, combined_prompt: str) -> tuple[bool, str | None]:
        """
        Call the OpenAI Chat Completions API with the combined prompt.

        Returns ``(success, response_text)``.
        """
        if not self._openai_client:
            self._debug("OpenAI: no client configured — skipping")
            return False, None

        model = PROVIDER_MODELS.get("openai", "gpt-5.2")
        prompt_len = len(combined_prompt)
        self._debug(f"OpenAI: calling model={model}, prompt_len={prompt_len:,} chars")

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": combined_prompt}],
            "max_tokens": 8192,
        }

        try:
            resp = self._openai_client.post("/v1/chat/completions", json=payload)
            if resp.status_code == 200:
                body = resp.json()
                choices = body.get("choices", [])
                if choices:
                    result_text = choices[0].get("message", {}).get("content")
                    resp_len = len(result_text) if result_text else 0
                    self._debug(f"OpenAI: OK — response_len={resp_len:,} chars")
                    return True, result_text
                self._debug("OpenAI: 200 but no choices in response")
                return True, None

            # Non-200 — log the error body for diagnostics
            error_detail = self._extract_error_body(resp)
            self._debug(
                f"OpenAI: HTTP {resp.status_code} — {error_detail}"
            )
            return False, None
        except httpx.TimeoutException:
            self._debug("OpenAI: request timed out")
            return False, None
        except Exception as exc:
            self._debug(f"OpenAI: exception — {type(exc).__name__}: {exc}")
            return False, None

    # ------------------------------------------------------------------
    # Debug helpers
    # ------------------------------------------------------------------

    def _debug(self, msg: str) -> None:
        """Emit a debug message via the logger and via the on_debug callback."""
        logger.debug(msg)
        if self._on_debug:
            self._on_debug(msg)

    @staticmethod
    def _extract_error_body(resp: httpx.Response) -> str:
        """Extract a short error description from a non-200 response."""
        try:
            body = resp.json()
            # Anthropic format
            err = body.get("error", {})
            if isinstance(err, dict):
                return err.get("message", str(body)[:200])
            # OpenAI format
            return str(body)[:200]
        except Exception:
            return resp.text[:200] if resp.text else "(empty body)"
