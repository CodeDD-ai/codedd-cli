"""
Category-specific prompts and output schemas for CLI architecture Phase 2.

Kept in sync with the server's LLMAnalyzer.category_prompts and category_schemas
so that per-file analysis produces the same structure the server expects.
"""

# Output structure expected from the LLM per category (server-compatible)
CATEGORY_SCHEMAS = {
    "dependency_files": {
        "file_type": "dependency_manifest",
        "primary_language": "",
        "package_manager": "",
        "tech_stack": [],
        "frameworks": [],
        "databases": [],
        "infrastructure_tools": [],
        "development_tools": [],
        "testing_frameworks": [],
        "architectural_implications": "",
    },
    "infrastructure_files": {
        "file_type": "infrastructure_config",
        "infrastructure_type": "",
        "services_defined": [],
        "network_config": [],
        "storage_config": [],
        "environment_variables": [],
        "ports_exposed": [],
        "dependencies_between_services": [],
        "external_integrations": [],
        "database_interactions": [],
        "kubernetes_resources": [],
        "kubernetes_services": [],
        "pipeline_stages": [],
        "deployment_targets": [],
        "configuration_dependencies": [],
        "deployment_pattern": "",
        "scalability_features": [],
        "security_configurations": [],
    },
    "backend_files": {
        "file_type": "backend_code",
        "code_purpose": "",
        "api_endpoints": [],
        "data_models": [],
        "business_logic": [],
        "external_integrations": [],
        "database_interactions": [],
        "middleware_components": [],
        "configuration_settings": [],
        "architectural_role": "",
        "dependencies_on_other_files": [],
    },
    "frontend_files": {
        "file_type": "frontend_code",
        "code_purpose": "",
        "ui_components": [],
        "routing_config": [],
        "state_management": [],
        "api_interactions": [],
        "styling_approach": "",
        "build_configuration": [],
        "third_party_integrations": [],
        "architectural_role": "",
        "user_interactions": [],
    },
    "database_files": {
        "file_type": "database_config",
        "database_purpose": "",
        "tables_schemas": [],
        "relationships": [],
        "indexes": [],
        "migrations": [],
        "stored_procedures": [],
        "database_type": "",
        "data_access_patterns": [],
        "performance_optimizations": [],
        "data_relationships": [],
    },
    "ci_cd_files": {
        "file_type": "cicd_pipeline",
        "pipeline_platform": "",
        "pipeline_stages": [],
        "deployment_targets": [],
        "testing_automation": [],
        "build_processes": [],
        "deployment_strategies": [],
        "environment_management": [],
        "security_scanning": [],
        "notifications": [],
        "automation_level": "",
    },
    "testing_files": {
        "file_type": "testing_config",
        "testing_framework": "",
        "test_types": [],
        "test_coverage_areas": [],
        "testing_environments": [],
        "automation_config": [],
        "quality_gates": [],
        "performance_testing": [],
        "integration_testing": [],
        "test_data_management": [],
    },
    "config_files": {
        "file_type": "application_config",
        "config_purpose": "",
        "application_settings": [],
        "environment_variables": [],
        "external_service_config": [],
        "security_settings": [],
        "logging_configuration": [],
        "feature_flags": [],
        "performance_settings": [],
        "integration_settings": [],
        "deployment_config": [],
    },
}

# Category-specific analysis prompts (server-compatible)
CATEGORY_PROMPTS = {
    "dependency_files": """
Analyze this dependency/package management file to understand the technology stack and architectural implications.

IMPORTANT: Categorize technologies properly across different fields:
- tech_stack: ALL technologies found in the file
- frameworks: Only web/application frameworks (Django, React, Express, Spring, etc.)
- databases: Database technologies, including inferred ones (e.g., psycopg2 → PostgreSQL)
- infrastructure_tools: Deployment, containerization, orchestration tools
- development_tools: Testing, build, linting, development utilities
- testing_frameworks: Specific testing libraries and frameworks

Focus on extracting ALL technologies and categorizing them appropriately. Keep architectural_implications to 2-3 sentences max.
""",
    "infrastructure_files": """
Analyze this infrastructure configuration file to understand deployment architecture and service relationships.

**KUBERNETES ANALYSIS** (if applicable): Extract deployment, service, and ingress configurations.
**CI/CD ANALYSIS** (if applicable): Extract pipeline stages and deployment targets.

**STRICT REQUIREMENT**: Your response must be valid JSON and include ALL services found in the file. Do not omit services or return empty arrays if services exist.
Focus on complete service definitions, networking, dependencies, and deployment patterns.
""",
    "backend_files": """
Analyze this backend code file to understand its architectural role and relationships.
Focus on API endpoints, data models, business logic, and architectural role.
""",
    "frontend_files": """
Analyze this frontend code file to understand UI architecture and user interactions.
Focus on UI components, routing, state management, and API interactions.
""",
    "database_files": """
Analyze this database-related file to understand data architecture and relationships.
Focus on table schemas, relationships, indexes, and data access patterns.
""",
    "ci_cd_files": """
Analyze this CI/CD configuration file to understand deployment and automation architecture.
Focus on pipeline stages, deployment strategies, testing automation, and environment management.
""",
    "testing_files": """
Analyze this testing configuration file to understand quality assurance architecture.
Focus on testing frameworks, test types, coverage areas, and quality gates.
""",
    "config_files": """
Analyze this configuration file to understand application settings and external integrations.
Focus on: purpose of configuration, application behavior settings, external service connections,
security and authentication settings, logging and monitoring, performance and caching, feature flags.
Focus on application settings, external integrations, security configurations, and performance settings.
""",
}
