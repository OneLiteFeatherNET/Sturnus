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


def health_app(state: ReadinessState) -> web.Application:
    """Builds the aiohttp application serving `/healthz`, `/readyz`, `/metrics`, `/version`."""

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
        # Metrics are **pushed** over OTLP to the endpoint named below (see
        # `sturnus.infrastructure.telemetry`), not scraped from here.
        #
        # This used to return 200 with an empty body, on the reasoning that
        # an empty exposition is still valid Prometheus. That is worse than
        # a 501: a scrape of an empty 200 is indistinguishable from "every
        # counter is legitimately zero", so the day someone points a
        # ServiceMonitor at this route, a completely uninstrumented process
        # looks perfectly healthy. A 501 marks the target down, which is the
        # truthful signal, and the route still exists so Spec 4.1's endpoint
        # list stays literally satisfied.
        return web.Response(
            status=501,
            text=(
                "Sturnus pushes metrics over OTLP; there is nothing to scrape here. "
                "Set STURNUS_OTEL_EXPORTER_OTLP_ENDPOINT and read them from Grafana.\n"
            ),
            content_type="text/plain",
        )

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


async def start_health_server(state: ReadinessState, port: int) -> web.AppRunner:
    """Starts the health app on `port` and returns the runner so it can be torn down.

    Bound to `0.0.0.0`: this listens inside the pod network, not on a
    public interface, so the wide bind is scoped by Kubernetes networking
    rather than by the application.
    """
    app = health_app(state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner
