#!/usr/bin/env python3
"""Bump package version, commit release files, and create a git tag."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = ROOT / "pyproject.toml"
INIT_PATH = ROOT / "codedd_cli" / "__init__.py"

SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
PYPROJECT_VERSION_PATTERN = re.compile(r'(?m)^(version\s*=\s*)"([^"]+)"\s*$')
INIT_VERSION_PATTERN = re.compile(r'(?m)^(__version__\s*=\s*)"([^"]+)"\s*$')


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def validate_git_repo() -> None:
    result = run_git(["rev-parse", "--is-inside-work-tree"])
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise RuntimeError("Not inside a git repository.")


def ensure_tag_does_not_exist(tag_name: str) -> None:
    result = run_git(["tag", "--list", tag_name])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Failed to list git tags.")
    if result.stdout.strip():
        raise RuntimeError(f"Tag '{tag_name}' already exists.")


def read_file(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"File not found: {path}")
    return path.read_text(encoding="utf-8")


def write_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def extract_current_versions(pyproject_text: str, init_text: str) -> tuple[str, str]:
    pyproject_match = PYPROJECT_VERSION_PATTERN.search(pyproject_text)
    init_match = INIT_VERSION_PATTERN.search(init_text)

    if not pyproject_match:
        raise RuntimeError("Could not find 'version = \"...\"' in pyproject.toml.")
    if not init_match:
        raise RuntimeError("Could not find '__version__ = \"...\"' in codedd_cli/__init__.py.")

    return pyproject_match.group(2), init_match.group(2)


def ensure_new_version_valid(new_version: str, current_version: str) -> None:
    if not SEMVER_PATTERN.match(new_version):
        raise RuntimeError("Version must match semantic version format: X.Y.Z (example: 0.1.1).")
    if new_version == current_version:
        raise RuntimeError(f"Version is unchanged ({new_version}).")


def replace_version(text: str, pattern: re.Pattern[str], new_version: str, label: str) -> str:
    updated_text, replaced = pattern.subn(rf'\1"{new_version}"', text, count=1)
    if replaced != 1:
        raise RuntimeError(f"Failed to update {label} version.")
    return updated_text


def run_or_fail(args: list[str], fail_message: str) -> None:
    result = run_git(args)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Unknown git error."
        raise RuntimeError(f"{fail_message}\n{detail}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bump version files, commit release change, and create annotated tag."
    )
    parser.add_argument("version", help="New version in X.Y.Z format (example: 0.1.1)")
    parser.add_argument(
        "--tag-prefix",
        default="v",
        help="Prefix for git tags (default: v, creates tags like v0.1.1).",
    )
    parser.add_argument(
        "--no-tag",
        action="store_true",
        help="Create commit only and skip tag creation.",
    )
    args = parser.parse_args()

    try:
        validate_git_repo()

        pyproject_text = read_file(PYPROJECT_PATH)
        init_text = read_file(INIT_PATH)
        current_pyproject_version, current_init_version = extract_current_versions(pyproject_text, init_text)

        if current_pyproject_version != current_init_version:
            raise RuntimeError(
                "Version mismatch before bump: "
                f"pyproject={current_pyproject_version}, __init__={current_init_version}."
            )

        ensure_new_version_valid(args.version, current_pyproject_version)

        tag_name = f"{args.tag_prefix}{args.version}"
        if not args.no_tag:
            ensure_tag_does_not_exist(tag_name)

        updated_pyproject = replace_version(
            pyproject_text, PYPROJECT_VERSION_PATTERN, args.version, "pyproject.toml"
        )
        updated_init = replace_version(init_text, INIT_VERSION_PATTERN, args.version, "__init__.py")

        write_file(PYPROJECT_PATH, updated_pyproject)
        write_file(INIT_PATH, updated_init)

        commit_message = f"chore(release): {tag_name}"
        run_or_fail(["commit", "-m", commit_message, "--", "pyproject.toml", "codedd_cli/__init__.py"], "Commit failed.")

        if not args.no_tag:
            run_or_fail(["tag", "-a", tag_name, "-m", f"Release {tag_name}"], "Tag creation failed.")

        print(f"Release commit created for version {args.version}.")
        if args.no_tag:
            print("Tagging skipped (--no-tag).")
        else:
            print(f"Annotated tag created: {tag_name}")
        print("Next: git push && git push --tags")
        return 0
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
