"""
Local architecture analysis for CLI-driven audits.

Runs Phase 1 (file discovery and categorization), Phase 2 (per-file LLM
analysis over local file contents), and Phase 2.5 (synthesis of
architectural_components, component_relationships and architectural_insights
from per-file analyses — also LLM-driven, using the user's API keys).

Phase 3 (graph construction and TypeDB storage) runs on the server after
the CLI POSTs the fully populated phase1_results and phase2_results.

All LLM calls use the user's own API keys; no source code leaves the machine.
"""

import json
import logging
import os
import re
import time

from typing import Any, Callable, Optional

from codedd_cli.auditor.architecture_prompts import CATEGORY_PROMPTS, CATEGORY_SCHEMAS

logger = logging.getLogger(__name__)

# Max file size for architecture analysis (1MB) and content truncation (30k chars)
_MAX_FILE_BYTES = 1024 * 1024
_MAX_CONTENT_CHARS = 30000

# Category keys expected by the server's GraphSynthesizer / file_identifier
ARCHITECTURE_CATEGORIES = [
    "backend_files",
    "frontend_files",
    "database_files",
    "infrastructure_files",
    "ci_cd_files",
    "testing_files",
    "config_files",
    "dependency_files",
]

# Extensions/names that map to categories (simplified from server FileIdentifier)
CATEGORY_PATTERNS = {
    "backend_files": (".py", ".java", ".kt", ".go", ".rs", ".rb", ".php", ".cs", ".ts", ".js", ".m", ".swift"),
    "frontend_files": (".tsx", ".jsx", ".vue", ".svelte", ".html", ".css", ".scss"),
    "database_files": (".sql", "migration", "schema", "models.py"),
    "infrastructure_files": ("Dockerfile", "docker-compose", ".yml", ".yaml", "Dockerfile"),
    "ci_cd_files": (".github/", "gitlab-ci", "Jenkinsfile", ".gitlab-ci", "build.gradle", "Makefile"),
    "testing_files": ("test_", "_test.", "spec.", ".spec.", "test.", "__tests__"),
    "config_files": ("config.", "settings.", ".env", "application.", ".config."),
    "dependency_files": ("requirements", "package.json", "pyproject.toml", "Pipfile", "go.mod", "Cargo.toml"),
}


def _categorize_file(file_path: str) -> Optional[str]:
    """Return the first matching category for a file path, or None."""
    path_lower = file_path.replace("\\", "/").lower()
    name = os.path.basename(path_lower)
    for category, patterns in CATEGORY_PATTERNS.items():
        for p in patterns:
            if p.startswith(".") and path_lower.endswith(p):
                return category
            if p in path_lower or p in name:
                return category
    return None


def build_phase1_results(
    file_paths: list[str],
    repository_path: str,
) -> dict[str, Any]:
    """
    Build Phase 1 (file identification) results from a list of file paths.

    file_paths: list of paths as stored in TypeDB (e.g. cli://uuid/repo/rel/path).
    repository_path: display path (e.g. cli://uuid/repo or repo name).

    Returns a dict compatible with the server's FileIdentifier output.
    """
    file_categories: dict[str, list[str]] = {cat: [] for cat in ARCHITECTURE_CATEGORIES}
    for fp in file_paths:
        cat = _categorize_file(fp)
        if cat and cat in file_categories:
            file_categories[cat].append(fp)

    relevant_files: list[str] = []
    for files in file_categories.values():
        relevant_files.extend(files)
    relevant_files = list(dict.fromkeys(relevant_files))

    return {
        "repository_path": repository_path,
        "total_files_scanned": len(file_paths),
        "filtered_files_count": len(file_paths),
        "relevant_files": relevant_files,
        "file_categories": file_categories,
        "identified_technologies": {},
        "folder_structure": {},
        "analysis_metadata": {
            "phase": "file_identification",
            "status": "completed",
            "data_source": "cli",
        },
    }


def build_phase2_results_empty(reason: str = "CLI stub") -> dict[str, Any]:
    """Build minimal Phase 2 results so the server can run Phase 3 and store."""
    return _build_phase2_skeleton(
        category_analyses={},
        total_files_analyzed=0,
        categories_analyzed=[],
        executive_summary=f"Architecture analysis (CLI): {reason}.",
    )


def _build_phase2_skeleton(
    category_analyses: dict[str, list],
    total_files_analyzed: int,
    categories_analyzed: list[str],
    executive_summary: str = "Architecture analysis completed on CLI.",
) -> dict[str, Any]:
    """Build Phase 2 payload with category_analyses; components/relationships/insights left for server synthesis."""
    return {
        "analysis_metadata": {
            "phase": "enhanced_llm_analysis",
            "status": "completed",
            "timestamp": time.time(),
            "total_files_analyzed": total_files_analyzed,
            "categories_analyzed": categories_analyzed,
            "enhanced_technologies_detected": 0,
            "enhanced_relationships_detected": 0,
        },
        "category_analyses": category_analyses,
        "enhanced_technology_detection": {
            "detected_technologies": {},
            "relationship_map": {},
            "technology_count": 0,
            "relationship_count": 0,
        },
        "enhanced_relationship_analysis": {
            "relationships": {},
            "component_graph": {},
            "architectural_patterns": [],
            "coupling_analysis": {},
            "communication_patterns": {},
            "critical_dependencies": [],
            "relationship_count": 0,
            "component_count": 0,
        },
        "architectural_components": {},
        "component_relationships": [],
        "architectural_insights": {
            "technology_stack": [],
            "architectural_patterns": [],
            "service_architecture": "unknown",
            "data_architecture": "unknown",
            "deployment_approach": "unknown",
            "development_practices": [],
            "coupling_analysis": {},
            "communication_patterns": {},
            "critical_dependencies": [],
            "executive_summary": executive_summary,
        },
    }


def _read_file_safely_local(local_path: str, max_chars: int = _MAX_CONTENT_CHARS) -> str | None:
    """Read file content with size limit; truncate by character count. Returns None on error."""
    try:
        if not os.path.isfile(local_path):
            return None
        size = os.path.getsize(local_path)
        if size > _MAX_FILE_BYTES:
            return None
        content = open(local_path, encoding="utf-8", errors="replace").read()
        if len(content) > max_chars:
            content = content[:max_chars] + "\n... (truncated)"
        return content
    except OSError:
        return None


def _build_analysis_prompt(file_path: str, file_content: str, category: str) -> str:
    """Build category-specific analysis prompt (server-compatible format)."""
    base_prompt = CATEGORY_PROMPTS.get(category, CATEGORY_PROMPTS["config_files"])
    expected_schema = CATEGORY_SCHEMAS.get(category, CATEGORY_SCHEMAS["config_files"])
    return f"""
{base_prompt}

File: {os.path.basename(file_path)}
Category: {category}

Expected Output Format (JSON):
{json.dumps(expected_schema, indent=2)}

File Content:
```
{file_content}
```

**CRITICAL**: Respond with ONLY valid JSON. No markdown, no explanations. Start with {{ and end with }}.
"""


def _clean_json_content(raw: str) -> str:
    """Try to fix common LLM JSON issues (trailing commas, markdown wrapper)."""
    text = raw.strip()
    # Extract from markdown code block
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    # Fix trailing commas before ] or }
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def _parse_llm_response(response_text: str, file_path: str, category: str) -> dict[str, Any] | None:
    """Extract and validate JSON from LLM response; add file_path and analysis_timestamp."""
    try:
        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start == -1 or end <= start:
            return None
        cleaned = _clean_json_content(response_text[start:end])
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    schema = CATEGORY_SCHEMAS.get(category, CATEGORY_SCHEMAS["config_files"])
    for key in schema:
        if key not in data:
            data[key] = schema[key] if isinstance(schema[key], (list, dict)) else ""
    data["file_path"] = file_path
    data["analysis_category"] = category
    data["analysis_timestamp"] = time.time()
    return data


def _call_llm_for_architecture(
    prompt: str,
    anthropic_client: Any,
    openai_client: Any,
    on_debug: Callable[[str], None] | None,
    *,
    system_override: str | None = None,
    max_tokens: int = 4000,
) -> str | None:
    """Call Anthropic first, then OpenAI fallback. Returns response text or None."""
    import httpx

    from codedd_cli.llm.key_manager import PROVIDER_MODELS

    system = system_override or (
        "You are a code analysis assistant that ONLY outputs valid JSON. "
        "No markdown, no explanations. Response must start with { and end with }."
    )
    timeout = 180.0

    def dbg(msg: str) -> None:
        if on_debug:
            on_debug(msg)
        logger.debug(msg)

    if anthropic_client:
        try:
            payload = {
                "model": PROVIDER_MODELS.get("anthropic", "claude-sonnet-4-6"),
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            }
            resp = anthropic_client.post("/v1/messages", json=payload, timeout=timeout)
            if resp.status_code == 200:
                body = resp.json()
                blocks = body.get("content", [])
                parts = [b["text"] for b in blocks if b.get("type") == "text"]
                out = "\n".join(parts) if parts else None
                if out:
                    dbg("Anthropic: OK")
                    return out
            else:
                dbg(f"Anthropic: HTTP {resp.status_code}")
        except httpx.TimeoutException:
            dbg("Anthropic: timeout")
        except Exception as e:
            dbg(f"Anthropic: {e}")

    if openai_client:
        try:
            payload = {
                "model": PROVIDER_MODELS.get("openai", "gpt-5.2"),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
            }
            resp = openai_client.post("/v1/chat/completions", json=payload, timeout=timeout)
            if resp.status_code == 200:
                body = resp.json()
                choices = body.get("choices", [])
                if choices:
                    out = choices[0].get("message", {}).get("content")
                    if out:
                        dbg("OpenAI: OK")
                        return out
            else:
                dbg(f"OpenAI: HTTP {resp.status_code}")
        except httpx.TimeoutException:
            dbg("OpenAI: timeout")
        except Exception as e:
            dbg(f"OpenAI: {e}")

    return None


# ---------------------------------------------------------------------------
#  Synthesis helpers — ported from server LLMAnalyzer
#  Build components, relationships, and insights from per-file analyses.
# ---------------------------------------------------------------------------

# Category-specific synthesis instructions.  Each value is a tuple of
# (title, focus_points, component_types, extra_schema_fields).
_SYNTHESIS_CATEGORY_META: dict[str, dict[str, Any]] = {
    "dependency_files": {
        "title": "DEPENDENCY COMPONENT SYNTHESIS",
        "focus": (
            "- Main programming language ecosystems\n"
            "- Core frameworks (web, data, testing)\n"
            "- Infrastructure dependencies\n"
            "- Development tools"
        ),
        "types": "framework|runtime|database|infrastructure|development_tool",
        "category_label": "dependency",
        "extra_fields": '"category_role": "backend_framework|frontend_framework|database|cache|testing",\n'
                        '"key_dependencies": ["dep1", "dep2"],\n'
                        '"architectural_role": "description of role in architecture"',
    },
    "infrastructure_files": {
        "title": "INFRASTRUCTURE COMPONENT SYNTHESIS",
        "focus": (
            "- Container services and their roles\n"
            "- Network configuration and service communication\n"
            "- Storage and persistence layers\n"
            "- Deployment orchestration"
        ),
        "types": "container_service|network_component|storage_component|orchestration",
        "category_label": "infrastructure",
        "extra_fields": '"services_managed": ["service1", "service2"],\n'
                        '"deployment_pattern": "containerized|kubernetes|serverless",\n'
                        '"communication_ports": ["8000", "5432"],\n'
                        '"architectural_role": "description of deployment role"',
    },
    "backend_files": {
        "title": "BACKEND COMPONENT SYNTHESIS",
        "focus": (
            "- API services and endpoints\n"
            "- Business logic and data processing\n"
            "- External service integrations\n"
            "- Data access patterns"
        ),
        "types": "api_service|business_logic|data_access|integration_service",
        "category_label": "backend",
        "extra_fields": '"api_endpoints": ["/api/endpoint1", "/api/endpoint2"],\n'
                        '"business_capabilities": ["capability1", "capability2"],\n'
                        '"data_models": ["Model1", "Model2"],\n'
                        '"architectural_role": "description of backend role"',
    },
    "frontend_files": {
        "title": "FRONTEND COMPONENT SYNTHESIS",
        "focus": (
            "- UI frameworks and component libraries\n"
            "- Routing and navigation systems\n"
            "- State management patterns\n"
            "- Build and bundling tools"
        ),
        "types": "ui_framework|component_library|routing_system|build_tool|state_management",
        "category_label": "frontend",
        "extra_fields": '"ui_components": ["Component1", "Component2"],\n'
                        '"routing_patterns": ["/route1", "/route2"],\n'
                        '"state_management": "redux|context|local",\n'
                        '"architectural_role": "description of frontend role"',
    },
    "database_files": {
        "title": "DATABASE COMPONENT SYNTHESIS",
        "focus": (
            "- Database schemas and table structures\n"
            "- Data relationships and constraints\n"
            "- Data access patterns and ORM usage\n"
            "- Migration and versioning strategies"
        ),
        "types": "database_schema|data_model|migration_system|data_access",
        "category_label": "database",
        "extra_fields": '"data_entities": ["Entity1", "Entity2"],\n'
                        '"relationships": ["entity1_to_entity2"],\n'
                        '"access_patterns": ["orm", "raw_sql"],\n'
                        '"architectural_role": "description of data role"',
    },
    "ci_cd_files": {
        "title": "CI/CD COMPONENT SYNTHESIS",
        "focus": (
            "- Build processes and automation\n"
            "- Deployment strategies and targets\n"
            "- Quality gates and testing integration\n"
            "- Artifact management and versioning"
        ),
        "types": "build_pipeline|deployment_pipeline|quality_gate|artifact_management",
        "category_label": "cicd",
        "extra_fields": '"pipeline_stages": ["build", "test", "deploy"],\n'
                        '"deployment_targets": ["staging", "production"],\n'
                        '"quality_gates": ["gate1", "gate2"],\n'
                        '"architectural_role": "description of CI/CD role"',
    },
    "testing_files": {
        "title": "TESTING COMPONENT SYNTHESIS",
        "focus": (
            "- Testing frameworks and tools\n"
            "- Test coverage and scope\n"
            "- Quality gates and metrics\n"
            "- Test data management"
        ),
        "types": "test_framework|quality_gate|test_data_management|coverage_analysis",
        "category_label": "testing",
        "extra_fields": '"test_types": ["unit", "integration", "e2e"],\n'
                        '"coverage_areas": ["area1", "area2"],\n'
                        '"quality_metrics": ["coverage", "pass_rate"],\n'
                        '"architectural_role": "description of testing role"',
    },
    "config_files": {
        "title": "CONFIGURATION COMPONENT SYNTHESIS",
        "focus": (
            "- Application settings and parameters\n"
            "- Environment-specific configurations\n"
            "- External service integrations\n"
            "- Security and credential management"
        ),
        "types": "app_configuration|environment_config|security_config|integration_config",
        "category_label": "config",
        "extra_fields": '"config_scope": ["application", "environment", "security"],\n'
                        '"external_services": ["service1", "service2"],\n'
                        '"security_features": ["feature1", "feature2"],\n'
                        '"architectural_role": "description of configuration role"',
    },
}

_JSON_CRITICAL_RULES = (
    "**CRITICAL JSON FORMATTING REQUIREMENTS:**\n"
    "1. Return ONLY valid JSON inside ```json code blocks\n"
    "2. Do NOT include actual newlines in string values - use spaces instead\n"
    "3. Keep all descriptions on single lines (no multi-line strings)\n"
    "4. Ensure all strings are properly quoted with double quotes\n"
    "5. Remove any trailing commas before closing braces or brackets\n"
    "6. Validate your JSON structure before responding"
)


def _safe_join(items: list, max_items: int = 5) -> str:
    """Format a list of strings or dicts into a comma-separated preview."""
    if not items:
        return "None identified"
    parts: list[str] = []
    for item in items[:max_items]:
        if isinstance(item, dict):
            name = (
                item.get("name")
                or item.get("path")
                or item.get("endpoint")
                or item.get("service")
                or item.get("component")
                or item.get("route")
                or item.get("table")
                or item.get("stage")
                or item.get("target")
                or item.get("type")
                or item.get("setting")
                or item.get("variable")
                or item.get("technology")
                or item.get("framework")
                or item.get("migration")
                or item.get("test")
                or str(item)
            )
            parts.append(str(name))
        else:
            parts.append(str(item))
    return ", ".join(parts) if parts else "None identified"


def _format_analyses_for_prompt(category_analyses: list[dict[str, Any]]) -> str:
    """Format per-file analyses for inclusion in category synthesis prompts."""
    formatted: list[str] = []
    for i, analysis in enumerate(category_analyses[:10]):
        if not isinstance(analysis, dict):
            continue
        file_path = analysis.get("file_path", "unknown")
        file_name = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
        ft = analysis.get("file_type", "")

        if ft == "infrastructure_config" or "docker-compose" in file_name.lower():
            text = (
                f"**File {i+1}: {file_name}**\n"
                f"- Services Defined: {_safe_join(analysis.get('services_defined', []), 8)}\n"
                f"- Ports Exposed: {_safe_join(analysis.get('ports_exposed', []))}\n"
                f"- Service Dependencies: {len(analysis.get('dependencies_between_services', []))} dependencies\n"
                f"- Deployment Pattern: {analysis.get('deployment_pattern', 'unknown')}"
            )
        elif ft == "backend_code":
            text = (
                f"**File {i+1}: {file_name}**\n"
                f"- API Endpoints: {_safe_join(analysis.get('api_endpoints', []))}\n"
                f"- Data Models: {_safe_join(analysis.get('data_models', []), 3)}\n"
                f"- Business Logic: {_safe_join(analysis.get('business_logic', []), 3)}\n"
                f"- External Integrations: {_safe_join(analysis.get('external_integrations', []), 3)}"
            )
        elif ft == "frontend_code":
            text = (
                f"**File {i+1}: {file_name}**\n"
                f"- UI Components: {_safe_join(analysis.get('ui_components', []))}\n"
                f"- Routing Config: {_safe_join(analysis.get('routing_config', []), 3)}\n"
                f"- State Management: {_safe_join(analysis.get('state_management', []), 3)}\n"
                f"- API Interactions: {_safe_join(analysis.get('api_interactions', []), 3)}\n"
                f"- Styling Approach: {analysis.get('styling_approach', 'unknown')}"
            )
        elif ft in ("database_schema", "migration"):
            text = (
                f"**File {i+1}: {file_name}**\n"
                f"- Tables/Schemas: {_safe_join(analysis.get('tables_schemas', []))}\n"
                f"- Relationships: {_safe_join(analysis.get('relationships', []), 3)}\n"
                f"- Indexes: {_safe_join(analysis.get('indexes', []), 3)}\n"
                f"- Migrations: {_safe_join(analysis.get('migrations', []), 3)}\n"
                f"- Database Type: {analysis.get('database_type', 'unknown')}"
            )
        elif ft == "ci_cd_pipeline":
            text = (
                f"**File {i+1}: {file_name}**\n"
                f"- Pipeline Stages: {_safe_join(analysis.get('pipeline_stages', []))}\n"
                f"- Deployment Targets: {_safe_join(analysis.get('deployment_targets', []), 3)}\n"
                f"- Testing Automation: {_safe_join(analysis.get('testing_automation', []), 3)}\n"
                f"- Build Processes: {_safe_join(analysis.get('build_processes', []), 3)}\n"
                f"- Pipeline Platform: {analysis.get('pipeline_platform', 'unknown')}"
            )
        elif ft in ("test_config", "test_suite"):
            text = (
                f"**File {i+1}: {file_name}**\n"
                f"- Test Types: {_safe_join(analysis.get('test_types', []))}\n"
                f"- Coverage Areas: {_safe_join(analysis.get('test_coverage_areas', []), 3)}\n"
                f"- Testing Environments: {_safe_join(analysis.get('testing_environments', []), 3)}\n"
                f"- Automation Config: {_safe_join(analysis.get('automation_config', []), 3)}\n"
                f"- Testing Framework: {analysis.get('testing_framework', 'unknown')}"
            )
        elif ft in ("application_config", "environment_config"):
            text = (
                f"**File {i+1}: {file_name}**\n"
                f"- Application Settings: {_safe_join(analysis.get('application_settings', []))}\n"
                f"- Environment Variables: {_safe_join(analysis.get('environment_variables', []), 3)}\n"
                f"- External Service Config: {_safe_join(analysis.get('external_service_config', []), 3)}\n"
                f"- Security Settings: {_safe_join(analysis.get('security_settings', []), 3)}\n"
                f"- Config Purpose: {analysis.get('config_purpose', 'unknown')}"
            )
        else:
            text = (
                f"**File {i+1}: {file_name}**\n"
                f"- Technologies: {_safe_join(analysis.get('tech_stack', []))}\n"
                f"- Frameworks: {_safe_join(analysis.get('frameworks', []), 3)}\n"
                f"- Key Functions: {_safe_join(analysis.get('key_functions', []), 3)}"
            )
        formatted.append(text)

    if len(category_analyses) > 10:
        formatted.append(f"\n... and {len(category_analyses) - 10} more files")
    return "\n\n".join(formatted)


def _build_category_synthesis_prompt(
    category: str,
    category_analyses: list[dict[str, Any]],
) -> str:
    """Build the LLM prompt that synthesises components for one category."""
    meta = _SYNTHESIS_CATEGORY_META.get(category)
    formatted_analyses = _format_analyses_for_prompt(category_analyses)

    if not meta:
        return (
            f"# GENERIC COMPONENT SYNTHESIS\n\n"
            f"Analyze these {len(category_analyses)} {category} files:\n\n"
            f"## DETAILED FILE ANALYSES:\n{formatted_analyses}\n\n"
            f"Create relevant architectural components based on the analysis.\n\n"
            f"{_JSON_CRITICAL_RULES}"
        )

    return (
        f"# {meta['title']}\n\n"
        f"Analyze these {len(category_analyses)} {category.replace('_', ' ')} files to create "
        f"architectural components:\n\n"
        f"## DETAILED FILE ANALYSES:\n{formatted_analyses}\n\n"
        f"**Create components that represent the architecture. Focus on:**\n"
        f"{meta['focus']}\n\n"
        f"**Output Format:**\n"
        f"```json\n"
        f"{{\n"
        f'  "component_name": {{\n'
        f'    "name": "component_name",\n'
        f'    "type": "{meta["types"]}",\n'
        f'    "category": "{meta["category_label"]}",\n'
        f'    "description": "Detailed description of the component",\n'
        f'    "technologies": ["tech1", "tech2"],\n'
        f'    "responsibilities": ["responsibility1", "responsibility2"],\n'
        f'    "key_files": ["file1.ext", "file2.ext"],\n'
        f'    "confidence": 85,\n'
        f'    "evidence_sources": ["{category.replace("_files", "")}_analysis"],\n'
        f"    {meta['extra_fields']}\n"
        f"  }}\n"
        f"}}\n"
        f"```\n\n"
        f"{_JSON_CRITICAL_RULES}"
    )


def _parse_category_synthesis_response(llm_response: str) -> dict[str, Any]:
    """Extract component dict from LLM synthesis response JSON."""
    try:
        m = re.search(r"```json\s*(\{.*?\})\s*```", llm_response, re.DOTALL)
        if m:
            json_str = m.group(1)
        else:
            m2 = re.search(r"(\{.*\})", llm_response, re.DOTALL)
            if m2:
                json_str = m2.group(1)
            else:
                return {}
        # Fix trailing commas
        json_str = re.sub(r",(\s*[}\]])", r"\1", json_str)
        components = json.loads(json_str)
        return components if isinstance(components, dict) else {}
    except json.JSONDecodeError:
        raise  # let caller retry
    except Exception:
        return {}


def _build_relationship_prompt(
    architectural_components: dict[str, Any],
    category_analyses: dict[str, list[dict[str, Any]]],
) -> str:
    """Build the LLM prompt for extracting relationships between components."""
    api_calls: list[Any] = []
    database_interactions: list[Any] = []
    service_dependencies: list[Any] = []
    for analyses in category_analyses.values():
        for analysis in analyses:
            if "api_interactions" in analysis:
                api_calls.extend(analysis["api_interactions"])
            elif "api_endpoints" in analysis:
                api_calls.extend([{"endpoint": ep.get("path", "")} for ep in analysis["api_endpoints"]])
            if "database_interactions" in analysis:
                database_interactions.extend(analysis["database_interactions"])
            if "dependencies_between_services" in analysis:
                service_dependencies.extend(analysis["dependencies_between_services"])
            elif "external_integrations" in analysis:
                service_dependencies.extend(analysis["external_integrations"])

    return (
        "Based on the identified architectural components and file analysis data, "
        "determine how these components interact with each other.\n\n"
        f"## ARCHITECTURAL COMPONENTS:\n{json.dumps(architectural_components, indent=2)}\n\n"
        f"## INTERACTION DATA:\n"
        f"API Calls/Endpoints: {json.dumps(api_calls[:10], indent=2)}\n"
        f"Database Interactions: {json.dumps(database_interactions[:10], indent=2)}\n"
        f"Service Dependencies: {json.dumps(service_dependencies[:10], indent=2)}\n\n"
        "Identify relationships between components focusing on:\n"
        "1. **Data Flow** - How data moves between components\n"
        "2. **API Dependencies** - Which components call which APIs\n"
        "3. **Database Access** - Which components access which databases\n"
        "4. **Service Communications** - How services communicate\n"
        "5. **Deployment Dependencies** - Which components depend on others\n\n"
        "IMPORTANT: Respond with ONLY a JSON object in this EXACT format "
        "(no markdown, no explanations):\n\n"
        "{\n"
        '  "relationships": [\n'
        "    {\n"
        '      "source": "source_component_name",\n'
        '      "target": "target_component_name",\n'
        '      "type": "api_call|data_access|dependency|communication|deployment_dependency|message_passing",\n'
        '      "description": "Clear description of how source interacts with target (max 100 characters)",\n'
        '      "strength": "high|medium|low"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Generate 3-15 meaningful relationships based on the component data above. "
        "Use exact component names from the ARCHITECTURAL COMPONENTS list.\n\n"
        f"{_JSON_CRITICAL_RULES}"
    )


def _parse_relationship_response(llm_response: str) -> list[dict[str, Any]]:
    """Parse component relationships from LLM response."""
    try:
        start = llm_response.find("{")
        end = llm_response.rfind("}") + 1
        if start == -1 or end <= start:
            return []
        raw = llm_response[start:end]
        raw = re.sub(r",(\s*[}\]])", r"\1", raw)
        parsed = json.loads(raw)
        rels: list[dict[str, Any]] = []
        for rel in parsed.get("relationships", []):
            src = rel.get("source", "")
            tgt = rel.get("target", "")
            if src and tgt:
                rels.append({
                    "source": src,
                    "target": tgt,
                    "type": rel.get("type", "unknown"),
                    "description": rel.get("description", "")[:200],
                    "strength": rel.get("strength", "medium"),
                })
        return rels
    except (json.JSONDecodeError, Exception):
        return []


def _create_fallback_relationships(
    architectural_components: dict[str, Any],
) -> list[dict[str, Any]]:
    """Basic fallback relationships when LLM extraction fails."""
    rels: list[dict[str, Any]] = []
    keys = list(architectural_components.keys())
    if len(keys) < 2:
        return rels
    if "core_application" in keys and "configuration_management" in keys:
        rels.append({
            "source": "core_application",
            "target": "configuration_management",
            "type": "uses",
            "description": "Core application uses configuration management for settings",
            "strength": "high",
        })
    if "testing_framework" in keys and "core_application" in keys:
        rels.append({
            "source": "testing_framework",
            "target": "core_application",
            "type": "tests",
            "description": "Testing framework validates core application functionality",
            "strength": "medium",
        })
    return rels


def _extract_architectural_insights(
    architectural_components: dict[str, Any],
    component_relationships: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive insights deterministically from components and relationships."""
    insights: dict[str, Any] = {
        "technology_stack": [],
        "architectural_patterns": [],
        "service_architecture": "",
        "data_architecture": "unknown",
        "deployment_approach": "",
        "development_practices": [],
        "coupling_analysis": {},
        "communication_patterns": {},
        "critical_dependencies": [],
    }
    all_tech: set[str] = set()
    for comp in architectural_components.values():
        all_tech.update(comp.get("technologies", []))
    insights["technology_stack"] = sorted(all_tech)

    patterns: set[str] = set()
    backend_services = [c for c in architectural_components.values() if c.get("category") == "backend"]
    if len(backend_services) > 1:
        insights["service_architecture"] = "microservices"
        patterns.add("Microservices Architecture")
    else:
        insights["service_architecture"] = "monolithic"
        patterns.add("Monolithic Architecture")

    if "Docker" in all_tech or "Kubernetes" in all_tech:
        patterns.add("Containerized Deployment")
    if "Redis" in all_tech or "RabbitMQ" in all_tech:
        patterns.add("Message Queue Pattern")
    if any("REST" in t for t in all_tech):
        patterns.add("RESTful API Design")
    insights["architectural_patterns"] = sorted(patterns)

    infra = [c for c in architectural_components.values() if c.get("category") == "infrastructure"]
    if any("kubernetes" in c.get("name", "").lower() for c in infra):
        insights["deployment_approach"] = "kubernetes"
    elif any("docker" in c.get("name", "").lower() for c in infra):
        insights["deployment_approach"] = "containerized"
    else:
        insights["deployment_approach"] = "traditional"

    return insights


def _build_executive_summary_prompt(
    architectural_components: dict[str, Any],
    component_relationships: list[dict[str, Any]],
    insights: dict[str, Any],
) -> str:
    """Build prompt for executive summary generation."""
    security_highlights: list[str] = []
    cicd_present = any(c.get("category") == "cicd" for c in architectural_components.values())
    testing_present = any(c.get("category") == "testing" for c in architectural_components.values())
    for comp in architectural_components.values():
        for tech in comp.get("technologies", []):
            name = str(tech).lower()
            if any(kw in name for kw in ("jwt", "oauth", "saml", "csrf", "security", "auth")):
                security_highlights.append(tech)
    security_highlights = list(set(security_highlights))[:10]

    return (
        "Create a concise executive summary of this software architecture based on "
        "the following analysis:\n\n"
        f"ARCHITECTURAL COMPONENTS ({len(architectural_components)}):\n"
        f"{json.dumps(architectural_components, indent=2)}\n\n"
        f"COMPONENT RELATIONSHIPS ({len(component_relationships)}):\n"
        f"{json.dumps(component_relationships, indent=2)}\n\n"
        f"IDENTIFIED PATTERNS:\n"
        f"- Service Architecture: {insights.get('service_architecture', 'unknown')}\n"
        f"- Deployment Approach: {insights.get('deployment_approach', 'unknown')}\n"
        f"- Technology Stack: {', '.join(insights.get('technology_stack', [])[:8])}\n"
        f"- Architectural Patterns: {', '.join(insights.get('architectural_patterns', []))}\n\n"
        f"SECURITY HIGHLIGHTS:\n"
        f"{', '.join(security_highlights) if security_highlights else 'None detected'}\n\n"
        f"CI/CD PIPELINE PRESENT: {cicd_present}\n"
        f"AUTOMATED TESTING PRESENT: {testing_present}\n\n"
        "**CRITICAL INSTRUCTIONS - MARKDOWN FORMAT REQUIRED:**\n\n"
        "Provide an executive summary in **pure Markdown format**.\n"
        "- DO NOT wrap the response in JSON\n"
        "- Start directly with Markdown headings and content\n"
        "- Use Markdown syntax: # for headings, - for bullets, ** for bold\n"
        "- Maximum length: 500 words\n\n"
        "Cover at least:\n"
        "1. Overall architecture style and approach\n"
        "2. Key technologies and frameworks used\n"
        "3. Main architectural strengths or notable patterns\n"
        "4. Deployment and scalability approach\n"
        "5. Security posture and critical dependencies\n"
        "6. CI/CD & testing maturity (if data available)\n\n"
        "Return ONLY the Markdown content, nothing else."
    )


def _extract_markdown_from_response(response: str) -> str:
    """Clean up LLM response to extract pure Markdown content."""
    text = response.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
            for key in ("executive_summary", "summary", "markdown", "content", "text", "description"):
                if key in parsed and isinstance(parsed[key], str):
                    return parsed[key].strip()
        except json.JSONDecodeError:
            pass
    if "```" in text:
        m = re.search(r"```(?:markdown|md)?\s*(.*?)\s*```", text, re.DOTALL)
        if m:
            return m.group(1).strip()
    return text


def _are_components_similar(name1: str, data1: dict, name2: str, data2: dict) -> bool:
    """Check if two components should be merged (name overlap or tech overlap)."""
    words1 = set(name1.lower().split("_"))
    words2 = set(name2.lower().split("_"))
    if len(words1 & words2) >= 2:
        return True
    tech1 = set(data1.get("technologies", []))
    tech2 = set(data2.get("technologies", []))
    if len(tech1 & tech2) >= 2:
        return True
    t1, t2 = data1.get("type", ""), data2.get("type", "")
    if t1 == t2 and t1:
        return True
    return False


def _merge_and_deduplicate_components(all_components: dict[str, Any]) -> dict[str, Any]:
    """Merge similar components and remove duplicates."""
    processed: set[str] = set()
    groups: dict[str, list[str]] = {}
    for name, data in all_components.items():
        if name in processed:
            continue
        group = [name]
        for other, odata in all_components.items():
            if other != name and other not in processed:
                if _are_components_similar(name, data, other, odata):
                    group.append(other)
        best = max(group, key=lambda n: all_components[n].get("confidence", 0))
        groups[best] = group
        processed.update(group)

    final: dict[str, Any] = {}
    for rep, members in groups.items():
        merged = all_components[rep].copy()
        all_tech: set[str] = set(merged.get("technologies", []))
        all_evidence: set[str] = set(merged.get("evidence_sources", []))
        max_conf = merged.get("confidence", 0)
        for m in members[1:] if members[0] == rep else members:
            if m == rep:
                continue
            md = all_components[m]
            all_tech.update(md.get("technologies", []))
            all_evidence.update(md.get("evidence_sources", []))
            mc = md.get("confidence", 0)
            if mc > max_conf:
                max_conf = mc
        merged["technologies"] = sorted(all_tech)
        merged["evidence_sources"] = sorted(all_evidence)
        merged["confidence"] = max_conf
        if len(members) > 1:
            merged["merged_from"] = [m for m in members if m != rep]
        final[rep] = merged
    return final


def synthesize_components_locally(
    category_analyses: dict[str, list[dict[str, Any]]],
    anthropic_client: Any,
    openai_client: Any,
    on_progress: Callable[[str], None] | None = None,
    on_debug: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Run the full synthesis pipeline locally: category synthesis, relationship
    extraction, insights and executive summary — all using the user's LLM keys.

    Returns a dict with keys ``architectural_components``,
    ``component_relationships`` and ``architectural_insights`` ready to be
    merged into the phase2 payload.
    """

    def prog(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    # --- Step 1: Synthesise components per category ---
    all_components: dict[str, Any] = {}
    categories_processed: list[str] = []

    active_categories = [
        cat for cat in ARCHITECTURE_CATEGORIES
        if category_analyses.get(cat)
    ]
    prog(f"Synthesising {len(active_categories)} categories...")

    for category in active_categories:
        analyses = category_analyses[category]
        prog(f"  {category} ({len(analyses)} files)...")

        prompt = _build_category_synthesis_prompt(category, analyses)
        components: dict[str, Any] = {}
        for attempt in range(3):
            raw = _call_llm_for_architecture(prompt, anthropic_client, openai_client, on_debug)
            if not raw:
                time.sleep(2)
                continue
            try:
                components = _parse_category_synthesis_response(raw)
            except json.JSONDecodeError:
                time.sleep(2)
                continue
            if components:
                break
            time.sleep(2)

        if components:
            all_components.update(components)
            categories_processed.append(category)
            prog(f"  {category}: {len(components)} components")

    if all_components:
        all_components = _merge_and_deduplicate_components(all_components)
    prog(f"Components: {len(all_components)} (from {len(categories_processed)} categories)")

    # --- Step 2: Extract relationships between components ---
    component_relationships: list[dict[str, Any]] = []
    if all_components:
        prog("Extracting component relationships...")
        rel_prompt = _build_relationship_prompt(all_components, category_analyses)
        raw = _call_llm_for_architecture(rel_prompt, anthropic_client, openai_client, on_debug)
        if raw:
            component_relationships = _parse_relationship_response(raw)
        if not component_relationships:
            component_relationships = _create_fallback_relationships(all_components)
        prog(f"Relationships: {len(component_relationships)}")

    # --- Step 3: Derive insights (deterministic) ---
    insights = _extract_architectural_insights(all_components, component_relationships)

    # --- Step 4: Generate executive summary (LLM) ---
    if all_components:
        prog("Generating executive summary...")
        summary_prompt = _build_executive_summary_prompt(
            all_components, component_relationships, insights,
        )
        summary_system = (
            "You are a software architecture analyst that produces clear, concise "
            "Markdown executive summaries. Respond ONLY with Markdown content."
        )
        raw = _call_llm_for_architecture(
            summary_prompt,
            anthropic_client,
            openai_client,
            on_debug,
            system_override=summary_system,
            max_tokens=2000,
        )
        if raw:
            insights["executive_summary"] = _extract_markdown_from_response(raw)
        else:
            insights["executive_summary"] = (
                f"Architecture consists of {len(all_components)} main components "
                f"using {insights.get('service_architecture', 'unknown')} architecture pattern."
            )
    else:
        insights["executive_summary"] = "No architectural components identified."

    return {
        "architectural_components": all_components,
        "component_relationships": component_relationships,
        "architectural_insights": insights,
    }


def _analyze_single_file(
    file_path: str,
    local_path: str,
    category: str,
    anthropic_client: Any,
    openai_client: Any,
    on_debug: Callable[[str], None] | None,
) -> dict[str, Any] | None:
    """Read file, build prompt, call LLM, parse. Returns analysis dict or None."""
    content = _read_file_safely_local(local_path)
    if not content:
        return None
    prompt = _build_analysis_prompt(file_path, content, category)
    for attempt in range(3):
        response = _call_llm_for_architecture(prompt, anthropic_client, openai_client, on_debug)
        if not response:
            time.sleep(2)
            continue
        parsed = _parse_llm_response(response, file_path, category)
        if parsed:
            return parsed
        time.sleep(2)
    return None


def run_phase2_with_llm(
    phase1_results: dict[str, Any],
    scope_dirs: dict[str, str],
    sub_audit_uuid: str,
    repo_name: str,
    on_progress: Callable[[str], None] | None = None,
    on_debug: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Run full Phase 2: per-file LLM analysis using local paths from scope_dirs.
    Resolves cli://sub_audit_uuid/repo_name/rel paths to local paths.
    Returns phase2 dict with category_analyses populated; server will synthesize components.
    """
    import httpx

    from codedd_cli.llm.key_manager import LLMKeyManager

    key_mgr = LLMKeyManager()
    anthropic_key = key_mgr.retrieve_key("anthropic")
    openai_key = key_mgr.retrieve_key("openai")
    if not anthropic_key and not openai_key:
        if on_debug:
            on_debug("No LLM keys configured — skipping Phase 2 LLM analysis")
        return build_phase2_results_empty("No LLM keys; run codedd llm set-key")

    local_base = scope_dirs.get(repo_name, "")
    if not local_base or not os.path.isdir(local_base):
        if on_debug:
            on_debug("No local scope for repo — skipping Phase 2 LLM")
        return build_phase2_results_empty("No local scope for repo")

    prefix = f"cli://{sub_audit_uuid}/{repo_name}/"
    file_categories = phase1_results.get("file_categories") or {}
    if not file_categories:
        return build_phase2_results_empty("No file categories in Phase 1")

    anthropic_client = None
    openai_client = None
    if anthropic_key:
        anthropic_client = httpx.Client(
            base_url="https://api.anthropic.com",
            headers={
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=180.0,
        )
    if openai_key:
        openai_client = httpx.Client(
            base_url="https://api.openai.com",
            headers={
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json",
            },
            timeout=180.0,
        )

    try:
        category_analyses: dict[str, list[dict[str, Any]]] = {c: [] for c in ARCHITECTURE_CATEGORIES}
        total_done = 0
        total_files = sum(len(files) for files in file_categories.values() if files)

        for category, files in file_categories.items():
            if category not in CATEGORY_PROMPTS or not files:
                continue
            if on_progress:
                on_progress(f"Analysing {category} ({len(files)} files)...")
            for file_path in files:
                if not file_path.startswith(prefix):
                    continue
                rel = file_path[len(prefix) :].lstrip("/")
                local_path = os.path.normpath(os.path.join(local_base, rel))
                if not os.path.isfile(local_path):
                    continue
                result = _analyze_single_file(
                    file_path, local_path, category,
                    anthropic_client, openai_client, on_debug,
                )
                if result:
                    category_analyses.setdefault(category, []).append(result)
                total_done += 1
                if on_progress and total_done % 5 == 0:
                    on_progress(f"Architecture: {total_done}/{total_files} files...")

        categories_with_results = [c for c, lst in category_analyses.items() if lst]

        # --- Phase 2.5: Synthesise components, relationships, insights locally ---
        if categories_with_results:
            if on_progress:
                on_progress("Synthesising architecture (components, relationships, insights)...")
            synthesis = synthesize_components_locally(
                category_analyses,
                anthropic_client,
                openai_client,
                on_progress=on_progress,
                on_debug=on_debug,
            )
        else:
            synthesis = {
                "architectural_components": {},
                "component_relationships": [],
                "architectural_insights": {
                    "technology_stack": [],
                    "architectural_patterns": [],
                    "service_architecture": "unknown",
                    "data_architecture": "unknown",
                    "deployment_approach": "unknown",
                    "development_practices": [],
                    "coupling_analysis": {},
                    "communication_patterns": {},
                    "critical_dependencies": [],
                    "executive_summary": "No file categories with results for synthesis.",
                },
            }

        phase2 = _build_phase2_skeleton(
            category_analyses=category_analyses,
            total_files_analyzed=sum(len(lst) for lst in category_analyses.values()),
            categories_analyzed=categories_with_results,
            executive_summary=synthesis["architectural_insights"].get(
                "executive_summary",
                f"CLI Phase 2: {total_done} files analysed.",
            ),
        )
        # Overlay synthesis results onto the skeleton
        phase2["architectural_components"] = synthesis["architectural_components"]
        phase2["component_relationships"] = synthesis["component_relationships"]
        phase2["architectural_insights"] = synthesis["architectural_insights"]

        return phase2
    finally:
        if anthropic_client:
            anthropic_client.close()
        if openai_client:
            openai_client.close()


def run_architecture_analysis(
    sub_audit_uuid: str,
    repo_name: str,
    file_paths: list[str],
    scope_dirs: Optional[dict[str, str]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
    on_debug: Optional[Callable[[str], None]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Run local architecture Phase 1, Phase 2 and synthesis for one sub-audit.

    file_paths: list of file paths in TypeDB form (cli://uuid/repo/rel).
    scope_dirs: optional mapping repo_name -> local directory; required for full Phase 2.
    Returns (phase1_results, phase2_results) to POST to the server.

    When scope_dirs is provided and LLM keys are configured, runs full Phase 2
    (per-file LLM analysis) **and** synthesis (components, relationships,
    insights) locally using the user's LLM API keys.  The server only needs
    to run Phase 3 (graph construction + TypeDB storage) — no server-side LLM
    calls are required.
    """
    def prog(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    repo_path = f"cli://{sub_audit_uuid}/{repo_name}"
    prog("Building file categories (Phase 1)...")
    phase1 = build_phase1_results(file_paths, repo_path)

    if scope_dirs and scope_dirs.get(repo_name):
        prog("Running Phase 2 (LLM analysis per file)...")
        phase2 = run_phase2_with_llm(
            phase1,
            scope_dirs=scope_dirs,
            sub_audit_uuid=sub_audit_uuid,
            repo_name=repo_name,
            on_progress=on_progress,
            on_debug=on_debug,
        )
    else:
        prog("Preparing architecture payload (Phase 2 stub; no scope_dirs or LLM)...")
        phase2 = build_phase2_results_empty(
            "No scope_dirs for this repo or LLM not configured; run with --scope or set LLM keys for full Phase 2."
        )

    return phase1, phase2
