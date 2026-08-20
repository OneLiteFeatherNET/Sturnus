"""Proves the three `main()` functions actually wire Sentry up, and in order.

`sturnus.infrastructure.observability` is unit-tested on its own, which
proves the scrubbing but says nothing about whether any shipped process ever
calls it — the same gap `tests/conftest`'s purge regression was written for.
It also cannot see the ordering, which is the point of `SentrySettings`
being a class with no required fields: `init_sentry` has to run *before* the
process's own settings are read, or a settings `ValidationError` is the one
failure that can never be reported.
"""

from __future__ import annotations

import importlib
from collections.abc import Coroutine
from types import ModuleType
from typing import Any

import pytest
import sentry_sdk

MODULES = [
    ("sturnus.entrypoints.bot", "bot"),
    ("sturnus.entrypoints.worker", "worker"),
    ("sturnus.entrypoints.link", "link"),
]


def _stub_asyncio_run(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, order: list[str]
) -> None:
    """Replaces `asyncio.run(_run())` without ever entering `_run`.

    The coroutine still has to be closed, or Python warns about a coroutine
    that was never awaited and the test output becomes noise.
    """

    def fake_run(coro: Coroutine[Any, Any, None]) -> None:
        coro.close()
        order.append("run")

    monkeypatch.setattr(module.asyncio, "run", fake_run)


@pytest.mark.parametrize("module_name,component", MODULES)
def test_main_initialises_sentry_before_running(
    module_name: str, component: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module(module_name)
    order: list[str] = []

    def fake_init_sentry(name: str) -> bool:
        order.append(f"sentry:{name}")
        return False

    monkeypatch.setattr(module, "init_sentry", fake_init_sentry)
    _stub_asyncio_run(module, monkeypatch, order)

    module.main()

    assert order == [f"sentry:{component}", "run"], (
        "each process must identify itself and install reporting before its "
        "settings class is constructed inside _run"
    )


@pytest.mark.parametrize("module_name,component", MODULES)
def test_main_starts_no_sentry_client_without_a_dsn(
    module_name: str, component: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sentry is optional, asserted against the real `init_sentry`.

    With no DSN the SDK must not be entered at all — not initialised and
    then muted. `sentry_sdk.init(dsn="")` would leave
    `logging.Logger.callHandlers`, `sys.excepthook`, `threading.Thread.run`
    and an `atexit` hook patched while sending nothing, so this asserts on
    `is_initialized()` rather than on whether anything was transmitted.
    """
    del component
    module = importlib.import_module(module_name)
    monkeypatch.delenv("STURNUS_SENTRY_DSN", raising=False)
    _stub_asyncio_run(module, monkeypatch, [])

    module.main()

    assert sentry_sdk.is_initialized() is False


@pytest.mark.parametrize("module_name,component", MODULES)
def test_main_survives_a_malformed_sentry_dsn(
    module_name: str, component: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in optional telemetry must not stop the process from starting.

    `init_sentry` runs before the event loop in every `main()`, so a `BadDsn`
    escaping it does not degrade error reporting — it crash-loops bot, worker
    and link, and the bot stops recording because the thing meant to observe
    the failure could not start. Reporting is disabled instead, loudly, and
    `main()` goes on to `asyncio.run`.
    """
    del component
    module = importlib.import_module(module_name)
    monkeypatch.setenv("STURNUS_SENTRY_DSN", "not-a-dsn")
    order: list[str] = []
    _stub_asyncio_run(module, monkeypatch, order)

    module.main()

    assert order == ["run"]
    assert sentry_sdk.is_initialized() is False
