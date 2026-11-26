#!/usr/bin/env python3
"""Release alignment checks for CI tag builds.

When GITHUB_REF is refs/tags/vX.Y.Z, verifies:
  - outlook_web.__version__ matches the tag (without ``v`` prefix)
  - pyproject.toml project.version matches the app and tag
  - CHANGELOG.md contains ``## [vX.Y.Z] - ...``

Non-tag refs (main/dev/PR) exit 0 without checks.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

_SEMVER_TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)(?:[._-].*)?$")


def _expected_version_from_ref(ref: str) -> str | None:
    if not ref.startswith("refs/tags/"):
        return None
    tag = ref.removeprefix("refs/tags/")
    m = _SEMVER_TAG_RE.match(tag)
    if not m:
        return None
    return m.group(1)


def check_app_version(expected: str) -> list[str]:
    app_source = _REPO_ROOT / "outlook_web" / "__init__.py"
    try:
        module = ast.parse(app_source.read_text(encoding="utf-8"), filename=str(app_source))
    except (OSError, SyntaxError) as exc:
        return [f"Cannot read outlook_web.__version__: {exc}"]

    version = None
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in statement.targets
        ):
            if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                version = statement.value.value
            break

    if version is None:
        return ["outlook_web.__version__ must be a literal string assignment"]
    if version != expected:
        return [f"outlook_web.__version__ is {version!r}, expected {expected!r} (from git tag)"]
    return []


def check_project_version(expected: str) -> list[str]:
    try:
        with (_REPO_ROOT / "pyproject.toml").open("rb") as project_file:
            project = tomllib.load(project_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"Cannot read pyproject.toml: {exc}"]

    version = project.get("project", {}).get("version")
    if version != expected:
        return [f"pyproject.toml project.version is {version!r}, expected {expected!r} (from git tag)"]
    return []


def check_changelog(tag: str) -> list[str]:
    changelog = Path("CHANGELOG.md")
    if not changelog.is_file():
        return ["CHANGELOG.md not found"]
    text = changelog.read_text(encoding="utf-8")
    header_re = re.compile(rf"^##\s+\[{re.escape(tag)}\]\s+-\s+.*$", re.M)
    if not header_re.search(text):
        return [f"CHANGELOG.md has no release section heading ## [{tag}] - YYYY-MM-DD"]
    return []


def run_checks(ref: str | None = None) -> list[str]:
    ref = ref if ref is not None else os.environ.get("GITHUB_REF", "")
    expected = _expected_version_from_ref(ref)
    if expected is None:
        return []

    tag = ref.removeprefix("refs/tags/")
    errors: list[str] = []
    errors.extend(check_app_version(expected))
    errors.extend(check_project_version(expected))
    errors.extend(check_changelog(tag))
    return errors


def main() -> int:
    ref = os.environ.get("GITHUB_REF", "")
    expected = _expected_version_from_ref(ref)
    if expected is None:
        print(f"skip: no release tag checks for ref={ref!r}")
        return 0

    tag = ref.removeprefix("refs/tags/")
    errors = run_checks(ref)
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    print(f"OK: __version__={expected}, pyproject.toml project.version and CHANGELOG.md section [{tag}] are aligned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
