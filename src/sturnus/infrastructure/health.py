"""HTTP health, readiness, metrics and version endpoints (Spec 4.1).

Kept deliberately dumb: Kubernetes polls these over plain HTTP, and the
readiness signal itself -- is the gateway connected, does the database
answer -- is computed elsewhere (`SturnusClient`) and only reported here.
`aiohttp` is already a transitive dependency of `discord.py`, so serving
these endpoints adds nothing new to install.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiohttp import web

from sturnus import __version__
from sturnus.infrastructure.metrics import COUNTERS, Counters, render_prometheus


@dataclass
class ReadinessState:
    """Mutable flags the caller flips as the process comes up.

    A plain dataclass rather than two booleans buried in the client lets a
    test inject exactly the state it wants to assert against, without a
    real gateway connection or a real database.
    """

    discord_connected: bool = False
    database_reachable: bool = False

    @property
    def ready(self) -> bool:
        return self.discord_connected and self.database_reachable


def health_app(state: ReadinessState, counters: Counters | None = None) -> web.Application:
    """Builds the aiohttp application serving `/healthz`, `/readyz`, `/metrics`, `/version`.

    `counters` defaults to the process-wide instance every production
    caller shares, so nothing has to be threaded through four
    constructors; a test passes its own and asserts on it without touching
    global state.
    """
    exported = counters if counters is not None else COUNTERS

    async def healthz(_request: web.Request) -> web.Response:
        # Liveness only: the process is running and its event loop answers
        # HTTP requests. No dependency is checked here -- that is `/readyz`'s
        # job -- so a slow database never makes Kubernetes kill a bot that
        # is otherwise fine.
        return web.json_response({"status": "ok"})

    async def readyz(_request: web.Request) -> web.Response:
        if state.ready:
            return web.json_response({"status": "ready"})
        return web.json_response(
            {
                "status": "not ready",
                "discord_connected": state.discord_connected,
                "database_reachable": state.database_reachable,
            },
            status=503,
        )

    async def metrics(_request: web.Request) -> web.Response:
        # An empty exposition is still a valid Prometheus response, which
        # is what this returns before anything has been counted.
        return web.Response(text=render_prometheus(exported.snapshot()), content_type="text/plain")

    async def version(_request: web.Request) -> web.Response:
        return web.json_response({"version": __version__})

    app = web.Application()
    app.add_routes(
        [
            web.get("/healthz", healthz),
            web.get("/readyz", readyz),
            web.get("/metrics", metrics),
            web.get("/version", version),
        ]
    )
    return app


async def start_health_server(
    state: ReadinessState, port: int, counters: Counters | None = None
) -> web.AppRunner:
    """Starts the health app on `port` and returns the runner so it can be torn down.

    Bound to `0.0.0.0`: this listens inside the pod network, not on a
    public interface, so the wide bind is scoped by Kubernetes networking
    rather than by the application.
    """
    app = health_app(state, counters)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner
