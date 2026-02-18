"""
Parse structured LLM audit responses into field dictionaries.

This module is the CLI-side equivalent of the server's
``AI_Auditor.parse_audit_response``.  It converts the plain-text form
filled out by an LLM into a typed Python dict that matches the CodeDD
TypeDB schema for ``file`` / ``file_analysis`` entities.

All parsing logic is deterministic (regex + string splitting) and
contains no proprietary IP — the intelligence lives in the system prompt
served by the server at audit time.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema mapping: field_name → section prefix in the LLM response
# ---------------------------------------------------------------------------
SCHEMA_MAPPING: dict[str, str] = {
    "is_script": "0.",
    "is_script_explanation": "0.1.",
    "script_purpose": "1.1.",
    "domain": "1.2.",
    "summary_all": "1.3.",
    "recommendation": "1.4.",
    "tags": "1.5.",
    "readability": "2.1.",
    "consistency": "2.2.",
    "modularity": "2.3.",
    "maintainability": "2.4.",
    "reusability": "2.5.",
    "redundancy": "2.6.",
    "technical_debt": "2.7.",
    "code_smells": "2.8.",
    "summary_code_quality": "2.9.",
    "completeness": "3.1.",
    "edge_cases": "3.2.",
    "error_handling": "3.3.",
    "summary_functionality": "3.4.",
    "efficiency": "4.1.",
    "scalability": "4.2.",
    "resource_utilization": "4.3.",
    "load_handling": "4.4.",
    "parallel_processing": "4.5.",
    "database_interaction_efficiency": "4.6.",
    "concurrency_management": "4.7.",
    "state_management_efficiency": "4.8.",
    "modularity_decoupling": "4.9.",
    "configuration_customization_ease": "4.10.",
    "summary_perf_scal": "4.11.",
    "input_validation": "5.1.",
    "data_handling": "5.2.",
    "authentication": "5.3.",
    "summary_security": "5.4.",
    "independence": "6.1.",
    "integration": "6.2.",
    "summary_compatibility": "6.3.",
    "inline_comments": "7.1.",
    "summary_documentation": "7.2.",
    "standards": "8.1.",
    "design_patterns": "8.2.",
    "code_complexity": "8.3.",
    "refactoring_opportunities": "8.4.",
    "summary_standards": "8.5.",
    "reasons_of_flag": "9.1.",
    "flag_color": "9.2.",
    "time_to_fix_flag": "9.3.",
}

# Pre-compiled regex for section headers (e.g. "1.1.", "9.3.")
_SECTION_HEADER_RE = re.compile(r"^\d+\.\d+\.")

# Allow optional Markdown around section headers (e.g. "**1.1." or "### 1.1.")
_MARKDOWN_PREFIX_RE = re.compile(r"^\s*(\*{1,2}\s*|#{1,3}\s*)")

# Pre-compiled regex to strip prompt-artefact phrases like "(Max 50 words)"
_PROMPT_ARTIFACT_RE = re.compile(
    r"\s*\([Mm]ax\.?\s*\d+\s*words?\)\s*:?\s*",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize_response_text(text: str) -> str:
    """Remove prompt-artefact phrases from labels so parsing is robust."""
    if not text:
        return text
    normalized = re.sub(
        r"\s+\([Mm]ax\.?\s*\d+\s*words?\)\s*:?\s*",
        ": ",
        text,
    )
    normalized = re.sub(
        r"\s+\([Mm]ax\.?\s*\d+\s*words?\s*:\s*\)\s*:\s*",
        ": ",
        normalized,
    )
    return normalized


def _normalize_line_for_parsing(line: str) -> str:
    """
    Strip Markdown formatting from a line so section headers are recognized.

    LLMs often return the audit form in Markdown, e.g.:
        **1.1. Script Purpose (Max 50 words):**
        This script provides...

    We need lines to start with the section prefix (e.g. "1.1.") for
    startswith() and _SECTION_HEADER_RE to match. This helper:
    - Strips leading ** or ### (and optional space)
    - Replaces ":**" with ":" so the colon separates label from value
    """
    s = line.strip()
    if not s:
        return s
    # Strip leading Markdown bold or heading
    s = _MARKDOWN_PREFIX_RE.sub("", s)
    # Turn ":** " or ":**" into ":" so split(":", 1) gives correct value
    s = s.replace(":**", ":", 1)
    return s


def _clean_value_from_prompt_artifacts(value: str) -> str:
    """Strip prompt-artefact phrases from extracted *values*."""
    if not value:
        return value
    return _PROMPT_ARTIFACT_RE.sub(" ", value).strip()


def _sanitize_text(text: str) -> str:
    """Sanitize an extracted text value (mirrors server-side sanitize_text)."""
    try:
        sanitized = (
            text.replace('"', "")
            .replace("'", "")
            .replace("[", "")
            .replace("]", "")
            .replace("/", "")
        )
        sanitized = sanitized.rstrip(".")
        if sanitized.strip().lower() in ("na", "n/a", "not applicable", "not available"):
            return "N/A"
        return sanitized
    except Exception:
        return text


def _sanitize_domain(text: str) -> str:
    """Sanitize domain values — keep at most two words, strip quotes."""
    try:
        cleaned = text.replace("(", "").replace(")", "").replace("'", "").replace('"', "").replace(",", "")
        words = cleaned.split()
        return " ".join(words[:2]) if len(words) > 2 else cleaned
    except Exception:
        return text


def _parse_time_to_fix(text: str | None) -> float | None:
    """Convert a raw time-to-fix string into a float (hours) or None."""
    if text is None or str(text).strip().lower() in (
        "na", "n/a", "not applicable", "not available", "none",
    ):
        return None
    try:
        return float(text)
    except ValueError:
        match = re.search(r"\d+(\.\d+)?", str(text))
        return float(match.group()) if match else None


def _is_na(value: str | None) -> bool:
    """Return True if *value* is semantically N/A."""
    return str(value or "").strip().lower() in (
        "n/a", "na", "none", "not applicable", "not available",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_audit_response(response_text: str) -> tuple[Optional[dict], int]:
    """
    Parse a raw LLM audit response into a structured field dictionary.

    Args:
        response_text: The complete text output from the LLM.

    Returns:
        A tuple of ``(audit_data, none_response_count)``.
        ``audit_data`` is ``None`` when the response is fatally incomplete.
        ``none_response_count`` counts fields that came back as ``"None"``.
    """
    if not response_text or not isinstance(response_text, str):
        return None, 999

    response_text = _normalize_response_text(response_text)
    raw_lines = response_text.split("\n")
    # Normalize lines so Markdown-wrapped headers (e.g. "**1.1. ...:**") are recognized
    response_lines = [_normalize_line_for_parsing(ln) for ln in raw_lines]

    # ------------------------------------------------------------------
    # Helper: extract value for a section prefix, with continuation lines
    # ------------------------------------------------------------------
    def _parse_response_line(
        point_key: str,
        startswith: str,
        default=None,
        is_time_to_fix: bool = False,
    ) -> tuple[str, object]:
        line_idx = next(
            (i for i, line in enumerate(response_lines) if line.startswith(startswith)),
            None,
        )
        if line_idx is None:
            return (point_key, default)

        line = response_lines[line_idx]
        parts = line.split(":", 1)
        if len(parts) < 2:
            return (point_key, default)

        result = parts[1].strip()

        # If value after colon is empty, use continuation line(s) until next section header
        if not result and line_idx + 1 < len(response_lines):
            continuation: list[str] = []
            for j in range(line_idx + 1, len(response_lines)):
                next_line = response_lines[j]
                if _SECTION_HEADER_RE.match(next_line.strip()):
                    break
                continuation.append(next_line)
            result = " ".join(continuation).strip()

        if not result:
            return (point_key, default)

        # Clean up extracted value
        if point_key != "domain" and not is_time_to_fix:
            result = _clean_value_from_prompt_artifacts(result)

        if point_key == "domain":
            result = _sanitize_domain(result)
        elif is_time_to_fix:
            result = _parse_time_to_fix(result)
        else:
            result = _sanitize_text(result)

        if result != default:
            return (point_key, result)
        return (point_key, default)

    # ------------------------------------------------------------------
    # Step 1: Check is_script (section 0.)
    # ------------------------------------------------------------------
    audit_data: dict = {}

    is_script_line = next(
        (line for line in response_lines if line.startswith("0.")),
        None,
    )
    if is_script_line:
        parts = is_script_line.split(":", 1)
        if len(parts) > 1:
            is_script_value = parts[1].strip().lower()
            if is_script_value == "no":
                explanation_line = next(
                    (line for line in response_lines if line.startswith("0.1.")),
                    None,
                )
                explanation = "N/A"
                if explanation_line:
                    exp_parts = explanation_line.split(":", 1)
                    if len(exp_parts) > 1:
                        explanation = exp_parts[1].strip()
                return {"is_script": "no", "is_script_explanation": explanation}, 0
            elif is_script_value == "yes":
                audit_data["is_script"] = "yes"

    # ------------------------------------------------------------------
    # Step 2: Parse every section in the schema mapping
    # ------------------------------------------------------------------
    none_response_count = 0

    for key, startswith in SCHEMA_MAPPING.items():
        if key == "is_script" and "is_script" in audit_data:
            continue

        point_key, value = _parse_response_line(
            key,
            startswith.strip(),
            default=None,
            is_time_to_fix=False,
        )

        if value == "None":
            none_response_count += 1
        audit_data[point_key] = value

    # ------------------------------------------------------------------
    # Step 3: If not a script, return early
    # ------------------------------------------------------------------
    is_script_value = audit_data.get("is_script")
    if is_script_value is not None and str(is_script_value).lower() == "no":
        return audit_data, 0

    # ------------------------------------------------------------------
    # Step 4: Build structured vulnerabilities list from 9.1/9.2/9.3
    # ------------------------------------------------------------------
    def _extract_value_with_continuation(prefix: str) -> str | None:
        idx = next(
            (i for i, line in enumerate(response_lines) if line.startswith(prefix)),
            None,
        )
        if idx is None:
            return None
        parts = response_lines[idx].split(":", 1)
        value = parts[1].strip() if len(parts) > 1 else ""
        if not value and idx + 1 < len(response_lines):
            continuation: list[str] = []
            for j in range(idx + 1, len(response_lines)):
                next_line = response_lines[j]
                if _SECTION_HEADER_RE.match(next_line.strip()):
                    break
                continuation.append(next_line)
            value = " ".join(continuation).strip()
        return value if value else None

    raw_reasons = _extract_value_with_continuation("9.1.")
    raw_colors = _extract_value_with_continuation("9.2.")
    raw_times = _extract_value_with_continuation("9.3.")

    any_9x_present = any(x is not None for x in [raw_reasons, raw_colors, raw_times])
    all_9x_na = any_9x_present and all(
        _is_na(v) for v in [raw_reasons, raw_colors, raw_times] if v is not None
    )

    if all(_is_na(v) for v in [raw_reasons, raw_colors, raw_times]):
        reasons_list: list[str] = []
        colors_list: list[str] = []
        times_list: list[float | None] = []
    else:
        reasons_list = (
            [_sanitize_text(x.strip()) for x in raw_reasons.split(";") if x.strip()]
            if raw_reasons else []
        )
        colors_list = (
            [_sanitize_text(x.strip()) for x in raw_colors.split(";") if x.strip()]
            if raw_colors else []
        )
        if raw_times and not _is_na(raw_times):
            times_list = [_parse_time_to_fix(x.strip()) for x in raw_times.split(";")]
        else:
            times_list = []

    vulnerabilities_list: list[dict] = []
    max_len = len(reasons_list) if reasons_list else max(
        len(colors_list), len(times_list),
    ) if any([colors_list, times_list]) else 0

    for idx in range(max_len):
        reason = reasons_list[idx] if idx < len(reasons_list) else "N/A"
        color = colors_list[idx] if idx < len(colors_list) else "N/A"
        hours = times_list[idx] if idx < len(times_list) else None
        vulnerabilities_list.append({"reason": reason, "color": color, "hours": hours})

    if vulnerabilities_list:
        audit_data["vulnerabilities_list"] = vulnerabilities_list

        # Backward-compatible aggregate attributes
        aggregate_reasons = "; ".join(
            v["reason"]
            for v in vulnerabilities_list
            if v.get("reason") and str(v["reason"]).strip().upper() != "N/A"
        ).strip()
        if aggregate_reasons:
            audit_data["reasons_of_flag"] = aggregate_reasons

        has_red = any(
            (v.get("color") or "").strip().lower() == "red" for v in vulnerabilities_list
        )
        has_orange = any(
            (v.get("color") or "").strip().lower() == "orange" for v in vulnerabilities_list
        )
        if has_red:
            audit_data["flag_color"] = "Red"
        elif has_orange:
            audit_data["flag_color"] = "Orange"

        total_hours = 0.0
        any_hours = False
        for v in vulnerabilities_list:
            h = v.get("hours")
            if isinstance(h, (int, float)):
                total_hours += float(h)
                any_hours = True
        if any_hours:
            audit_data["time_to_fix_flag"] = total_hours

    # ------------------------------------------------------------------
    # Step 5: Completeness check
    # ------------------------------------------------------------------
    has_any_flag_point = (
        (audit_data.get("reasons_of_flag") not in [None, "", "None", "N/A"])
        or (audit_data.get("flag_color") not in [None, "", "None", "N/A"])
        or isinstance(audit_data.get("time_to_fix_flag"), (int, float))
        or (isinstance(audit_data.get("vulnerabilities_list"), list) and len(audit_data["vulnerabilities_list"]) > 0)
    )

    if not has_any_flag_point and all_9x_na:
        audit_data.setdefault("reasons_of_flag", "N/A")
        audit_data.setdefault("flag_color", "N/A")
    elif not has_any_flag_point:
        # Incomplete — return high none_response_count to trigger retry
        return None, 999

    return audit_data, none_response_count


def is_audit_data_valid(audit_data: dict | None) -> bool:
    """
    Validate that parsed audit data contains the minimum required fields.

    For non-script content, only ``is_script`` and ``is_script_explanation``
    are required.  For script content, ``script_purpose``, ``domain``, and
    ``summary_all`` must be present.
    """
    if not audit_data or not isinstance(audit_data, dict):
        return False

    is_script = audit_data.get("is_script")
    if is_script is not None and str(is_script).lower() == "no":
        return all(
            audit_data.get(f) for f in ("is_script", "is_script_explanation")
        )

    required_fields = ("script_purpose", "domain", "summary_all")
    return all(audit_data.get(f) for f in required_fields)


def is_response_complete(response_text: str, audit_data: dict | None = None) -> bool:
    """
    Check whether the LLM response is sufficiently complete.

    Accepts as complete if:
        - Parsed data indicates not-a-script
        - Parsed data contains any flag info (9.x section)
        - Explicit N/A for all 9.x points
        - Fallback: raw text contains section 9.x markers
    """
    try:
        if isinstance(audit_data, dict):
            is_script_value = audit_data.get("is_script")
            if is_script_value is not None and str(is_script_value).lower() == "no":
                return True

            has_any_flag = (
                (isinstance(audit_data.get("vulnerabilities_list"), list) and len(audit_data["vulnerabilities_list"]) > 0)
                or (audit_data.get("reasons_of_flag") not in [None, "", "None"])
                or (audit_data.get("flag_color") not in [None, "", "None"])
                or isinstance(audit_data.get("time_to_fix_flag"), (int, float))
            )
            if has_any_flag:
                return True

            if (
                str(audit_data.get("reasons_of_flag", "")).strip().upper() == "N/A"
                and str(audit_data.get("flag_color", "")).strip().upper() == "N/A"
            ):
                return True

        # Fallback: check raw text markers
        text = (response_text or "").lower()
        markers = ("9. due diligence", "9.1.", "9.2.", "9.3.")
        return any(m in text for m in markers)
    except Exception:
        return True  # Be permissive to avoid false negatives
