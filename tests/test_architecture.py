"""Architecture test for the inward dependency rule.

This test enforces that sturnus.domain has no outward dependencies, and that
sturnus.application does not depend on sturnus.infrastructure.

Scope: Static import statements are checked via AST parsing. Dynamic imports
via __import__() or importlib.import_module() are not detected by this test
and are out of scope.
"""

import ast
import sys
from pathlib import Path

import pytest

DOMAIN = Path(__file__).parent.parent / "src" / "sturnus" / "domain"
SRC = Path(__file__).parent.parent / "src"

DOMAIN_PACKAGE = "sturnus.domain"


def _is_stdlib_or_domain(module: str) -> bool:
    """Allowlist for domain imports: standard library, or within sturnus.domain.

    This is deliberately an allowlist rather than a denylist: a new
    third-party dependency landing in pyproject.toml is safe by default
    instead of silently passing the check until someone remembers to add
    it to a forbidden-list.
    """
    root = module.split(".", 1)[0]
    if root in sys.stdlib_module_names:
        return True
    return module == DOMAIN_PACKAGE or module.startswith(f"{DOMAIN_PACKAGE}.")


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


def _resolve_imports_in_module(tree: ast.Module, file_path: Path) -> set[str]:
    """Resolve all module paths reachable by imports in an AST tree.

    For each import statement, yields the full set of module paths it can reach:
    - 'import a.b' reaches 'a.b'
    - 'from a.b import c' reaches both 'a.b' and 'a.b.c'
    - 'from a.b import *' reaches only 'a.b'
    - Relative imports are resolved to absolute paths first

    Args:
        tree: An ast.Module from ast.parse()
        file_path: The file path (used to determine the package context for relative imports)

    Returns:
        Set of all reachable module names.
    """
    found: set[str] = set()
    file_package = _get_package_name(file_path, SRC)

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
                found.add(base_module)

                # For each imported name, also record the submodule path
                # (unless it's a star import)
                for alias in node.names:
                    if alias.name != "*":
                        found.add(f"{base_module}.{alias.name}")

    return found


def _imported_modules(path: Path) -> set[str]:
    """Extract all module paths reachable by imports in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _resolve_imports_in_module(tree, path)


def _check_violations(source: str, file_path: Path) -> set[str]:
    """Parse source and return any resolved modules that violate the rule."""
    tree = ast.parse(source, filename=str(file_path))
    all_modules = _resolve_imports_in_module(tree, file_path)
    return {m for m in all_modules if not _is_stdlib_or_domain(m)}


def test_domain_has_no_outward_imports() -> None:
    assert DOMAIN.exists(), f"Domain directory does not exist: {DOMAIN}"

    violations: list[str] = []
    files_checked = 0

    for path in DOMAIN.rglob("*.py"):
        files_checked += 1
        for module in _imported_modules(path):
            if not _is_stdlib_or_domain(module):
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
    "import_stmt,should_violate,expected_modules",
    [
        # Violations: absolute imports from forbidden modules
        ("import sqlalchemy", True, {"sqlalchemy"}),
        ("import sqlalchemy.orm", True, {"sqlalchemy.orm"}),
        ("import sqlalchemy as sa", True, {"sqlalchemy"}),
        (
            "from sqlalchemy import select",
            True,
            {"sqlalchemy", "sqlalchemy.select"},
        ),
        (
            "from sqlalchemy.orm import Mapped",
            True,
            {"sqlalchemy.orm", "sqlalchemy.orm.Mapped"},
        ),
        # Violations: absolute imports from sturnus forbidden packages
        (
            "from sturnus import infrastructure",
            True,
            {"sturnus", "sturnus.infrastructure"},
        ),
        ("from sturnus import application", True, {"sturnus", "sturnus.application"}),
        (
            "from sturnus.infrastructure.db import models",
            True,
            {"sturnus.infrastructure.db", "sturnus.infrastructure.db.models"},
        ),
        # Violations: relative imports that escape domain
        (
            "from ..infrastructure import db",
            True,
            {"sturnus.infrastructure", "sturnus.infrastructure.db"},
        ),
        (
            "from .. import infrastructure",
            True,
            {"sturnus", "sturnus.infrastructure"},
        ),
        (
            "from .. import infrastructure, application",
            True,
            {"sturnus", "sturnus.infrastructure", "sturnus.application"},
        ),
        ("from ..infrastructure import *", True, {"sturnus.infrastructure"}),
        # Allowed: standard library and project packages
        ("import json", False, {"json"}),
        (
            "from dataclasses import dataclass",
            False,
            {"dataclasses", "dataclasses.dataclass"},
        ),
        (
            "from . import helper",
            False,
            {"sturnus.domain", "sturnus.domain.helper"},
        ),
        (
            "from .timeline import SpeakerClock",
            False,
            {"sturnus.domain.timeline", "sturnus.domain.timeline.SpeakerClock"},
        ),
        (
            "from sturnus.domain import timeline",
            False,
            {"sturnus.domain", "sturnus.domain.timeline"},
        ),
    ],
)
def test_import_resolution_comprehensive(
    import_stmt: str, should_violate: bool, expected_modules: set[str]
) -> None:
    """Exhaustive test of all import forms to prevent regression.

    This table specifies the expected behavior for every import spelling.
    Each row verifies both:
    - Whether a violation is detected (if applicable)
    - That the imports resolve to the expected module names

    If this test fails, either a new syntax has been found that bypasses
    the rule, or an existing syntax is incorrectly handled.
    """
    fake_path = DOMAIN / "session.py"
    tree = ast.parse(import_stmt, filename=str(fake_path))
    modules = _resolve_imports_in_module(tree, fake_path)
    violations = _check_violations(import_stmt, fake_path)

    # Verify the expected modules were resolved
    assert modules == expected_modules, (
        f"Expected modules mismatch\n"
        f"Statement: {import_stmt}\n"
        f"Expected: {expected_modules}\n"
        f"Got: {modules}"
    )

    # Verify violation detection matches expectation
    if should_violate:
        assert violations, (
            f"Expected violation not detected\nStatement: {import_stmt}\nViolations: {violations}"
        )
    else:
        assert not violations, (
            f"Unexpected violation detected\nStatement: {import_stmt}\nViolations: {violations}"
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
                if not _is_stdlib_or_domain(module):
                    violations.append(f"{path.relative_to(DOMAIN.parent)}: {module}")

        # Verify the violation was caught by the directory walk
        found = any("_probe_real_violation.py" in v for v in violations)
        assert found, f"Expected violation not found in directory walk. Violations: {violations}"
    finally:
        probe_file.unlink(missing_ok=True)
