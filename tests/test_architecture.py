# tests/test_architecture.py
import ast
from pathlib import Path

import pytest

DOMAIN = Path(__file__).parent.parent / "src" / "sturnus" / "domain"
SRC = Path(__file__).parent.parent / "src"

FORBIDDEN_PREFIXES = (
    "sturnus.application",
    "sturnus.infrastructure",
    "discord",
    "sqlalchemy",
    "alembic",
    "asyncpg",
    "boto3",
    "botocore",
    "jinja2",
    "faster_whisper",
    "aiohttp",
)


def _get_package_name(file_path: Path, src_path: Path) -> str:
    """Get the package name that a Python file belongs to."""
    relative = file_path.relative_to(src_path)
    directory = relative.parent
    parts = directory.parts
    return ".".join(parts) if parts else ""


def _resolve_relative_base(module: str | None, level: int, file_package: str) -> str | None:
    """Resolve the base module path for a relative from...import statement.

    Args:
        module: The module name after 'from' (e.g., 'db' in 'from ..db import')
        level: The relative level (1 for '.', 2 for '..', etc.)
        file_package: The package the importing file belongs to

    Returns:
        The absolute base module path, or None if invalid.
    """
    parts = file_package.split(".")
    levels_up = level - 1

    if levels_up >= len(parts):
        # Invalid: trying to go above the root
        return None

    # Walk up the package hierarchy
    base_parts = list(parts[: len(parts) - levels_up])

    # Append the module name if present
    if module:
        base_parts.extend(module.split("."))

    return ".".join(base_parts) if base_parts else None


def _imported_modules(path: Path, src_path: Path = SRC) -> set[str]:
    """Extract all module paths reachable by imports in a file.

    For each import statement, yields the full set of module paths it can reach:
    - 'import a.b' reaches 'a.b'
    - 'from a.b import c' reaches both 'a.b' and 'a.b.c'
    - 'from a.b import *' reaches only 'a.b'
    - Relative imports are resolved to absolute paths first
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    file_package = _get_package_name(path, src_path)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # import a, import a.b, import a.b as x
            # Record the full module name (alias.name is the left side of 'as')
            for alias in node.names:
                found.add(alias.name)

        elif isinstance(node, ast.ImportFrom):
            # Determine the base module (what's after 'from')
            if node.level == 0:
                # Absolute import: from a.b import c
                base_module = node.module
            else:
                # Relative import: from . import c or from ..a.b import c
                base_module = _resolve_relative_base(node.module, node.level, file_package)

            if base_module:
                # Record the base module itself
                found.add(base_module)

                # For each imported name, also record the submodule path
                # (unless it's a star import)
                for alias in node.names:
                    if alias.name != "*":
                        found.add(f"{base_module}.{alias.name}")

    return found


def _check_violations(
    source: str, file_path: Path = DOMAIN / "session.py"
) -> set[str]:
    """Parse source and return any resolved modules that violate the rule."""
    tree = ast.parse(source, filename=str(file_path))
    found: set[str] = set()
    file_package = _get_package_name(file_path, SRC)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)

        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base_module = node.module
            else:
                base_module = _resolve_relative_base(node.module, node.level, file_package)

            if base_module:
                found.add(base_module)

                for alias in node.names:
                    if alias.name != "*":
                        found.add(f"{base_module}.{alias.name}")

    return {m for m in found if m.startswith(FORBIDDEN_PREFIXES)}


def test_domain_has_no_outward_imports() -> None:
    assert DOMAIN.exists(), f"Domain directory does not exist: {DOMAIN}"

    violations: list[str] = []
    files_checked = 0

    for path in DOMAIN.rglob("*.py"):
        files_checked += 1
        for module in _imported_modules(path):
            if module.startswith(FORBIDDEN_PREFIXES):
                violations.append(f"{path.relative_to(DOMAIN.parent)}: {module}")

    assert files_checked > 0, f"No Python files found in {DOMAIN}"
    assert not violations, "domain must not import outward:\n" + "\n".join(violations)


def test_application_does_not_import_infrastructure() -> None:
    app = DOMAIN.parent / "application"
    assert app.exists(), f"Application directory does not exist: {app}"

    violations: list[str] = []
    files_checked = 0

    for path in app.rglob("*.py"):
        files_checked += 1
        for module in _imported_modules(path):
            if module.startswith("sturnus.infrastructure"):
                violations.append(f"{path.relative_to(DOMAIN.parent)}: {module}")

    assert files_checked > 0, f"No Python files found in {app}"
    assert not violations, "application must not import infrastructure:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    "import_stmt,should_violate",
    [
        # Violations: absolute imports from forbidden modules
        ("import sqlalchemy", True),
        ("import sqlalchemy.orm", True),
        ("import sqlalchemy as sa", True),
        ("from sqlalchemy import select", True),
        ("from sqlalchemy.orm import Mapped", True),
        # Violations: absolute imports from sturnus forbidden packages
        ("from sturnus import infrastructure", True),
        ("from sturnus import application", True),
        ("from sturnus.infrastructure.db import models", True),
        # Violations: relative imports that escape domain
        ("from ..infrastructure import db", True),
        ("from .. import infrastructure", True),
        ("from .. import infrastructure, application", True),
        ("from ..infrastructure import *", True),
        # Allowed: standard library and project packages
        ("import json", False),
        ("from dataclasses import dataclass", False),
        ("from . import helper", False),
        ("from .timeline import SpeakerClock", False),
        ("from sturnus.domain import timeline", False),
    ],
)
def test_import_resolution_comprehensive(
    import_stmt: str, should_violate: bool
) -> None:
    """Exhaustive test of all import forms to prevent regression.

    This table specifies the expected behavior for every import spelling.
    If this test fails, it means a new syntax has been found that bypasses
    the rule, or an existing syntax is incorrectly flagged.
    """
    violations = _check_violations(import_stmt)

    if should_violate:
        assert violations, (
            f"Expected violation not detected\n"
            f"Statement: {import_stmt}\n"
            f"Violations: {violations}"
        )
    else:
        assert not violations, (
            f"Unexpected violation detected\n"
            f"Statement: {import_stmt}\n"
            f"Violations: {violations}"
        )


def test_directory_walk_detects_real_violation() -> None:
    """End-to-end test that the directory walk detects actual violations."""
    probe_file = DOMAIN / "_probe_real_violation.py"
    try:
        # Create a real probe file with an absolute import of a forbidden module
        probe_file.write_text("from sturnus.infrastructure import db\n")

        violations: list[str] = []
        for path in DOMAIN.rglob("*.py"):
            for module in _imported_modules(path):
                if module.startswith(FORBIDDEN_PREFIXES):
                    violations.append(f"{path.relative_to(DOMAIN.parent)}: {module}")

        # Verify the violation was caught by the directory walk
        found = any("_probe_real_violation.py" in v for v in violations)
        assert found, f"Expected violation not found in directory walk. Violations: {violations}"
    finally:
        probe_file.unlink(missing_ok=True)
