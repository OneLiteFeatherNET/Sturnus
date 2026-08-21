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


async def test_readyz_is_200_once_discord_and_the_database_are_both_up() -> None:
    state = ReadinessState(discord_connected=True, database_reachable=True)
    async with TestClient(TestServer(health_app(state))) as client:
        response = await client.get("/readyz")
        assert response.status == 200


async def test_readyz_stays_503_with_only_one_dependency_up() -> None:
    state = ReadinessState(discord_connected=True, database_reachable=False)
    async with TestClient(TestServer(health_app(state))) as client:
        response = await client.get("/readyz")
        assert response.status == 503


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
