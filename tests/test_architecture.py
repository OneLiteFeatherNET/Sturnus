# tests/test_architecture.py
import ast
from pathlib import Path

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

    # The package is the parent directory (same for __init__.py and other files)
    directory = relative.parent

    # Convert path to module name
    parts = directory.parts
    return ".".join(parts) if parts else ""


def _resolve_relative_import(module: str | None, level: int, file_package: str) -> str | None:
    """Resolve a relative import to its absolute module name.

    Args:
        module: Module part after 'from' (e.g., 'infrastructure' in 'from ..infrastructure')
        level: The number of dots (e.g., 2 for '..')
        file_package: The package the importing file belongs to

    Returns:
        The absolute module name, or None if the import is invalid or local-only.
    """
    if not module:
        # from . or from .. without a specific module - these are local
        return None

    parts = file_package.split(".")

    # Walk up level-1 levels from the current package
    # level=1 means "from .module" - stay at current package
    # level=2 means "from ..module" - go up one level
    levels_up = level - 1

    if levels_up >= len(parts):
        # Invalid: trying to go above the root
        return None

    # Remove the last levels_up elements
    base_parts = list(parts[: len(parts) - levels_up])

    # Append the module
    base_parts.append(module)
    return ".".join(base_parts)


def _resolve_relative_names(
    names: list[ast.alias], level: int, file_package: str
) -> set[str]:
    """Resolve names imported from a relative package (from .. import x, y, z).

    When level >= 2, these imports can escape the current package.
    When level == 1, imports stay within the package.

    Args:
        names: The imported names from the from...import statement
        level: The relative level (1 for '.', 2 for '..', etc.)
        file_package: The package the importing file belongs to

    Returns:
        Set of absolute module names, or empty set if imports stay local (level == 1).
    """
    if level < 2:
        # from . import x stays within the package
        return set()

    parts = file_package.split(".")
    levels_up = level - 1

    if levels_up >= len(parts):
        # Can't go up that many levels
        return set()

    base_parts = list(parts[: len(parts) - levels_up])
    result = set()

    for alias in names:
        name = alias.name
        submodule = ".".join(base_parts + [name])
        result.add(submodule)

    return result


def _imported_modules(path: Path, src_path: Path = SRC) -> set[str]:
    """Extract all imported modules from a file, resolving relative imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()

    # Determine the package of this file
    file_package = _get_package_name(path, src_path)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # Absolute imports: import x, import x.y, import x as y
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                # Absolute import: from x import y
                if node.module:
                    found.add(node.module)
            else:
                # Relative import: from .x import y or from ..x import y
                if node.module:
                    # from .x import y or from ..x import y
                    resolved = _resolve_relative_import(node.module, node.level, file_package)
                    if resolved:
                        found.add(resolved)
                else:
                    # from . import x, y or from .. import x, y
                    resolved_names = _resolve_relative_names(node.names, node.level, file_package)
                    found.update(resolved_names)

    return found


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


def test_domain_rejects_relative_imports_escaping_domain_layer() -> None:
    """Verify the test catches relative imports that escape the domain package."""
    probe_file = DOMAIN / "_probe_escape.py"
    try:
        # This probe violates the rule: escaping domain via relative import
        probe_file.write_text("from ..infrastructure import db\n")

        violations: list[str] = []
        for path in DOMAIN.rglob("*.py"):
            for module in _imported_modules(path):
                if module.startswith(FORBIDDEN_PREFIXES):
                    violations.append(f"{path.relative_to(DOMAIN.parent)}: {module}")

        # The violation must be caught
        assert any("_probe_escape.py" in v and "sturnus.infrastructure" in v for v in violations), \
            f"Expected violation not detected. Got violations: {violations}"
    finally:
        probe_file.unlink(missing_ok=True)


def test_domain_rejects_name_list_relative_imports_escaping_domain_layer() -> None:
    """Verify the test catches 'from .. import name' syntax for escaping imports."""
    probe_file = DOMAIN / "_probe_escape_names.py"
    try:
        # This probe uses the name-list syntax: from .. import infrastructure
        # Both this and 'from ..infrastructure import db' should escape to sturnus.infrastructure
        probe_file.write_text("from .. import infrastructure\n")

        modules = _imported_modules(probe_file)

        # Verify it resolved to the forbidden module
        assert "sturnus.infrastructure" in modules, \
            f"Expected sturnus.infrastructure in resolved modules, got: {modules}"

        violations: list[str] = []
        for path in DOMAIN.rglob("*.py"):
            for module in _imported_modules(path):
                if module.startswith(FORBIDDEN_PREFIXES):
                    violations.append(f"{path.relative_to(DOMAIN.parent)}: {module}")

        # The violation must be caught
        found = any(
            "_probe_escape_names.py" in v and "sturnus.infrastructure" in v
            for v in violations
        )
        assert found, f"Expected violation not detected. Got violations: {violations}"
    finally:
        probe_file.unlink(missing_ok=True)


def test_domain_allows_relative_imports_within_domain_layer() -> None:
    """Verify relative imports within domain are allowed and resolve correctly."""
    probe_file = DOMAIN / "_probe_local.py"
    try:
        # These imports stay within domain - should NOT violate
        probe_file.write_text("from .models import Session\n")

        modules = _imported_modules(probe_file)

        # Verify the import resolved correctly to a domain-internal module
        assert "sturnus.domain.models" in modules, \
            f"Expected sturnus.domain.models in resolved modules, got: {modules}"

        # Verify no violations
        violations = [m for m in modules if m.startswith(FORBIDDEN_PREFIXES)]
        assert not violations, f"Unexpected violations: {violations}"
    finally:
        probe_file.unlink(missing_ok=True)
