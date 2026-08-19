# tests/test_architecture.py
import ast
from pathlib import Path

DOMAIN = Path(__file__).parent.parent / "src" / "sturnus" / "domain"

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


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):  # noqa: SIM102
            # level > 0 is a relative import and thus stays within the package
            if node.level == 0 and node.module:
                found.add(node.module)
    return found


def test_domain_has_no_outward_imports() -> None:
    violations: list[str] = []
    for path in DOMAIN.rglob("*.py"):
        for module in _imported_modules(path):
            if module.startswith(FORBIDDEN_PREFIXES):
                violations.append(f"{path.relative_to(DOMAIN.parent)}: {module}")
    assert not violations, "domain must not import outward:\n" + "\n".join(violations)


def test_application_does_not_import_infrastructure() -> None:
    app = DOMAIN.parent / "application"
    violations: list[str] = []
    for path in app.rglob("*.py"):
        for module in _imported_modules(path):
            if module.startswith("sturnus.infrastructure"):
                violations.append(f"{path.relative_to(DOMAIN.parent)}: {module}")
    assert not violations, "application must not import infrastructure:\n" + "\n".join(violations)
