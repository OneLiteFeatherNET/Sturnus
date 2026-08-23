"""Health endpoint tests (Spec 4.1).

`/healthz` is a plain liveness probe -- it must answer as long as the
process is alive, with no dependency on Discord or the database.
`/readyz` is the readiness probe and must reflect whatever state it is
handed, without reaching out to a real gateway connection or a real
database -- that is exactly what `ReadinessState` decouples it from.
"""

from aiohttp.test_utils import TestClient, TestServer

from sturnus.infrastructure.health import ReadinessState, health_app


async def test_healthz_is_always_ok() -> None:
    async with TestClient(TestServer(health_app(ReadinessState()))) as client:
        response = await client.get("/healthz")
        assert response.status == 200


async def test_readyz_is_503_before_the_injected_flags_are_set() -> None:
    async with TestClient(TestServer(health_app(ReadinessState()))) as client:
        response = await client.get("/readyz")
        assert response.status == 503


async def test_readyz_is_200_once_every_shard_and_the_database_are_up() -> None:
    state = ReadinessState(database_reachable=True)
    state.shard_connected(0, shard_count=1)
    async with TestClient(TestServer(health_app(state))) as client:
        response = await client.get("/readyz")
        assert response.status == 200


async def test_readyz_stays_503_with_only_one_dependency_up() -> None:
    state = ReadinessState(database_reachable=False)
    state.shard_connected(0, shard_count=1)
    async with TestClient(TestServer(health_app(state))) as client:
        response = await client.get("/readyz")
        assert response.status == 503


async def test_a_process_that_holds_no_gateway_is_ready_on_the_database_alone() -> None:
    """`worker` and `link` have no shards to wait on, and must not wait for one.

    Readiness over shards is "every shard I am supposed to hold is up".
    A process that holds none satisfies that vacuously, and saying so with
    `expected_shards=0` is what lets one `ReadinessState` serve every
    process without a second flag meaning "ignore the gateway".
    """
    state = ReadinessState(database_reachable=True, expected_shards=0)
    async with TestClient(TestServer(health_app(state))) as client:
        assert (await client.get("/readyz")).status == 200


async def test_a_partly_connected_cluster_of_shards_is_not_ready() -> None:
    """Three of four shards up is a quarter of the guilds unreachable.

    The tempting alternative -- ready as soon as *any* shard is up --
    reports healthy while a quarter of the servers Sturnus is in cannot
    be recorded in at all, and nothing anywhere else would say so. A probe
    that lies in that direction is worse than one that flaps.
    """
    state = ReadinessState(database_reachable=True)
    for shard_id in (0, 1, 3):
        state.shard_connected(shard_id, shard_count=4)
    async with TestClient(TestServer(health_app(state))) as client:
        response = await client.get("/readyz")
        assert response.status == 503
        body = await response.json()
        assert body["shards_connected"] == 3
        assert body["shards_expected"] == 4
        assert body["shards_missing"] == [2]


async def test_the_first_shard_up_does_not_declare_the_whole_process_ready() -> None:
    """The count of connected shards is meaningless without the expected one.

    `all(connected)` over a dict that only shard 0 has reached is
    trivially true, which would turn a four-shard process green the
    moment its first connection landed and leave it green while the other
    three never arrived.
    """
    state = ReadinessState(database_reachable=True)
    state.shard_connected(0, shard_count=4)
    async with TestClient(TestServer(health_app(state))) as client:
        assert (await client.get("/readyz")).status == 503


async def test_a_shard_that_drops_takes_readiness_with_it() -> None:
    """Readiness is a live statement, not a latch set once at startup.

    Before shards it was set `True` in `on_ready` and never cleared, so a
    gateway lost hours later left `/readyz` reporting green for the rest
    of the process's life. With shards there is a `shard_disconnect`
    event, so there is no longer an excuse for that.
    """
    state = ReadinessState(database_reachable=True)
    state.shard_connected(0, shard_count=2)
    state.shard_connected(1, shard_count=2)
    async with TestClient(TestServer(health_app(state))) as client:
        assert (await client.get("/readyz")).status == 200
        state.shard_disconnected(1)
        assert (await client.get("/readyz")).status == 503
        state.shard_connected(1, shard_count=2)
        assert (await client.get("/readyz")).status == 200


async def test_version_reports_the_installed_package_version() -> None:
    async with TestClient(TestServer(health_app(ReadinessState()))) as client:
        response = await client.get("/version")
        assert response.status == 200
        body = await response.json()
        assert "version" in body


async def test_metrics_answers_501_because_metrics_are_pushed() -> None:
    """The route exists and truthfully says there is nothing to scrape.

    It used to return `200` with an empty body, on the reasoning that an
    empty exposition is still valid Prometheus. That reasoning inverts the
    signal: a scrape of an empty `200` is indistinguishable from "every
    counter is legitimately zero", so an uninstrumented process would look
    perfectly healthy to a ServiceMonitor. A `501` marks the target down,
    which is the true statement, and the route still exists so Spec 4.1's
    endpoint list stays satisfied.
    """
    async with TestClient(TestServer(health_app(ReadinessState()))) as client:
        response = await client.get("/metrics")
        assert response.status == 501
        # Names the variable that turns metrics on, so the 501 is
        # self-documenting to whoever pointed a scraper here.
        assert "STURNUS_OTEL_EXPORTER_OTLP_ENDPOINT" in await response.text()
