"""`sturnus.observability` must stay importable from `application`.

`tests/test_architecture.py` already forbids `application` importing
`infrastructure`, and `application` calls `log_event` in five modules. That
only stays safe while this package imports nothing but the standard library
and `sturnus.domain` -- otherwise `from sturnus.observability.events import
log_event` in `application/worker.py` would be a third-party import wearing
a disguise, and the existing architecture test would not see it.

The concrete thing this prevents: putting the OpenTelemetry API in
`events.current_trace_context` instead of behind
`set_trace_context_provider`. That would be the obvious simplification, it
would work, and it would quietly make every `application` module depend on
`opentelemetry`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

OBSERVABILITY = Path(__file__).parent.parent.parent / "src" / "sturnus" / "observability"

_ALLOWED_PREFIXES = ("sturnus.observability", "sturnus.domain", "sturnus")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _is_allowed(module: str) -> bool:
    if module.split(".", 1)[0] in sys.stdlib_module_names:
        return True
    return module in _ALLOWED_PREFIXES or module.startswith(
        ("sturnus.observability.", "sturnus.domain.")
    )


def test_observability_imports_only_stdlib_and_domain() -> None:
    assert OBSERVABILITY.is_dir()
    violations: list[str] = []
    checked = 0
    for path in OBSERVABILITY.rglob("*.py"):
        checked += 1
        for module in _imported_modules(path):
            if not _is_allowed(module):
                violations.append(f"{path.name}: {module}")
    assert checked > 0
    assert not violations, (
        "sturnus.observability must import only the standard library and "
        "sturnus.domain, because sturnus.application imports it:\n" + "\n".join(violations)
    )


def test_opentelemetry_is_absent_from_the_shared_package() -> None:
    """Stated separately because it is the specific mistake worth naming."""
    for path in OBSERVABILITY.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import opentelemetry" not in source, path.name
        assert "from opentelemetry" not in source, path.name


def test_application_still_imports_no_infrastructure() -> None:
    """A guard on the guard: the five new `log_event` imports must not have
    dragged `sturnus.infrastructure` into `application` through a re-export.
    """
    application = OBSERVABILITY.parent / "application"
    violations: list[str] = []
    for path in application.rglob("*.py"):
        for module in _imported_modules(path):
            if module.startswith("sturnus.infrastructure"):
                violations.append(f"{path.name}: {module}")
    assert not violations, "\n".join(violations)
