"""HTTP health, readiness, metrics and version endpoints (Spec 4.1).

Kept deliberately dumb: Kubernetes polls these over plain HTTP, and the
readiness signal itself -- is the gateway connected, does the database
answer -- is computed elsewhere (`SturnusClient`) and only reported here.
`aiohttp` is already a transitive dependency of `discord.py`, so serving
these endpoints adds nothing new to install.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiohttp import web

from sturnus import __version__
from sturnus.application.sharding import shards_this_process_owns


@dataclass
class ReadinessState:
    """Mutable state the caller updates as the process comes up.

    A plain dataclass rather than flags buried in the client lets a test
    inject exactly the state it wants to assert against, without a real
    gateway connection or a real database.

    **What "ready" means when the bot holds several shards.** The gateway
    half used to be one boolean, set in `on_ready` and never cleared: a
    gateway lost an hour after startup left `/readyz` green for the rest
    of the process's life, because nothing was watching. Sharding both
    forces the question and supplies the answer, since `on_shard_connect`
    / `on_shard_disconnect` are per-connection events there is no excuse
    for ignoring.

    The rule chosen is **every shard this process is supposed to hold**.
    The two alternatives are both worse:

    - *Any shard up.* With four shards and one down, a quarter of the
      guilds Sturnus is in cannot be recorded in at all, and the probe
      would report perfect health. A probe that lies in that direction is
      worse than one that flaps -- nothing else in the stack would say so.
    - *A fraction.* "Three quarters up is ready" needs a threshold nobody
      can defend, and buys nothing: the pod is not load-balanced, so
      failing readiness sheds no traffic. It is purely a signal, and the
      truthful signal is "this process is not doing all of its job".

    **It does not flap on a routine reconnect**, and that is arithmetic
    rather than hope. discord.py dispatches `shard_disconnect` for every
    RESUME too, so a blip does clear a shard here -- for the second or
    two until `shard_resumed`. The chart polls `/readyz` every 10s with
    the default `failureThreshold: 3`, so a shard has to be down for
    30 continuous seconds before Kubernetes marks the pod NotReady. A
    RESUME is invisible; a shard that is genuinely gone is not.

    `expected_shards` is `None` until the gateway has told this process
    how many shards it will open, which keeps a four-shard process from
    turning green the moment its first connection lands. `0` is the
    honest value for a process that holds no gateway at all (`worker`,
    `link`): it holds all zero of the shards it is supposed to hold.
    """

    database_reachable: bool = False
    #: How many shards this process must hold before it is doing its job.
    #: `None` while the gateway has not said yet; `0` for a process with
    #: no gateway connection at all.
    expected_shards: int | None = None
    #: The shard ids currently holding a live gateway connection.
    connected_shards: set[int] = field(default_factory=set)

    def shard_connected(self, shard_id: int, *, shard_count: int | None = None) -> None:
        """Records that one shard is up, and how many there are in total.

        `shard_count` arrives with every call rather than being set once
        at startup because `discord.Client.shard_count` is only populated
        once `launch_shards` has run -- which is after this object is
        constructed and before the first shard reports in. Taking it from
        whichever event happens to arrive first removes the ordering
        question entirely.
        """
        if shard_count is not None:
            self.expected_shards = shard_count
        self.connected_shards.add(shard_id)

    def shard_disconnected(self, shard_id: int) -> None:
        """Records that one shard has dropped, whether or not it comes straight back."""
        self.connected_shards.discard(shard_id)

    @property
    def missing_shards(self) -> list[int]:
        """The shard ids this process should hold and does not, in order.

        Reported in the `/readyz` body so a 503 names *which* quarter of
        the guilds is unreachable, rather than leaving an operator to
        correlate log lines to find out.

        Which ids this process *should* hold is asked of
        `sturnus.application.sharding` rather than assumed to be
        `range(shard_count)`. Today they are the same set; stage two --
        one shard range per pod -- narrows that function, and this
        property follows without being touched.
        """
        if self.expected_shards is None:
            return []
        return sorted(shards_this_process_owns(self.expected_shards) - self.connected_shards)

    @property
    def discord_connected(self) -> bool:
        """Whether every shard this process is supposed to hold is up."""
        if self.expected_shards is None:
            return False
        return not self.missing_shards

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
        # The body names *which* shards are missing, not only that the
        # gateway half is unsatisfied. With one shard that is the same
        # statement; with four it is the difference between "the bot is
        # down" and "a quarter of the guilds are", which are different
        # incidents with different responses.
        return web.json_response(
            {
                "status": "not ready",
                "discord_connected": state.discord_connected,
                "database_reachable": state.database_reachable,
                "shards_connected": len(state.connected_shards),
                "shards_expected": state.expected_shards,
                "shards_missing": state.missing_shards,
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
