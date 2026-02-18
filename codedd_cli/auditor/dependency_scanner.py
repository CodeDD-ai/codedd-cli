"""
Local dependency scanning for CLI audits.

Ports three server-side modules into a single CLI module:
  - **DependencyProcessor** — manifest file parsing (60+ formats)
  - **ExtensionPackageParser** — source import extraction (25+ languages)
  - **Vulnerability scanner** — OSV API queries via ``httpx``

All operations run locally.  No source code leaves the machine.
Only structured metadata (package names, versions, vulnerability counts)
is transmitted to CodeDD.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("codedd_cli")

# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ManifestResult:
    """Result from parsing a single dependency manifest file."""
    manifest_path: str          # relative path of the manifest file
    repo_name: str
    registry: str               # e.g. "npm", "pypi"
    packages: list[dict]        # [{name, version, latest_version}]
    error: str | None = None


@dataclass
class ImportResult:
    """Result from extracting imports from a single source file."""
    file_path: str              # cli:// path
    registry_prefix: str        # e.g. "$!pypi$!_"
    packages: list[str]         # just names (no versions)
    error: str | None = None


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — Manifest Parsers (ported from DependencyProcessor)
# ═══════════════════════════════════════════════════════════════════════════

class ManifestParser:
    """
    Pure-Python manifest file parser supporting 60+ dependency file formats.

    Ported wholesale from ``DependencyProcessor`` — all parsing logic is
    identical to the server-side implementation.  No Django/TypeDB
    dependencies.
    """

    # ----- Validation / normalisation helpers -----

    @staticmethod
    def _validate_package_name(name: str) -> bool:
        """Return True if *name* looks like a valid package identifier."""
        if not name or not isinstance(name, str):
            return False
        name = name.strip()
        if not name or name in ("", "null", "undefined", "None"):
            return False
        return True

    @staticmethod
    def _normalize_version(version: str) -> str | None:
        """Normalise a version string, returning None for wildcards/empties."""
        if not version or not isinstance(version, str):
            return None
        version = version.strip()
        if not version:
            return None
        if version in ("*", "latest", "master", "main", "HEAD"):
            return None
        if version.startswith("v") and len(version) > 1 and version[1].isdigit():
            version = version[1:]
        if not any(c.isalnum() for c in version):
            return None
        return version

    @staticmethod
    def _extract_version_range_bounds(spec: str) -> Tuple[str | None, str | None]:
        """
        Extract representative lower/upper bounds from a version spec
        like ``"^10.0|^11.0|^12.0"`` or ``"^1.0.0 || ^2.0.0"``.
        """
        if not isinstance(spec, str):
            return None, None
        spec = spec.strip()
        if not spec or spec in ("*", "dev-master"):
            return None, None

        parts = re.split(r"\s*\|\|?\s*", spec)
        cleaned: list[str] = []
        for part in parts:
            p = part.strip()
            if not p:
                continue
            p = re.sub(r"^[\^~><=!*]+", "", p)
            m = re.search(r"[0-9][0-9A-Za-z.\-]*", p)
            if m:
                cleaned.append(m.group(0))

        if not cleaned:
            return None, None
        return cleaned[0], cleaned[-1]

    @staticmethod
    def _deduplicate_packages(
        packages: list[tuple[str, str | None, str | None]],
    ) -> list[tuple[str, str | None, str | None]]:
        """Remove duplicates (case-insensitive) and filter invalid names."""
        seen: set[tuple[str, str | None]] = set()
        result: list[tuple[str, str | None, str | None]] = []
        for pkg, ver, latest in packages:
            if not ManifestParser._validate_package_name(pkg):
                continue
            ver = ManifestParser._normalize_version(ver)
            latest = ManifestParser._normalize_version(latest)
            key = (pkg.lower(), ver)
            if key not in seen:
                seen.add(key)
                result.append((pkg, ver, latest))
        return result

    # ----- Dispatch table -----

    @staticmethod
    def _get_parser_dispatch_table() -> dict:
        """Return filename → parser-function mapping (O(1) lookup)."""
        return {
            # JavaScript / Node.js
            "package.json": ManifestParser._parse_package_json,
            "package-lock.json": ManifestParser._parse_package_lock_json,
            "yarn.lock": ManifestParser._parse_yarn_lock,
            "pnpm-lock.yaml": ManifestParser._parse_pnpm_lock_yaml,
            # Python
            "pyproject.toml": ManifestParser._parse_pyproject_toml,
            "Pipfile": ManifestParser._parse_pipfile,
            "Pipfile.lock": ManifestParser._parse_pipfile_lock,
            "poetry.lock": ManifestParser._parse_poetry_lock,
            "uv.lock": ManifestParser._parse_uv_lock,
            "pdm.lock": ManifestParser._parse_pdm_lock,
            "environment.yml": ManifestParser._parse_conda_environment,
            "conda.yaml": ManifestParser._parse_conda_environment,
            "environment.yaml": ManifestParser._parse_conda_environment,
            # Go
            "go.mod": ManifestParser._parse_go_mod,
            "go.sum": ManifestParser._parse_go_sum,
            "Gopkg.toml": ManifestParser._parse_gopkg_toml,
            "Gopkg.lock": ManifestParser._parse_gopkg_lock,
            # PHP
            "composer.json": ManifestParser._parse_composer_json,
            "composer.lock": ManifestParser._parse_composer_lock,
            # Rust
            "Cargo.toml": ManifestParser._parse_cargo_toml,
            "Cargo.lock": ManifestParser._parse_cargo_lock,
            # Java / Maven
            "pom.xml": ManifestParser._parse_pom_xml,
            # Ruby
            "Gemfile": ManifestParser._parse_gemfile,
            "Gemfile.lock": ManifestParser._parse_gemfile_lock,
            # Gradle / Kotlin
            "build.gradle": ManifestParser._parse_gradle_build,
            "build.gradle.kts": ManifestParser._parse_gradle_build,
            "gradle.properties": ManifestParser._parse_gradle_properties,
            # Scala
            "build.sbt": ManifestParser._parse_build_sbt,
            # .NET / C#
            "packages.config": ManifestParser._parse_dotnet_packages,
            "packages.lock.json": ManifestParser._parse_packages_lock_json,
            # Dart / Flutter
            "pubspec.yaml": ManifestParser._parse_pubspec_yaml,
            "pubspec.lock": ManifestParser._parse_pubspec_lock,
            # Elixir
            "mix.exs": ManifestParser._parse_mix_exs,
            "mix.lock": ManifestParser._parse_mix_lock,
            # Swift
            "Package.swift": ManifestParser._parse_package_swift,
            "Package.resolved": ManifestParser._parse_package_resolved,
            # CocoaPods / Carthage
            "Podfile": ManifestParser._parse_podfile,
            "Podfile.lock": ManifestParser._parse_podfile_lock,
            "Cartfile": ManifestParser._parse_cartfile,
            "Cartfile.resolved": ManifestParser._parse_cartfile_resolved,
            # Haskell
            "stack.yaml": ManifestParser._parse_stack_yaml,
            ".cabal": ManifestParser._parse_cabal,
            # Julia
            "Project.toml": ManifestParser._parse_julia_project_toml,
            "Manifest.toml": ManifestParser._parse_julia_manifest_toml,
            # C++
            "vcpkg.json": ManifestParser._parse_vcpkg_json,
            "conanfile.txt": ManifestParser._parse_conanfile_txt,
            "conanfile.py": ManifestParser._parse_conanfile_py,
            "CMakeLists.txt": ManifestParser._parse_cmake_lists,
            # Deno
            "deno.json": ManifestParser._parse_deno_json,
            "deno.jsonc": ManifestParser._parse_deno_json,
            "deno.lock": ManifestParser._parse_deno_lock,
            # Perl
            "cpanfile": ManifestParser._parse_cpanfile,
            "Makefile.PL": ManifestParser._parse_makefile_pl,
            "Build.PL": ManifestParser._parse_makefile_pl,
            # R
            "DESCRIPTION": ManifestParser._parse_r_description,
            "renv.lock": ManifestParser._parse_renv_lock,
            "packrat.lock": ManifestParser._parse_packrat_lock,
            # Lua
            ".rockspec": ManifestParser._parse_rockspec,
            # Clojure
            "project.clj": ManifestParser._parse_project_clj,
            "deps.edn": ManifestParser._parse_deps_edn,
            # OCaml
            "dune-project": ManifestParser._parse_dune_project,
            # Nim
            ".nimble": ManifestParser._parse_nimble,
            # Zig
            "build.zig.zon": ManifestParser._parse_build_zig_zon,
            # Erlang
            "rebar.config": ManifestParser._parse_rebar_config,
            "rebar.lock": ManifestParser._parse_rebar_lock,
        }

    # ----- Lock-file preference (prefer lock over manifest) -----

    # When both a lock and its manifest exist in the same directory,
    # prefer the lock file (contains exact resolved versions).
    LOCK_FILE_PREFERENCES: dict[str, list[str]] = {
        "package-lock.json": ["package.json"],
        "yarn.lock": ["package.json"],
        "pnpm-lock.yaml": ["package.json"],
        "composer.lock": ["composer.json"],
        "Gemfile.lock": ["Gemfile"],
        "Cargo.lock": ["Cargo.toml"],
        "Pipfile.lock": ["Pipfile"],
        "poetry.lock": ["pyproject.toml"],
        "pubspec.lock": ["pubspec.yaml"],
        "mix.lock": ["mix.exs"],
        "Podfile.lock": ["Podfile"],
        "Cartfile.resolved": ["Cartfile"],
        "deno.lock": ["deno.json", "deno.jsonc"],
        "renv.lock": ["DESCRIPTION"],
        "packrat.lock": ["DESCRIPTION"],
        "rebar.lock": ["rebar.config"],
        "uv.lock": ["pyproject.toml"],
        "pdm.lock": ["pyproject.toml"],
    }

    # ----- Main dispatcher -----

    @staticmethod
    def parse_dependency_file(
        file_path: str,
        file_content: str,
    ) -> list[tuple[str, str | None, str | None]]:
        """
        Parse a dependency manifest and return a list of
        ``(package_name, version, latest_version)`` tuples.
        """
        try:
            filename = os.path.basename(file_path)
            filename_lower = filename.lower()
            normalized_path = os.path.normpath(file_path).lower()

            dispatch_table = ManifestParser._get_parser_dispatch_table()
            raw_packages: list[tuple] = []

            # Special cases (path-based patterns)
            if ".github/workflows" in normalized_path and filename_lower.endswith(
                (".yml", ".yaml")
            ):
                raw_packages = ManifestParser._parse_github_actions_workflow(file_content)
            elif filename_lower == "opam" or filename_lower.endswith(".opam"):
                raw_packages = ManifestParser._parse_opam(file_content)
            elif filename in dispatch_table:
                raw_packages = dispatch_table[filename](file_content)
            elif filename_lower in dispatch_table:
                raw_packages = dispatch_table[filename_lower](file_content)
            else:
                _, ext = os.path.splitext(filename_lower)
                if ext in dispatch_table:
                    raw_packages = dispatch_table[ext](file_content)
                else:
                    for pattern, parser_func in dispatch_table.items():
                        if file_path.endswith(pattern):
                            raw_packages = parser_func(file_content)
                            break
                    else:
                        raw_packages = ManifestParser._parse_requirements_txt(file_content)

            return ManifestParser._deduplicate_packages(raw_packages)

        except Exception as exc:
            logger.error("Error parsing dependency file %s: %s", file_path, exc)
            return []

    @staticmethod
    def is_dependency_file(file_path: str) -> bool:
        """
        Return True if *file_path* looks like a dependency manifest.

        Uses the dispatch-table keys plus a set of well-known patterns
        (equivalent to the server-side ``FileIdentifier``-backed check).
        """
        normalized_path = os.path.normpath(file_path).lower()
        filename = os.path.basename(normalized_path)

        # Build pattern set from dispatch table keys + extras
        dispatch_keys = set(ManifestParser._get_parser_dispatch_table().keys())

        # Additional known patterns not in dispatch table
        extras = {
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-test.txt",
            "requirements-prod.txt",
            "constraints.txt",
            "dev-requirements.txt",
            "test-requirements.txt",
            "prod-requirements.txt",
            "settings.gradle.kts",
            "ivy.xml",
            "bun.lockb",
            "cabal.project",
        }
        patterns = dispatch_keys | extras

        for raw in patterns:
            p = raw.lower()
            if p.startswith("."):
                if filename.endswith(p):
                    return True
            elif filename == p:
                return True

        # Path-based checks
        if ".github/workflows" in normalized_path and filename.endswith(
            (".yml", ".yaml")
        ):
            return True
        if filename == "opam" or filename.endswith(".opam"):
            return True
        if filename.endswith("requirements.txt"):
            return True

        return False

    @staticmethod
    def filter_duplicate_dependency_files(file_paths: list[str]) -> list[str]:
        """
        When both a manifest and its lock file exist in the same directory,
        keep only the lock file to avoid double-counting.
        """
        files_by_dir: dict[str, list[str]] = defaultdict(list)
        for fp in file_paths:
            if ManifestParser.is_dependency_file(fp):
                dir_path = os.path.dirname(os.path.normpath(fp))
                files_by_dir[dir_path].append(fp)

        result: list[str] = []
        for dir_path, files in files_by_dir.items():
            file_basenames = {os.path.basename(f).lower(): f for f in files}
            skip_manifests: set[str] = set()

            for lock_pattern, manifest_patterns in ManifestParser.LOCK_FILE_PREFERENCES.items():
                lock_lower = lock_pattern.lower()
                if lock_lower in file_basenames:
                    result.append(file_basenames[lock_lower])
                    for mp in manifest_patterns:
                        ml = mp.lower()
                        if ml in file_basenames:
                            skip_manifests.add(file_basenames[ml])

            for fp in files:
                if fp not in skip_manifests and fp not in result:
                    result.append(fp)

        return result

    # ═══════════════════════════════════════════════════════════════════════
    # Individual parser methods (ported verbatim from server)
    # ═══════════════════════════════════════════════════════════════════════

    # --- JavaScript / Node.js ---

    @staticmethod
    def _parse_package_json(file_content: str) -> list[tuple]:
        """Parse package.json file."""
        package_data = json.loads(file_content)
        pkg_info: list[tuple] = []
        for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            deps = package_data.get(section, {})
            if deps and isinstance(deps, dict):
                pkg_info.extend(ManifestParser._parse_npm_dependencies(deps))
        return pkg_info

    @staticmethod
    def _parse_npm_dependencies(dependencies: dict) -> list[tuple]:
        """Parse NPM-style dependency dict {name: version_spec}."""
        pkg_info: list[tuple] = []
        try:
            for name, version_spec in dependencies.items():
                version, latest_version = ManifestParser._extract_version_range_bounds(version_spec)
                pkg_info.append((name, version, latest_version))
        except Exception:
            pass
        return pkg_info

    @staticmethod
    def _parse_package_lock_json(file_content: str) -> list[tuple]:
        """Parse package-lock.json (v1 and v2+ formats)."""
        pkg_info: list[tuple] = []

        def _extract_npm_name(pkg_path: str) -> str | None:
            if not pkg_path:
                return None
            if "node_modules/" in pkg_path:
                name_part = pkg_path.split("node_modules/")[-1]
            else:
                name_part = pkg_path
            name_part = name_part.strip("/")
            if not name_part:
                return None
            if name_part.startswith("@"):
                parts = name_part.split("/")
                return f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else None
            return name_part.split("/")[0]

        try:
            lock_data = json.loads(file_content)
            lockfile_version = lock_data.get("lockfileVersion", 1)

            if lockfile_version >= 2:
                packages = lock_data.get("packages", {})
                for pkg_path, info in packages.items():
                    if pkg_path and isinstance(info, dict) and "version" in info:
                        name = _extract_npm_name(pkg_path)
                        if name:
                            pkg_info.append((name, info["version"], None))
            else:
                dependencies = lock_data.get("dependencies", {})
                for name, info in dependencies.items():
                    if isinstance(info, dict) and "version" in info:
                        pkg_info.append((name, info["version"], None))
        except Exception:
            pass
        return pkg_info

    @staticmethod
    def _parse_yarn_lock(file_content: str) -> list[tuple]:
        """Parse yarn.lock file."""
        pkg_info: list[tuple] = []
        current_package = None
        for line in file_content.splitlines():
            pkg_match = re.match(r'^"?(@?[^@"]+)@', line)
            if pkg_match:
                current_package = pkg_match.group(1)
                continue
            if current_package:
                ver_match = re.match(r'^\s+version\s+"([^"]+)"', line)
                if ver_match:
                    pkg_info.append((current_package, ver_match.group(1), None))
                    current_package = None
        return pkg_info

    @staticmethod
    def _parse_pnpm_lock_yaml(file_content: str) -> list[tuple]:
        """Parse pnpm-lock.yaml file."""
        pkg_info: list[tuple] = []

        def _extract_pnpm_name(pkg_path: str) -> str | None:
            if not isinstance(pkg_path, str):
                return None
            key = pkg_path.lstrip("/")
            key = key.split("(", 1)[0]
            if "@" in key:
                name_part = key.split("@", 1)[0]
            else:
                name_part = key
            name_part = name_part.strip("/")
            if not name_part:
                return None
            segments = name_part.split("/")
            if not segments:
                return None
            for i, seg in enumerate(segments):
                if seg.startswith("@") and i + 1 < len(segments):
                    return f"{seg}/{segments[i + 1]}"
            return segments[-1]

        try:
            import yaml
            lock_data = yaml.safe_load(file_content)
            packages = lock_data.get("packages", {})
            for pkg_path, pkg_data in packages.items():
                if isinstance(pkg_data, dict) and "version" in pkg_data:
                    name = _extract_pnpm_name(pkg_path)
                    if name:
                        pkg_info.append((name, pkg_data["version"], None))
        except Exception:
            # Fallback to regex if yaml not available
            current_raw_key = None
            for line in file_content.splitlines():
                m = re.match(r"^\s*/([^:]+):", line)
                if m:
                    current_raw_key = m.group(1)
                ver_match = re.match(r"^\s+version:\s+([0-9][0-9a-zA-Z.\-]+)", line)
                if ver_match and current_raw_key:
                    name = _extract_pnpm_name(current_raw_key)
                    if name:
                        pkg_info.append((name, ver_match.group(1), None))
                    current_raw_key = None
        return pkg_info

    # --- Python ---

    @staticmethod
    def _parse_pyproject_toml(file_content: str) -> list[tuple]:
        """Parse pyproject.toml (PEP 621 and Poetry formats)."""
        pkg_info: list[tuple] = []
        try:
            import tomllib  # Python 3.11+
        except ModuleNotFoundError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ModuleNotFoundError:
                tomllib = None  # type: ignore[assignment]

        toml_data: dict = {}
        if tomllib:
            try:
                toml_data = tomllib.loads(file_content)
            except Exception:
                toml_data = {}

        if toml_data:
            # PEP 621 format
            for dep_str in toml_data.get("project", {}).get("dependencies", []):
                if isinstance(dep_str, str):
                    m = re.match(
                        r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*"
                        r"(?:\(([^)]+)\)|([><=!~]+)\s*([0-9][0-9a-zA-Z.\-]+))?",
                        dep_str,
                    )
                    if m:
                        pkg_name = m.group(1)
                        version = m.group(2) or m.group(4) if m.group(3) else None
                        if version:
                            version = re.sub(r"[><=!~]+", "", version).split(",")[0].strip()
                        pkg_info.append((pkg_name, version, None))

            # Poetry format
            poetry_deps = toml_data.get("tool", {}).get("poetry", {}).get("dependencies", {})
            for name, spec in poetry_deps.items():
                if name.lower() == "python":
                    continue
                if isinstance(spec, str):
                    version = spec
                elif isinstance(spec, dict):
                    version = spec.get("version") or None
                else:
                    version = None
                pkg_info.append((name, version, None))
        else:
            # Regex fallback for [tool.poetry.dependencies]
            in_block = False
            for line in file_content.splitlines():
                if line.strip().startswith("[tool.poetry.dependencies]"):
                    in_block = True
                    continue
                if in_block:
                    if line.startswith("["):
                        break
                    m = re.match(
                        r'\s*([A-Za-z0-9_\-]+)\s*=\s*(?:"([^"\n]+)"'
                        r'|\{[^}]*version\s*=\s*"([^"\n]+)"[^}]*\})',
                        line,
                    )
                    if m:
                        ver = m.group(2) or m.group(3)
                        pkg_info.append((m.group(1), ver, None))
        return pkg_info

    @staticmethod
    def _parse_requirements_txt(file_content: str) -> list[tuple]:
        """Parse requirements.txt and similar Python dependency files."""
        pkg_info: list[tuple] = []
        req_pattern = re.compile(
            r"^\s*"
            r"(?P<name>[A-Za-z0-9_.\-]+)"
            r"(?:\[[^\]]*\])?"
            r"\s*"
            r"(?:(?P<op>===|==|~=|!=|>=|<=|>|<)\s*(?P<ver>[A-Za-z0-9_.\-]+))?"
            r"(?:\s*;.*)?"
            r"\s*$"
        )
        for raw_line in file_content.split("\n"):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(("-r ", "--requirement ", "-e ", "--editable ")) or "://" in line or line.startswith("git+"):
                continue
            m = req_pattern.match(line)
            if not m:
                continue
            pkg = m.group("name")
            op = m.group("op")
            ver = m.group("ver")
            version = ver if ver else None
            latest_version = ver if ver and op in (">=", "~=") else None
            pkg_info.append((pkg, version, latest_version))
        return pkg_info

    @staticmethod
    def _parse_pipfile(file_content: str) -> list[tuple]:
        """Parse Pipfile."""
        pkg_info: list[tuple] = []
        in_packages = None
        for line in file_content.splitlines():
            stripped = line.strip()
            if stripped.startswith("[packages]"):
                in_packages = "packages"
                continue
            if stripped.startswith("[dev-packages]"):
                in_packages = "dev"
                continue
            if stripped.startswith("["):
                in_packages = None
            if in_packages and "=" in stripped:
                m = re.match(r'([A-Za-z0-9_\-]+)\s*=\s*["{]*([^"}\n]+)', stripped)
                if m:
                    version = m.group(2).strip().lstrip("=~><*")
                    version = None if version in ("*", "") else version
                    pkg_info.append((m.group(1), version, None))
        return pkg_info

    @staticmethod
    def _parse_pipfile_lock(file_content: str) -> list[tuple]:
        """Parse Pipfile.lock."""
        pkg_info: list[tuple] = []
        try:
            lock_data = json.loads(file_content)
            for section in ("default", "develop"):
                deps = lock_data.get(section, {})
                for pkg_name, pkg_data in deps.items():
                    if isinstance(pkg_data, dict):
                        version = pkg_data.get("version", "").lstrip("=")
                        if version:
                            pkg_info.append((pkg_name, version, None))
        except Exception:
            pass
        return pkg_info

    @staticmethod
    def _parse_poetry_lock(file_content: str) -> list[tuple]:
        """Parse poetry.lock."""
        pkg_info: list[tuple] = []
        current_package: dict | None = None
        for line in file_content.splitlines():
            if line.strip() == "[[package]]":
                current_package = {}
                continue
            if current_package is not None:
                name_match = re.match(r'^name\s*=\s*"([^"]+)"', line)
                if name_match:
                    current_package["name"] = name_match.group(1)
                version_match = re.match(r'^version\s*=\s*"([^"]+)"', line)
                if version_match:
                    current_package["version"] = version_match.group(1)
                if "name" in current_package and "version" in current_package:
                    pkg_info.append((current_package["name"], current_package["version"], None))
                    current_package = None
        return pkg_info

    @staticmethod
    def _parse_uv_lock(file_content: str) -> list[tuple]:
        """Parse uv.lock file."""
        pkg_info: list[tuple] = []
        current_package: dict | None = None
        for line in file_content.splitlines():
            if line.strip() == "[[package]]":
                current_package = {}
                continue
            if current_package is not None:
                name_match = re.match(r'^name\s*=\s*"([^"]+)"', line)
                if name_match:
                    current_package["name"] = name_match.group(1)
                version_match = re.match(r'^version\s*=\s*"([^"]+)"', line)
                if version_match:
                    current_package["version"] = version_match.group(1)
                if "name" in current_package and "version" in current_package:
                    pkg_info.append((current_package["name"], current_package["version"], None))
                    current_package = None
        return pkg_info

    @staticmethod
    def _parse_pdm_lock(file_content: str) -> list[tuple]:
        """Parse pdm.lock file."""
        pkg_info: list[tuple] = []
        current_package: dict | None = None
        for line in file_content.splitlines():
            if line.strip() == "[[package]]":
                current_package = {}
                continue
            if current_package is not None:
                name_match = re.match(r'^name\s*=\s*"([^"]+)"', line)
                if name_match:
                    current_package["name"] = name_match.group(1)
                version_match = re.match(r'^version\s*=\s*"([^"]+)"', line)
                if version_match:
                    current_package["version"] = version_match.group(1)
                if "name" in current_package and "version" in current_package:
                    pkg_info.append((current_package["name"], current_package["version"], None))
                    current_package = None
        return pkg_info

    @staticmethod
    def _parse_conda_environment(file_content: str) -> list[tuple]:
        """Parse conda environment.yml files."""
        pkg_info: list[tuple] = []
        in_deps = False
        in_pip = False
        for line in file_content.splitlines():
            if line.strip().startswith("dependencies:"):
                in_deps = True
                continue
            if in_deps:
                if line.strip() == "- pip:":
                    in_pip = True
                    continue
                if in_pip and line and not line.startswith("    ") and not line.strip().startswith("#"):
                    in_pip = False
                if in_pip:
                    m = re.match(r"\s+-\s*([A-Za-z0-9_.\-]+)\s*([><=!~]+)\s*([A-Za-z0-9.\-]+)", line)
                    if m:
                        pkg_info.append((m.group(1), m.group(3), None))
                        continue
                    m2 = re.match(r"\s+-\s*([A-Za-z0-9_.\-]+)\s*$", line)
                    if m2:
                        pkg_info.append((m2.group(1), None, None))
                        continue
                if not in_pip:
                    m = re.match(r"\s*-\s*([A-Za-z0-9_\-]+)(?:[=]+([A-Za-z0-9.\-]+))?", line)
                    if m:
                        pkg_info.append((m.group(1), m.group(2), None))
                    elif line and not line.startswith("  -") and not line.strip().startswith("#"):
                        break
        return pkg_info

    # --- Go ---

    @staticmethod
    def _parse_go_mod(file_content: str) -> list[tuple]:
        """Parse go.mod file."""
        pkg_info: list[tuple] = []
        in_block = False
        for line in file_content.splitlines():
            if re.match(r"^\s*require\s*\(", line):
                in_block = True
                continue
            if in_block and line.strip() == ")":
                in_block = False
                continue
            if in_block:
                m = re.match(r"^\s*([\w\-./]+)\s+(v?[0-9][^\s]*)", line)
                if m:
                    pkg_info.append((m.group(1), m.group(2), None))
                    continue
            m2 = re.match(r"^\s*require\s+([\w\-./]+)\s+(v?[0-9][^\s]*)", line)
            if m2:
                pkg_info.append((m2.group(1), m2.group(2), None))
        return pkg_info

    @staticmethod
    def _parse_go_sum(file_content: str) -> list[tuple]:
        """Parse go.sum file (deduplicated — skips /go.mod entries)."""
        pkg_info: list[tuple] = []
        seen: set[tuple[str, str]] = set()
        for line in file_content.splitlines():
            m = re.match(r"^([\w\-./]+)\s+(v?[0-9][^\s/]+)", line)
            if m:
                key = (m.group(1), m.group(2))
                if key not in seen:
                    seen.add(key)
                    pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    @staticmethod
    def _parse_gopkg_toml(file_content: str) -> list[tuple]:
        """Parse Gopkg.toml file."""
        pkg_info: list[tuple] = []
        current_package: dict | None = None
        for line in file_content.splitlines():
            if line.strip() in ("[[constraint]]", "[[override]]"):
                current_package = {}
                continue
            if current_package is not None:
                name_match = re.match(r'^\s*name\s*=\s*"([^"]+)"', line)
                if name_match:
                    current_package["name"] = name_match.group(1)
                version_match = re.match(r'^\s*version\s*=\s*"([^"]+)"', line)
                if version_match:
                    current_package["version"] = version_match.group(1)
                if "name" in current_package and "version" in current_package:
                    pkg_info.append((current_package["name"], current_package["version"], None))
                    current_package = None
        return pkg_info

    @staticmethod
    def _parse_gopkg_lock(file_content: str) -> list[tuple]:
        """Parse Gopkg.lock file."""
        pkg_info: list[tuple] = []
        current_package: dict | None = None
        for line in file_content.splitlines():
            if line.strip() == "[[projects]]":
                current_package = {}
                continue
            if current_package is not None:
                name_match = re.match(r'^\s*name\s*=\s*"([^"]+)"', line)
                if name_match:
                    current_package["name"] = name_match.group(1)
                version_match = re.match(r'^\s*version\s*=\s*"([^"]+)"', line)
                if version_match:
                    current_package["version"] = version_match.group(1).lstrip("v")
                revision_match = re.match(r'^\s*revision\s*=\s*"([^"]+)"', line)
                if revision_match and "version" not in current_package:
                    current_package["version"] = revision_match.group(1)[:7]
                if "name" in current_package and "version" in current_package:
                    pkg_info.append((current_package["name"], current_package["version"], None))
                    current_package = None
        return pkg_info

    # --- PHP ---

    @staticmethod
    def _parse_composer_json(file_content: str) -> list[tuple]:
        """Parse composer.json file."""
        pkg_info: list[tuple] = []
        comp_data = json.loads(file_content)
        require_deps = comp_data.get("require", {}) or {}
        dev_deps = comp_data.get("require-dev", {}) or {}
        merged = dict(require_deps)
        for k, v in dev_deps.items():
            merged.setdefault(k, v)
        for pkg, raw_spec in merged.items():
            version, latest_version = ManifestParser._extract_version_range_bounds(raw_spec)
            pkg_info.append((pkg, version, latest_version))
        return pkg_info

    @staticmethod
    def _parse_composer_lock(file_content: str) -> list[tuple]:
        """Parse composer.lock file."""
        pkg_info: list[tuple] = []
        try:
            lock_data = json.loads(file_content)
            for section in ("packages", "packages-dev"):
                for pkg in lock_data.get(section, []):
                    if isinstance(pkg, dict):
                        name = pkg.get("name")
                        version = (pkg.get("version") or "").lstrip("v")
                        if name and version:
                            pkg_info.append((name, version, version))
        except Exception:
            pass
        return pkg_info

    # --- Rust ---

    @staticmethod
    def _parse_cargo_toml(file_content: str) -> list[tuple]:
        """Parse Cargo.toml file."""
        pkg_info: list[tuple] = []
        in_block = False
        for line in file_content.splitlines():
            if re.match(r"^\s*\[(dependencies|dev-dependencies|build-dependencies)\]", line):
                in_block = True
                continue
            if in_block:
                if line.strip().startswith("["):
                    in_block = False
                    continue
                m = re.match(r'\s*([A-Za-z0-9_\-]+)\s*=\s*"([^"]+)"', line)
                if m:
                    pkg_info.append((m.group(1), m.group(2), None))
                    continue
                m2 = re.match(r'\s*([A-Za-z0-9_\-]+)\s*=\s*\{[^}]*version\s*=\s*"([^"]+)"', line)
                if m2:
                    pkg_info.append((m2.group(1), m2.group(2), None))
        return pkg_info

    @staticmethod
    def _parse_cargo_lock(file_content: str) -> list[tuple]:
        """Parse Cargo.lock file."""
        pkg_info: list[tuple] = []
        current_package: dict | None = None
        for line in file_content.splitlines():
            if line.strip() == "[[package]]":
                current_package = {}
                continue
            if current_package is not None:
                name_match = re.match(r'^name\s*=\s*"([^"]+)"', line)
                if name_match:
                    current_package["name"] = name_match.group(1)
                version_match = re.match(r'^version\s*=\s*"([^"]+)"', line)
                if version_match:
                    current_package["version"] = version_match.group(1)
                if "name" in current_package and "version" in current_package:
                    pkg_info.append((current_package["name"], current_package["version"], None))
                    current_package = None
        return pkg_info

    # --- Java / Maven ---

    @staticmethod
    def _parse_pom_xml(file_content: str) -> list[tuple]:
        """Parse pom.xml file."""
        pkg_info: list[tuple] = []
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(file_content)
            for dep in root.iterfind(".//{*}dependency"):
                g = dep.find("{*}groupId")
                a = dep.find("{*}artifactId")
                v = dep.find("{*}version")
                if g is not None and a is not None:
                    name = f"{g.text}:{a.text}"
                    version = v.text if v is not None else None
                    pkg_info.append((name, version, None))
        except Exception:
            pass
        return pkg_info

    # --- Ruby ---

    @staticmethod
    def _parse_gemfile(file_content: str) -> list[tuple]:
        """Parse Gemfile."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            if line.strip().startswith("#") or not line.strip():
                continue
            m = re.match(r"""^\s*gem\s+['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]""", line)
            if m:
                pkg_info.append((m.group(1), m.group(2).lstrip("~>>=<"), None))
            else:
                m2 = re.match(r"""^\s*gem\s+['"]([^'"]+)['"]""", line)
                if m2:
                    pkg_info.append((m2.group(1), None, None))
        return pkg_info

    @staticmethod
    def _parse_gemfile_lock(file_content: str) -> list[tuple]:
        """Parse Gemfile.lock."""
        pkg_info: list[tuple] = []
        in_specs = False
        for line in file_content.splitlines():
            if line.strip() == "specs:":
                in_specs = True
                continue
            if in_specs:
                m = re.match(r"^\s{4,}([A-Za-z0-9_\-]+)\s+\(([0-9][0-9a-zA-Z.\-]*)\)", line)
                if m:
                    pkg_info.append((m.group(1), m.group(2), None))
                elif line and not line.startswith(" "):
                    in_specs = False
        return pkg_info

    # --- Gradle / Kotlin ---

    @staticmethod
    def _parse_gradle_build(file_content: str) -> list[tuple]:
        """Parse build.gradle or build.gradle.kts."""
        pkg_info: list[tuple] = []
        gradle_pattern = re.compile(
            r"\b(?:implementation|api|compile|compileOnly|testImplementation|runtimeOnly|"
            r"kapt|annotationProcessor|classpath|testCompile|testRuntime|runtimeClasspath|"
            r"compileClasspath|testRuntimeClasspath)\s*[\(\[\{]?\s*['\"]([^:'\"]+"
            r"):([^:'\"]+):([^:'\"]+)['\"]"
        )
        for m in gradle_pattern.finditer(file_content):
            group, artifact, version = m.groups()
            pkg_info.append((f"{group}:{artifact}", version, None))

        map_pattern = re.compile(
            r"group:\s*['\"]([^'\"]+)['\"],\s*name:\s*['\"]([^'\"]+)['\"]"
            r",\s*version:\s*['\"]([^'\"]+)['\"]"
        )
        for m in map_pattern.finditer(file_content):
            group, artifact, version = m.groups()
            pkg_info.append((f"{group}:{artifact}", version, None))
        return pkg_info

    @staticmethod
    def _parse_gradle_properties(file_content: str) -> list[tuple]:
        """Parse gradle.properties file."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            m = re.match(r"^\s*([A-Za-z0-9_]+Version)\s*=\s*([0-9][0-9a-zA-Z.\-]*)", line)
            if m:
                pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    # --- Scala ---

    @staticmethod
    def _parse_build_sbt(file_content: str) -> list[tuple]:
        """Parse build.sbt file."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            m = re.match(
                r'^\s*libraryDependencies\s*\+[=+]\s*"([^"]+)"\s*%%?\s*"([^"]+)"\s*%\s*"([^"]+)"',
                line,
            )
            if m:
                org, artifact, version = m.groups()
                pkg_info.append((f"{org}:{artifact}", version, None))
                continue
            for org, artifact, version in re.findall(
                r'"([^"]+)"\s*%%?\s*"([^"]+)"\s*%\s*"([^"]+)"', line
            ):
                pkg_info.append((f"{org}:{artifact}", version, None))
        return pkg_info

    # --- .NET / C# ---

    @staticmethod
    def _parse_dotnet_packages(file_content: str) -> list[tuple]:
        """Parse .csproj or packages.config files."""
        pkg_info: list[tuple] = []
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(file_content)
            for pr in root.iterfind(".//{*}PackageReference"):
                inc = pr.attrib.get("Include") or pr.attrib.get("Update")
                ver = pr.attrib.get("Version") or pr.findtext("{*}Version")
                if inc:
                    pkg_info.append((inc, ver, None))
            for pkg in root.iterfind(".//{*}package"):
                id_ = pkg.attrib.get("id")
                ver = pkg.attrib.get("version")
                if id_:
                    pkg_info.append((id_, ver, None))
        except Exception:
            pass
        return pkg_info

    @staticmethod
    def _parse_packages_lock_json(file_content: str) -> list[tuple]:
        """Parse packages.lock.json file."""
        pkg_info: list[tuple] = []
        try:
            lock_data = json.loads(file_content)
            dependencies = lock_data.get("dependencies", {})
            for _target, deps in dependencies.items():
                if isinstance(deps, dict):
                    for pkg_name, pkg_data in deps.items():
                        if isinstance(pkg_data, dict):
                            version = pkg_data.get("resolved") or pkg_data.get("version")
                            if version:
                                pkg_info.append((pkg_name, version, None))
        except Exception:
            pass
        return pkg_info

    # --- Dart / Flutter ---

    @staticmethod
    def _parse_pubspec_yaml(file_content: str) -> list[tuple]:
        """Parse pubspec.yaml file."""
        pkg_info: list[tuple] = []
        in_deps = False
        for line in file_content.splitlines():
            if re.match(r"^(dependencies|dev_dependencies):\s*$", line):
                in_deps = True
                continue
            if in_deps:
                if re.match(r"^\S", line):
                    in_deps = False
                    continue
                m = re.match(r"\s*([A-Za-z0-9_\-]+):\s*[\^~]?([0-9][0-9.\-]*)", line)
                if m:
                    pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    @staticmethod
    def _parse_pubspec_lock(file_content: str) -> list[tuple]:
        """Parse pubspec.lock file."""
        pkg_info: list[tuple] = []
        in_packages = False
        current_package = None
        for line in file_content.splitlines():
            if line.strip() == "packages:":
                in_packages = True
                continue
            if in_packages:
                pkg_match = re.match(r"^\s{2}([A-Za-z0-9_\-]+):", line)
                if pkg_match:
                    current_package = pkg_match.group(1)
                    continue
                if current_package:
                    ver_match = re.match(r'^\s+version:\s*"([^"]+)"', line)
                    if ver_match:
                        pkg_info.append((current_package, ver_match.group(1), None))
                        current_package = None
        return pkg_info

    # --- Elixir ---

    @staticmethod
    def _parse_mix_exs(file_content: str) -> list[tuple]:
        """Parse mix.exs file."""
        pkg_info: list[tuple] = []
        for m in re.finditer(r'\{\s*:([A-Za-z0-9_]+)\s*,\s*"([^"]+)"', file_content):
            pkg, ver = m.groups()
            pkg_info.append((pkg, ver.lstrip("~><="), None))
        return pkg_info

    @staticmethod
    def _parse_mix_lock(file_content: str) -> list[tuple]:
        """Parse mix.lock file."""
        pkg_info: list[tuple] = []
        for m in re.finditer(
            r'"([A-Za-z0-9_]+)":\s*\{:hex,\s*:[A-Za-z0-9_]+,\s*"([0-9.]+)"',
            file_content,
        ):
            pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    # --- Swift ---

    @staticmethod
    def _parse_package_swift(file_content: str) -> list[tuple]:
        """Parse Package.swift file."""
        pkg_info: list[tuple] = []
        for m in re.finditer(
            r'\.package\s*\(.*?url:\s*"https://github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)'
            r'.*?from:\s*"([0-9.]+)"',
            file_content,
            re.DOTALL,
        ):
            repo, ver = m.groups()
            pkg_info.append((f"github.com/{repo}", ver, None))
        for m in re.finditer(
            r'\.package\s*\(.*?url:\s*"https://github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)'
            r'.*?\.exact\("([0-9.]+)"\)',
            file_content,
            re.DOTALL,
        ):
            repo, ver = m.groups()
            pkg_info.append((f"github.com/{repo}", ver, None))
        return pkg_info

    @staticmethod
    def _parse_package_resolved(file_content: str) -> list[tuple]:
        """Parse Package.resolved file."""
        pkg_info: list[tuple] = []
        try:
            resolved_data = json.loads(file_content)
            pins = resolved_data.get("pins", []) or resolved_data.get("object", {}).get("pins", [])
            for pin in pins:
                if isinstance(pin, dict):
                    identity = pin.get("identity") or pin.get("package")
                    location = pin.get("location", "")
                    state = pin.get("state", {})
                    version = state.get("version")
                    branch = state.get("branch")
                    revision = state.get("revision")
                    version_id = None
                    if version:
                        version_id = version
                    elif branch:
                        version_id = f"branch:{branch}"
                    elif revision:
                        version_id = revision[:12] if len(revision) > 12 else revision
                    package_name = identity
                    if location:
                        github_match = re.search(r"github\.com[:/]([^/]+/[^/]+)", location)
                        if github_match:
                            repo_path = github_match.group(1).rstrip(".git")
                            package_name = f"github.com/{repo_path}"
                    if identity and version_id:
                        pkg_info.append((package_name, version_id, None))
        except Exception:
            pass
        return pkg_info

    # --- CocoaPods / Carthage ---

    @staticmethod
    def _parse_podfile(file_content: str) -> list[tuple]:
        """Parse Podfile."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            m = re.match(r"""^\s*pod\s+['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]""", line)
            if m:
                pkg_info.append((m.group(1), m.group(2).lstrip("~><="), None))
        return pkg_info

    @staticmethod
    def _parse_podfile_lock(file_content: str) -> list[tuple]:
        """Parse Podfile.lock."""
        pkg_info: list[tuple] = []
        in_pods = False
        for line in file_content.splitlines():
            if line.strip() == "PODS:":
                in_pods = True
                continue
            if in_pods:
                if line and not line.startswith(" "):
                    in_pods = False
                    continue
                m = re.match(r"^\s*-\s+([A-Za-z0-9_\-]+)\s+\(([0-9.]+)\)", line)
                if m:
                    pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    @staticmethod
    def _parse_cartfile(file_content: str) -> list[tuple]:
        """Parse Cartfile."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            m = re.match(r'^\s*github\s+"([^/]+/[^"]+)"\s+[~=]?>=?\s*([0-9.]+)', line)
            if m:
                pkg_info.append((f"github.com/{m.group(1)}", m.group(2), None))
                continue
            m2 = re.match(r'^\s*git\s+"([^"]+)"\s+[~=]?>=?\s*([0-9.]+)', line)
            if m2:
                repo_name = m2.group(1).split("/")[-1].replace(".git", "")
                pkg_info.append((repo_name, m2.group(2), None))
        return pkg_info

    @staticmethod
    def _parse_cartfile_resolved(file_content: str) -> list[tuple]:
        """Parse Cartfile.resolved."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            m = re.match(r'^\s*github\s+"([^/]+/[^"]+)"\s+"([0-9.]+)"', line)
            if m:
                pkg_info.append((f"github.com/{m.group(1)}", m.group(2), None))
                continue
            m2 = re.match(r'^\s*git\s+"([^"]+)"\s+"([0-9.]+)"', line)
            if m2:
                repo_name = m2.group(1).split("/")[-1].replace(".git", "")
                pkg_info.append((repo_name, m2.group(2), None))
        return pkg_info

    # --- Haskell ---

    @staticmethod
    def _parse_stack_yaml(file_content: str) -> list[tuple]:
        """Parse stack.yaml file."""
        pkg_info: list[tuple] = []
        in_deps = False
        for line in file_content.splitlines():
            if line.strip().startswith("extra-deps:"):
                in_deps = True
                continue
            if in_deps:
                if line and not line.startswith(" ") and not line.startswith("-"):
                    in_deps = False
                    continue
                m = re.match(r"^\s*-\s*([A-Za-z0-9_\-]+)-([0-9][0-9.]*)", line)
                if m:
                    pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    @staticmethod
    def _parse_cabal(file_content: str) -> list[tuple]:
        """Parse .cabal file."""
        pkg_info: list[tuple] = []
        in_deps = False
        for line in file_content.splitlines():
            if line.strip().startswith("build-depends:"):
                in_deps = True
                deps_str = line.split(":", 1)[1] if ":" in line else ""
                for dep in deps_str.split(","):
                    m = re.match(r"\s*([A-Za-z0-9_\-]+)\s*>=?\s*([0-9][0-9.]*)", dep)
                    if m:
                        pkg_info.append((m.group(1), m.group(2), None))
                continue
            if in_deps:
                if line and not line.startswith(" "):
                    in_deps = False
                    continue
                for dep in line.split(","):
                    m = re.match(r"\s*([A-Za-z0-9_\-]+)\s*>=?\s*([0-9][0-9.]*)", dep)
                    if m:
                        pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    # --- Julia ---

    @staticmethod
    def _parse_julia_project_toml(file_content: str) -> list[tuple]:
        """Parse Julia Project.toml file."""
        pkg_info: list[tuple] = []
        in_deps = False
        for line in file_content.splitlines():
            if line.strip() == "[deps]":
                in_deps = True
                continue
            if in_deps:
                if line.startswith("["):
                    break
                m = re.match(r"^([A-Za-z0-9_]+)\s*=", line)
                if m:
                    pkg_info.append((m.group(1), None, None))
        return pkg_info

    @staticmethod
    def _parse_julia_manifest_toml(file_content: str) -> list[tuple]:
        """Parse Julia Manifest.toml file."""
        pkg_info: list[tuple] = []
        current_package = None
        for line in file_content.splitlines():
            pkg_match = re.match(r"^\[\[(?:deps\.)?([A-Za-z0-9_]+)\]\]", line)
            if pkg_match:
                current_package = pkg_match.group(1)
                continue
            if current_package:
                ver_match = re.match(r'^version\s*=\s*"([^"]+)"', line)
                if ver_match:
                    pkg_info.append((current_package, ver_match.group(1), None))
                    current_package = None
        return pkg_info

    # --- C++ ---

    @staticmethod
    def _parse_vcpkg_json(file_content: str) -> list[tuple]:
        """Parse vcpkg.json file."""
        pkg_info: list[tuple] = []
        try:
            vcpkg_data = json.loads(file_content)
            deps = vcpkg_data.get("dependencies", [])
            for dep in deps:
                if isinstance(dep, str):
                    pkg_info.append((dep, None, None))
                elif isinstance(dep, dict):
                    name = dep.get("name")
                    version = dep.get("version>=") or dep.get("version") or dep.get("version-string")
                    if name:
                        pkg_info.append((name, version, None))
        except Exception:
            pass
        return pkg_info

    @staticmethod
    def _parse_conanfile_txt(file_content: str) -> list[tuple]:
        """Parse conanfile.txt file."""
        pkg_info: list[tuple] = []
        in_requires = False
        for line in file_content.splitlines():
            if line.strip() == "[requires]":
                in_requires = True
                continue
            if in_requires:
                if line.startswith("["):
                    break
                m = re.match(r"^\s*([A-Za-z0-9_\-]+)/([0-9][0-9.\-]*)(?:@[^\s]*)?", line)
                if m:
                    pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    @staticmethod
    def _parse_conanfile_py(file_content: str) -> list[tuple]:
        """Parse conanfile.py file."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            if "requires" in line and "=" in line:
                for m in re.finditer(r'["\']([A-Za-z0-9_\-]+)/([0-9][0-9.\-]*)["\']', line):
                    pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    @staticmethod
    def _parse_cmake_lists(file_content: str) -> list[tuple]:
        """Parse CMakeLists.txt file."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            m = re.match(
                r"^\s*find_package\s*\(\s*([A-Za-z0-9_]+)(?:\s+([0-9][0-9.]*))?",
                line,
                re.IGNORECASE,
            )
            if m:
                pkg_info.append((m.group(1), m.group(2) if m.group(2) else None, None))
        return pkg_info

    # --- Deno ---

    @staticmethod
    def _parse_deno_json(file_content: str) -> list[tuple]:
        """Parse deno.json or deno.jsonc file."""
        pkg_info: list[tuple] = []
        try:
            content_to_parse = file_content
            if file_content.startswith("//") or "/*" in file_content:
                content_to_parse = re.sub(r"//.*$", "", file_content, flags=re.MULTILINE)
                content_to_parse = re.sub(r"/\*.*?\*/", "", content_to_parse, flags=re.DOTALL)
            deno_data = json.loads(content_to_parse)
            imports = deno_data.get("imports", {})
            for _alias, url in imports.items():
                if url.startswith("npm:"):
                    pkg_with_ver = url[4:]
                    if "@" in pkg_with_ver:
                        if pkg_with_ver.startswith("@"):
                            parts = pkg_with_ver.split("@")
                            if len(parts) >= 3:
                                pkg_info.append((f"@{parts[1]}", parts[2], None))
                        else:
                            parts = pkg_with_ver.split("@")
                            pkg_info.append((parts[0], parts[1] if len(parts) > 1 else None, None))
                elif "esm.sh" in url:
                    m = re.search(r"esm\.sh/([^@/]+)@([0-9][0-9.\-]*)", url)
                    if m:
                        pkg_info.append((m.group(1), m.group(2), None))
        except Exception:
            pass
        return pkg_info

    @staticmethod
    def _parse_deno_lock(file_content: str) -> list[tuple]:
        """Parse deno.lock file."""
        pkg_info: list[tuple] = []
        try:
            lock_data = json.loads(file_content)
            npm_section = lock_data.get("npm", {})
            specifiers = npm_section.get("specifiers", {})
            for _alias, full_spec in specifiers.items():
                if "@" in full_spec:
                    parts = full_spec.split("@")
                    if len(parts) >= 2:
                        version = parts[-1]
                        pkg_name = "@".join(parts[:-1])
                        pkg_info.append((pkg_name, version, None))
            remote_section = lock_data.get("remote", {})
            std_versions: set[str] = set()
            for url in remote_section.keys():
                m = re.search(r"deno\.land/std@([0-9][0-9.\-]*)", url)
                if m:
                    std_versions.add(m.group(1))
            for ver in std_versions:
                pkg_info.append(("deno_std", ver, None))
        except Exception:
            pass
        return pkg_info

    # --- Perl ---

    @staticmethod
    def _parse_cpanfile(file_content: str) -> list[tuple]:
        """Parse cpanfile."""
        pkg_info: list[tuple] = []
        cpan_pattern = re.compile(
            r"""^\s*(?:on\s+['"][^'"]+['"]\s+)?"""
            r"""(requires|recommends|suggests|feature|test_requires)\s+"""
            r"""['"]([A-Za-z0-9_:]+)['"]"""
            r"""(?:\s*(?:=>|,)\s*['"]([^'"]+)['"])?""",
            re.MULTILINE,
        )
        for match in cpan_pattern.finditer(file_content):
            pkg_info.append((match.group(2), match.group(3), None))
        return pkg_info

    @staticmethod
    def _parse_makefile_pl(file_content: str) -> list[tuple]:
        """Parse Makefile.PL or Build.PL."""
        pkg_info: list[tuple] = []
        pattern = re.compile(
            r"""^\s*(requires|recommends|test_requires|build_requires|configure_requires)\s+"""
            r"""['"]([A-Za-z0-9_:]+)['"]"""
            r"""\s*=>\s*['"]?([0-9][0-9a-zA-Z._-]*|0)['"]?""",
            re.MULTILINE,
        )
        for match in pattern.finditer(file_content):
            module = match.group(2)
            version = match.group(3)
            version = None if version == "0" else version
            pkg_info.append((module, version, None))
        return pkg_info

    # --- R ---

    @staticmethod
    def _parse_r_description(file_content: str) -> list[tuple]:
        """Parse R DESCRIPTION file."""
        pkg_info: list[tuple] = []
        in_imports = False
        in_depends = False
        for line in file_content.splitlines():
            if line.startswith("Imports:"):
                in_imports = True
                line = line.replace("Imports:", "")
            elif line.startswith("Depends:"):
                in_depends = True
                line = line.replace("Depends:", "")
            elif line and not line.startswith(" "):
                in_imports = False
                in_depends = False
                continue
            if in_imports or in_depends:
                for pkg_str in line.split(","):
                    m = re.match(r"\s*([A-Za-z0-9._]+)\s*(?:\(>=?\s*([0-9][0-9.]*)\))?", pkg_str)
                    if m:
                        pkg_name = m.group(1)
                        if pkg_name.lower() == "r":
                            continue
                        pkg_info.append((pkg_name, m.group(2), None))
        return pkg_info

    @staticmethod
    def _parse_renv_lock(file_content: str) -> list[tuple]:
        """Parse renv.lock file."""
        pkg_info: list[tuple] = []
        try:
            lock_data = json.loads(file_content)
            packages = lock_data.get("Packages", {})
            for pkg_name, pkg_data in packages.items():
                if isinstance(pkg_data, dict):
                    version = pkg_data.get("Version")
                    if version:
                        pkg_info.append((pkg_name, version, None))
        except Exception:
            pass
        return pkg_info

    @staticmethod
    def _parse_packrat_lock(file_content: str) -> list[tuple]:
        """Parse packrat.lock file."""
        pkg_info: list[tuple] = []
        current_package = None
        for line in file_content.splitlines():
            pkg_match = re.match(r"^Package:\s*([A-Za-z0-9._]+)", line)
            if pkg_match:
                current_package = pkg_match.group(1)
                continue
            if current_package:
                ver_match = re.match(r"^Version:\s*([0-9][0-9.\-]*)", line)
                if ver_match:
                    pkg_info.append((current_package, ver_match.group(1), None))
                    current_package = None
        return pkg_info

    # --- Lua ---

    @staticmethod
    def _parse_rockspec(file_content: str) -> list[tuple]:
        """Parse .rockspec file."""
        pkg_info: list[tuple] = []
        in_deps = False
        for line in file_content.splitlines():
            if "dependencies" in line and "=" in line:
                in_deps = True
                continue
            if in_deps:
                if line.strip() == "}":
                    break
                m = re.match(r"""^\s*['"]([A-Za-z0-9_\-]+)\s*[>~]=?\s*([0-9][0-9.]*)['"]""", line)
                if m:
                    if m.group(1).lower() != "lua":
                        pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    # --- Clojure ---

    @staticmethod
    def _parse_project_clj(file_content: str) -> list[tuple]:
        """Parse project.clj file."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            m = re.match(r'^\s*\[([A-Za-z0-9_\-./]+)\s+"([^"]+)"\]', line)
            if m:
                pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    @staticmethod
    def _parse_deps_edn(file_content: str) -> list[tuple]:
        """Parse deps.edn file."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            m = re.match(r'^\s*([A-Za-z0-9_\-./]+)\s+\{[^}]*:mvn/version\s+"([^"]+)"', line)
            if m:
                pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    # --- OCaml ---

    @staticmethod
    def _parse_opam(file_content: str) -> list[tuple]:
        """Parse .opam or opam file."""
        pkg_info: list[tuple] = []
        in_depends = False
        for line in file_content.splitlines():
            if line.strip().startswith("depends:"):
                in_depends = True
                continue
            if in_depends:
                if line.strip() == "]":
                    break
                m = re.match(r'^\s*"([A-Za-z0-9_\-]+)"\s*\{[^}]*>=?\s*"([^"]+)"', line)
                if m:
                    if m.group(1) != "ocaml":
                        pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    @staticmethod
    def _parse_dune_project(file_content: str) -> list[tuple]:
        """Parse dune-project file."""
        pkg_info: list[tuple] = []
        in_depends = False
        for line in file_content.splitlines():
            if line.strip().startswith("(depends"):
                in_depends = True
                continue
            if in_depends:
                if line.strip() == ")" and not line.strip().startswith("("):
                    break
                m = re.match(r"^\s*\(([A-Za-z0-9_\-]+)\s*\([^)]*>=?\s*([0-9][0-9.]*)", line)
                if m:
                    if m.group(1) != "ocaml":
                        pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    # --- Nim ---

    @staticmethod
    def _parse_nimble(file_content: str) -> list[tuple]:
        """Parse .nimble file."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            m = re.match(r'^\s*requires\s+"([A-Za-z0-9_\-]+)\s*>=?\s*([0-9][0-9.]*)"', line)
            if m:
                if m.group(1).lower() != "nim":
                    pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    # --- Zig ---

    @staticmethod
    def _parse_build_zig_zon(file_content: str) -> list[tuple]:
        """Parse build.zig.zon file."""
        pkg_info: list[tuple] = []
        in_dependencies = False
        for line in file_content.splitlines():
            stripped = line.strip()
            if ".dependencies" in stripped and "=" in stripped:
                in_dependencies = True
                continue
            if in_dependencies:
                if stripped in ("},", "}"):
                    in_dependencies = False
                    continue
                pkg_match = re.match(r"^\.([A-Za-z0-9_\-]+)\s*=", stripped)
                if pkg_match:
                    pkg_info.append((pkg_match.group(1), None, None))
        return pkg_info

    # --- Erlang ---

    @staticmethod
    def _parse_rebar_config(file_content: str) -> list[tuple]:
        """Parse rebar.config file."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            m = re.match(r'^\s*\{([A-Za-z0-9_\-]+),\s*"([0-9][0-9.]*)"', line)
            if m:
                pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    @staticmethod
    def _parse_rebar_lock(file_content: str) -> list[tuple]:
        """Parse rebar.lock file."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            m = re.match(r'^\s*\{([A-Za-z0-9_\-]+),\s*"([0-9][0-9.]*)"', line)
            if m:
                pkg_info.append((m.group(1), m.group(2), None))
        return pkg_info

    # --- GitHub Actions ---

    @staticmethod
    def _parse_github_actions_workflow(file_content: str) -> list[tuple]:
        """Parse GitHub Actions workflow files."""
        pkg_info: list[tuple] = []
        for line in file_content.splitlines():
            m = re.match(r"^\s*uses:\s*([^@\s]+)@([^\s]+)", line)
            if m:
                action = m.group(1)
                version = m.group(2)
                if re.match(r"^v?\d", version) or version in ("main", "master"):
                    pkg_info.append((action, version, None))
        return pkg_info


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — Source Import Extractors (ported from ExtensionPackageParser)
# ═══════════════════════════════════════════════════════════════════════════

class SourceImportExtractor:
    """
    Regex-based extraction of external package names from source files,
    supporting 25+ languages.

    Ported from ``ExtensionPackageParser`` with no Django dependencies.
    """

    @staticmethod
    def parse_packages(extension: str, file_content: str) -> list[str]:
        """Route to the correct language-specific parser by extension."""
        try:
            extension = extension.lower()
            parser_map = {
                ".py": SourceImportExtractor._parse_python,
                ".pyx": SourceImportExtractor._parse_python,
                ".pyi": SourceImportExtractor._parse_python,
                ".js": SourceImportExtractor._parse_js_ts,
                ".jsx": SourceImportExtractor._parse_js_ts,
                ".mjs": SourceImportExtractor._parse_js_ts,
                ".cjs": SourceImportExtractor._parse_js_ts,
                ".ts": SourceImportExtractor._parse_js_ts,
                ".tsx": SourceImportExtractor._parse_js_ts,
                ".go": SourceImportExtractor._parse_go,
                ".java": SourceImportExtractor._parse_java,
                ".php": SourceImportExtractor._parse_php,
                ".php5": SourceImportExtractor._parse_php,
                ".phps": SourceImportExtractor._parse_php,
                ".phtml": SourceImportExtractor._parse_php,
                ".rb": SourceImportExtractor._parse_ruby,
                ".rs": SourceImportExtractor._parse_rust,
                ".cs": SourceImportExtractor._parse_csharp,
                ".swift": SourceImportExtractor._parse_swift,
                ".kt": SourceImportExtractor._parse_kotlin,
                ".kts": SourceImportExtractor._parse_kotlin,
                ".scala": SourceImportExtractor._parse_scala,
                ".dart": SourceImportExtractor._parse_dart,
                ".c": SourceImportExtractor._parse_cpp,
                ".cc": SourceImportExtractor._parse_cpp,
                ".cpp": SourceImportExtractor._parse_cpp,
                ".h": SourceImportExtractor._parse_cpp,
                ".hpp": SourceImportExtractor._parse_cpp,
                ".gradle": SourceImportExtractor._parse_gradle,
                ".ex": SourceImportExtractor._parse_elixir,
                ".exs": SourceImportExtractor._parse_elixir,
                ".hs": SourceImportExtractor._parse_haskell,
                ".r": SourceImportExtractor._parse_r,
                ".lua": SourceImportExtractor._parse_lua,
                ".pl": SourceImportExtractor._parse_perl,
                ".pm": SourceImportExtractor._parse_perl,
                ".m": SourceImportExtractor._parse_objc,
                ".mm": SourceImportExtractor._parse_objc,
                ".cmake": SourceImportExtractor._parse_cmake,
            }
            parser_fn = parser_map.get(extension)
            return parser_fn(file_content) if parser_fn else []
        except Exception:
            return []

    # --- Language parsers ---

    @staticmethod
    def _parse_python(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"^\s*import\s+([a-zA-Z0-9_.]+)", content, re.MULTILINE):
            root = match.group(1).split(",")[0].strip().split(".")[0]
            if root and not root.startswith("."):
                imports.add(root)
        for match in re.finditer(r"^\s*from\s+([a-zA-Z0-9_.]+)\s+import", content, re.MULTILINE):
            root = match.group(1).split(".")[0]
            if root and not root.startswith("."):
                imports.add(root)
        return list(imports)

    @staticmethod
    def _parse_js_ts(content: str) -> list[str]:
        imports: set[str] = set()

        def _is_internal(pkg: str) -> bool:
            if pkg.startswith((".", "/", "#")):
                return True
            if pkg.startswith("@/") or pkg.startswith("~/"):
                return True
            if "://" in pkg:
                return True
            return False

        def _root_pkg(pkg: str) -> str | None:
            if pkg.startswith("@"):
                parts = pkg.split("/")
                return f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else None
            return pkg.split("/")[0]

        for match in re.finditer(r'''require\(["']([^"']+)["']\)''', content):
            pkg = match.group(1)
            if not _is_internal(pkg):
                root = _root_pkg(pkg)
                if root:
                    imports.add(root)

        for match in re.finditer(r'''import[^;]*?from\s+["']([^"']+)["']''', content):
            pkg = match.group(1)
            if not _is_internal(pkg):
                root = _root_pkg(pkg)
                if root:
                    imports.add(root)

        for match in re.finditer(r'''import\s+["']([^"']+)["']''', content):
            pkg = match.group(1)
            if not _is_internal(pkg):
                root = _root_pkg(pkg)
                if root:
                    imports.add(root)

        return list(imports)

    @staticmethod
    def _root_go_path(path: str) -> str:
        parts = path.split("/")
        return "/".join(parts[:3] if len(parts) >= 3 else parts)

    @staticmethod
    def _parse_go(content: str) -> list[str]:
        imports: set[str] = set()
        single_re = re.compile(
            r'^\s*import\s+(?:[A-Za-z_][A-Za-z0-9_]*\s+)?"([^"]+)"',
            re.MULTILINE,
        )
        for match in single_re.finditer(content):
            path = match.group(1)
            if "/" in path:
                imports.add(SourceImportExtractor._root_go_path(path))
            else:
                imports.add(path)

        block_match = re.search(r"^\s*import\s*\((.*?)\)", content, re.DOTALL | re.MULTILINE)
        if block_match:
            for raw_line in block_match.group(1).split("\n"):
                line = raw_line.strip()
                if not line:
                    continue
                m = re.match(r'(?:[A-Za-z_][A-Za-z0-9_]*\s+)?"([^"]+)"', line)
                if not m:
                    continue
                path = m.group(1)
                if path.startswith((".", "/")):
                    continue
                if "/" in path:
                    imports.add(SourceImportExtractor._root_go_path(path))
                else:
                    imports.add(path)
        return list(imports)

    @staticmethod
    def _parse_java(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"^\s*import\s+([a-zA-Z0-9_.]+);", content, re.MULTILINE):
            root = ".".join(match.group(1).split(".")[:3])
            imports.add(root)
        return list(imports)

    @staticmethod
    def _parse_php(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"^\s*use\s+([A-Za-z0-9_\\\\]+)", content, re.MULTILINE):
            vendor = match.group(1).strip("\\").split("\\")[0]
            if vendor:
                imports.add(vendor)
        for match in re.finditer(
            r"""\b(?:require|include)(?:_once)?\s*\(\s*["']([^"']+)["']""", content
        ):
            path = match.group(1)
            if not path.startswith((".", "/")) and "/" in path:
                imports.add(path.split("/")[0])
        return list(imports)

    @staticmethod
    def _parse_ruby(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"""^\s*require\s+["']([^"']+)["']""", content, re.MULTILINE):
            gem = match.group(1)
            if not gem.startswith("."):
                imports.add(gem.split("/")[-1])
        return list(imports)

    @staticmethod
    def _parse_rust(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"^\s*extern\s+crate\s+([A-Za-z0-9_]+);", content, re.MULTILINE):
            imports.add(match.group(1))
        for match in re.finditer(r"^\s*use\s+([A-Za-z0-9_]+)::", content, re.MULTILINE):
            imports.add(match.group(1))
        return list(imports)

    @staticmethod
    def _parse_csharp(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"^\s*using\s+([A-Za-z0-9_.]+);", content, re.MULTILINE):
            root = match.group(1).split(".")[0]
            if root:
                imports.add(root)
        return list(imports)

    @staticmethod
    def _parse_swift(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"^\s*import\s+([A-Za-z0-9_]+)", content, re.MULTILINE):
            imports.add(match.group(1))
        return list(imports)

    @staticmethod
    def _parse_kotlin(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"^\s*import\s+([a-zA-Z0-9_.]+)", content, re.MULTILINE):
            root = ".".join(match.group(1).split(".")[:3])
            imports.add(root)
        return list(imports)

    @staticmethod
    def _parse_scala(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"^\s*import\s+([a-zA-Z0-9_.]+)", content, re.MULTILINE):
            root = ".".join(match.group(1).split(".")[:3])
            imports.add(root)
        return list(imports)

    @staticmethod
    def _parse_dart(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"""\bimport\s+["']package:([^/'"]+)""", content):
            imports.add(match.group(1))
        return list(imports)

    @staticmethod
    def _parse_cpp(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r'^\s*#include\s+[<"]([^">]+)[">]', content, re.MULTILINE):
            header = match.group(1)
            if "/" in header:
                imports.add(header.split("/")[0])
            else:
                imports.add(header.split(".")[0])
        return list(imports)

    @staticmethod
    def _parse_gradle(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(
            r"""\b(?:implementation|api|compile|compileOnly|testImplementation|runtimeOnly|kapt)\s+['"]([^:'"]+):([^:'"]+):[^'"]+['"]""",
            content,
        ):
            imports.add(match.group(2))
        return list(imports)

    @staticmethod
    def _parse_elixir(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"\{\s*:(\w+)\s*,", content):
            imports.add(match.group(1))
        return list(imports)

    @staticmethod
    def _parse_haskell(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(
            r"^\s*import\s+(?:qualified\s+)?([A-Z][A-Za-z0-9_.]+)", content, re.MULTILINE
        ):
            imports.add(match.group(1).split(".")[0])
        return list(imports)

    @staticmethod
    def _parse_r(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(
            r"""\b(?:library|require)\s*\(\s*["']?([A-Za-z0-9_.]+)["']?\s*\)""", content
        ):
            imports.add(match.group(1))
        return list(imports)

    @staticmethod
    def _parse_lua(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"""\brequire\s*\(?\s*["']([^"']+)["']""", content):
            module = match.group(1)
            if not module.startswith((".", "/")):
                imports.add(module.split(".")[0])
        return list(imports)

    @staticmethod
    def _parse_perl(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"^\s*use\s+([A-Za-z0-9_:]+)", content, re.MULTILINE):
            imports.add(match.group(1))
        return list(imports)

    @staticmethod
    def _parse_objc(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r'^\s*#import\s+[<"]([^">]+)[">]', content, re.MULTILINE):
            header = match.group(1)
            if "/" in header:
                imports.add(header.split("/")[0])
            else:
                imports.add(header.split(".")[0])
        return list(imports)

    @staticmethod
    def _parse_cmake(content: str) -> list[str]:
        imports: set[str] = set()
        for match in re.finditer(r"\bfind_package\s*\(\s*([A-Za-z0-9_]+)", content):
            imports.add(match.group(1))
        return list(imports)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — Vulnerability Scanner (ported from vulnerability_scanner.py)
# ═══════════════════════════════════════════════════════════════════════════

# Default empty-result template
_EMPTY_VULN: Dict[str, object] = {
    "vulnerability_count": 0,
    "critical_vulnerabilities": 0,
    "high_vulnerabilities": 0,
    "medium_vulnerabilities": 0,
    "low_vulnerabilities": 0,
    "vulnerability_summary": "No known vulnerabilities",
    "latest_vulnerability_date": None,
    "vulnerability_ids": "",
    "vulnerability_details": "",
}


def _process_osv_response(response_data: dict) -> dict:
    """Parse an OSV single-package response into a structured vuln summary."""
    if not response_data or "vulns" not in response_data:
        return dict(_EMPTY_VULN)

    vulns = response_data["vulns"]
    vulnerability_count = len(vulns)
    critical_count = high_count = medium_count = low_count = 0
    vuln_ids: list[str] = []
    vuln_details: list[str] = []
    latest_date: datetime | None = None

    for vuln in vulns:
        vuln_id = vuln.get("id", "")
        vuln_ids.append(vuln_id)

        summary = vuln.get("summary", "")
        if summary:
            vuln_details.append(f"{vuln_id}: {summary[:200]}")

        published = vuln.get("published")
        if published:
            try:
                vuln_date = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if latest_date is None or vuln_date > latest_date:
                    latest_date = vuln_date
            except Exception:
                pass

        severity = None
        if "severity" in vuln:
            for sev_entry in vuln["severity"]:
                if sev_entry.get("type") == "CVSS_V3":
                    score = sev_entry.get("score")
                    if score:
                        try:
                            cvss = float(score)
                            if cvss >= 9.0:
                                severity = "CRITICAL"
                            elif cvss >= 7.0:
                                severity = "HIGH"
                            elif cvss >= 4.0:
                                severity = "MEDIUM"
                            else:
                                severity = "LOW"
                            break
                        except ValueError:
                            pass

        if not severity and "database_specific" in vuln:
            db_sev = vuln["database_specific"].get("severity", "").upper()
            if db_sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
                severity = db_sev

        if severity == "CRITICAL":
            critical_count += 1
        elif severity == "HIGH":
            high_count += 1
        elif severity == "MEDIUM":
            medium_count += 1
        elif severity == "LOW":
            low_count += 1
        else:
            medium_count += 1

    if critical_count > 0:
        summary_str = f"{critical_count} critical, {high_count} high severity vulnerabilities found"
    elif high_count > 0:
        summary_str = f"{high_count} high, {medium_count} medium severity vulnerabilities found"
    elif medium_count > 0:
        summary_str = f"{medium_count} medium, {low_count} low severity vulnerabilities found"
    elif low_count > 0:
        summary_str = f"{low_count} low severity vulnerabilities found"
    else:
        summary_str = f"{vulnerability_count} vulnerabilities found"

    return {
        "vulnerability_count": vulnerability_count,
        "critical_vulnerabilities": critical_count,
        "high_vulnerabilities": high_count,
        "medium_vulnerabilities": medium_count,
        "low_vulnerabilities": low_count,
        "vulnerability_summary": summary_str,
        "latest_vulnerability_date": latest_date.isoformat() if latest_date else None,
        "vulnerability_ids": ", ".join(vuln_ids[:10]) + ("..." if len(vuln_ids) > 10 else ""),
        "vulnerability_details": " | ".join(vuln_details[:5]) + ("..." if len(vuln_details) > 5 else ""),
    }


class VulnerabilityScanner:
    """
    Query the OSV public API for known vulnerabilities.

    Uses ``httpx`` for HTTP calls (replacing server-side ``requests``).
    All configuration (URLs, retries, ecosystem mappings) is supplied
    via the *config* dict fetched from the server.
    """

    def __init__(self, config: dict, on_debug: Callable[[str], None] | None = None):
        self._osv_api_url = config.get("osv_api_url", "https://api.osv.dev/v1/query")
        self._osv_batch_url = config.get("osv_batch_url", "https://api.osv.dev/v1/querybatch")
        self._max_retries = config.get("max_retries", 3)
        self._retry_backoff = config.get("retry_backoff", 1.5)
        self._ecosystem_map: dict[str, str] = config.get("osv_ecosystem_map", {})
        self._prefix_to_registry: dict[str, str] = config.get("prefix_to_registry", {})
        self._debug = on_debug or (lambda _: None)

    def _http_post_json(self, url: str, data: dict) -> dict:
        """HTTP POST with retry and exponential back-off."""
        attempt = 0
        while attempt < self._max_retries:
            try:
                resp = httpx.post(url, json=data, timeout=30.0)
                if resp.status_code == 200:
                    return resp.json()
            except Exception as exc:
                logger.debug("HTTP POST failed for %s (attempt %d): %s", url, attempt + 1, exc)
            attempt += 1
            if attempt < self._max_retries:
                time.sleep(self._retry_backoff * (2 ** (attempt - 1)))
        return {}

    def _strip_prefix(self, package_name: str) -> tuple[str, str]:
        """Remove our internal registry prefix and return (bare_name, registry)."""
        for prefix, registry in self._prefix_to_registry.items():
            if package_name.startswith(prefix):
                return package_name[len(prefix):], registry
        return package_name, "unknown"

    def scan_batch(self, packages: list[dict]) -> dict[str, dict]:
        """
        Scan a list of packages for vulnerabilities via OSV batch API.

        Each entry in *packages* must have ``prefixed_name`` and optionally
        ``version``.  Returns a dict keyed by ``prefixed_name``.
        """
        results: dict[str, dict] = {}
        if not packages:
            return results

        # Build batch queries grouped by ecosystem
        queries: list[dict] = []
        query_keys: list[str] = []

        for pkg in packages:
            prefixed = pkg.get("prefixed_name", "")
            version = pkg.get("version")
            bare_name, registry = self._strip_prefix(prefixed)
            ecosystem = self._ecosystem_map.get(registry)

            if not ecosystem:
                results[prefixed] = dict(_EMPTY_VULN)
                continue

            query: dict = {"package": {"name": bare_name, "ecosystem": ecosystem}}
            if version and version.strip() and version != "latest":
                query["version"] = version.strip()

            queries.append(query)
            query_keys.append(prefixed)

        if not queries:
            return results

        # OSV batch API accepts up to 1000 queries per request
        batch_size = 1000
        for i in range(0, len(queries), batch_size):
            batch_queries = queries[i: i + batch_size]
            batch_keys = query_keys[i: i + batch_size]

            self._debug(f"Querying OSV for {len(batch_queries)} packages (batch {i // batch_size + 1})")

            batch_data = {"queries": batch_queries}
            resp_data = self._http_post_json(self._osv_batch_url, batch_data)

            if not resp_data or "results" not in resp_data:
                for key in batch_keys:
                    results[key] = dict(_EMPTY_VULN)
                    results[key]["vulnerability_summary"] = "Batch query failed"
                continue

            for idx, raw_result in enumerate(resp_data["results"]):
                key = batch_keys[idx]
                if raw_result and "vulns" in raw_result:
                    results[key] = _process_osv_response(raw_result)
                else:
                    results[key] = dict(_EMPTY_VULN)

            # Brief pause between batches to respect rate limits
            if i + batch_size < len(queries):
                time.sleep(0.5)

        return results


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — Orchestrator (LocalDependencyScanner)
# ═══════════════════════════════════════════════════════════════════════════

class LocalDependencyScanner:
    """
    High-level scanner that orchestrates manifest parsing, source-import
    extraction, and OSV vulnerability scanning — all running locally.

    ``config`` is fetched from the server's hidden-config endpoint
    (``GET /api/cli/audit/dependency-config/``) and contains registry
    mappings, OSV settings, etc.
    """

    def __init__(
        self,
        config: dict,
        on_debug: Callable[[str], None] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ):
        self._config = config
        self._registry_prefix_map: dict[str, str] = config.get("registry_prefix_map", {})
        self._dep_file_prefixes: dict[str, str] = config.get("dependency_file_prefixes", {})
        self._debug = on_debug or (lambda _: None)
        self._progress = on_progress or (lambda _: None)
        self._vuln_scanner = VulnerabilityScanner(
            config=config.get("vuln_scan", {}),
            on_debug=on_debug,
        )

    # ----- Manifest scanning -----

    def scan_manifests(
        self,
        scope_dirs: dict[str, str],
    ) -> list[ManifestResult]:
        """
        Walk local directories, find dependency manifest files, and parse them.

        Args:
            scope_dirs: Mapping of ``repo_name → local_path``.

        Returns:
            One ``ManifestResult`` per manifest file found.
        """
        results: list[ManifestResult] = []

        for repo_name, local_path in scope_dirs.items():
            if not local_path or not os.path.isdir(local_path):
                self._debug(f"Skipping repo '{repo_name}': directory not found")
                continue

            manifest_paths: list[str] = []
            for root, _dirs, files in os.walk(local_path):
                # Skip common non-project directories
                rel_root = os.path.relpath(root, local_path)
                skip_dirs = {"node_modules", ".git", "__pycache__", ".tox", "venv", ".venv", "vendor", "dist", "build"}
                if any(part in skip_dirs for part in rel_root.split(os.sep)):
                    continue
                for fname in files:
                    abs_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(abs_path, local_path)
                    if ManifestParser.is_dependency_file(rel_path):
                        manifest_paths.append(abs_path)

            # Filter duplicates (prefer lock files)
            manifest_paths = ManifestParser.filter_duplicate_dependency_files(manifest_paths)
            self._debug(f"Found {len(manifest_paths)} manifest file(s) in '{repo_name}'")

            for abs_path in manifest_paths:
                rel_path = os.path.relpath(abs_path, local_path)
                try:
                    with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()

                    packages = ManifestParser.parse_dependency_file(rel_path, content)
                    registry = self._get_registry_for_manifest(rel_path)

                    pkg_dicts = [
                        {"name": name, "version": ver, "latest_version": latest}
                        for name, ver, latest in packages
                    ]

                    results.append(ManifestResult(
                        manifest_path=rel_path,
                        repo_name=repo_name,
                        registry=registry,
                        packages=pkg_dicts,
                    ))
                    self._debug(f"  {rel_path}: {len(pkg_dicts)} packages ({registry})")

                except Exception as exc:
                    results.append(ManifestResult(
                        manifest_path=rel_path,
                        repo_name=repo_name,
                        registry="unknown",
                        packages=[],
                        error=str(exc),
                    ))

        return results

    # ----- Source-import extraction -----

    def scan_source_imports(
        self,
        files: list[dict],
        scope_dirs: dict[str, str],
    ) -> list[ImportResult]:
        """
        Extract external package names from source file imports.

        Args:
            files: File dicts from the audit plan (must include
                   ``file_path``, ``relative_path``, ``repo_name``).
            scope_dirs: Mapping of ``repo_name → local_path``.

        Returns:
            One ``ImportResult`` per file that yielded imports.
        """
        results: list[ImportResult] = []

        for f in files:
            file_path = f.get("file_path", "")
            rel_path = f.get("relative_path", "")
            repo_name = f.get("repo_name", "")
            local_root = scope_dirs.get(repo_name, "")

            if not local_root:
                continue

            _, ext = os.path.splitext(rel_path)
            if not ext:
                continue

            # Get registry prefix for this extension from server config
            registry_prefix = self._registry_prefix_map.get(ext.lower(), "")
            if not registry_prefix:
                continue

            abs_path = os.path.join(local_root, rel_path)
            if not os.path.isfile(abs_path):
                continue

            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()

                packages = SourceImportExtractor.parse_packages(ext, content)
                if packages:
                    results.append(ImportResult(
                        file_path=file_path,
                        registry_prefix=registry_prefix,
                        packages=packages,
                    ))
            except Exception as exc:
                results.append(ImportResult(
                    file_path=file_path,
                    registry_prefix=registry_prefix,
                    packages=[],
                    error=str(exc),
                ))

        if results:
            total_pkgs = sum(len(r.packages) for r in results)
            self._debug(f"Extracted imports from {len(results)} file(s) ({total_pkgs} packages total)")

        return results

    # ----- Vulnerability scanning -----

    def scan_vulnerabilities(
        self,
        manifest_results: list[ManifestResult],
        import_results: list[ImportResult],
    ) -> dict[str, dict]:
        """
        Query OSV for vulnerabilities of all discovered packages.

        Returns a dict keyed by prefixed package name (e.g.
        ``"$!npm$!_react"``) with vulnerability summary dicts.
        """
        # Collect unique (prefixed_name, version) pairs
        seen: set[str] = set()
        queries: list[dict] = []

        for mr in manifest_results:
            # Map registry to prefix
            prefix = ""
            for ext_prefix, reg in (self._config.get("prefix_to_registry") or {}).items():
                if reg == mr.registry:
                    prefix = ext_prefix
                    break
            if not prefix:
                # Construct from registry name
                prefix = f"$!{mr.registry}$!_"

            for pkg in mr.packages:
                prefixed = f"{prefix}{pkg['name']}"
                if prefixed not in seen:
                    seen.add(prefixed)
                    queries.append({
                        "prefixed_name": prefixed,
                        "version": pkg.get("version"),
                    })

        for ir in import_results:
            for pkg_name in ir.packages:
                prefixed = f"{ir.registry_prefix}{pkg_name}"
                if prefixed not in seen:
                    seen.add(prefixed)
                    queries.append({"prefixed_name": prefixed, "version": None})

        self._debug(f"Scanning {len(queries)} unique packages for vulnerabilities")
        return self._vuln_scanner.scan_batch(queries)

    # ----- Helpers -----

    def _get_registry_for_manifest(self, rel_path: str) -> str:
        """Determine the package registry for a manifest file path."""
        filename = os.path.basename(rel_path)
        filename_lower = filename.lower()

        # Check server-provided mapping first
        if filename in self._dep_file_prefixes:
            return self._dep_file_prefixes[filename].lstrip("!")
        if filename_lower in self._dep_file_prefixes:
            return self._dep_file_prefixes[filename_lower].lstrip("!")

        # Fallback: check path patterns
        for pattern, prefix in self._dep_file_prefixes.items():
            if rel_path.endswith(pattern):
                return prefix.lstrip("!")

        return "unknown"
