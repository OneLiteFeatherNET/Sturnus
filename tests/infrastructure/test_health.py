"""Health endpoint tests (Spec 4.1).

`/healthz` is a plain liveness probe -- it must answer as long as the
process is alive, with no dependency on Discord or the database.
`/readyz` is the readiness probe and must reflect whatever state it is
handed, without reaching out to a real gateway connection or a real
database -- that is exactly what `ReadinessState` decouples it from.
"""

from aiohttp.test_utils import TestClient, TestServer

from sturnus.infrastructure.health import ReadinessState, health_app
from sturnus.infrastructure.metrics import Counters


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


async def test_metrics_is_reachable_before_anything_has_been_counted() -> None:
    """An empty exposition is a valid Prometheus response, not a 404."""
    async with TestClient(TestServer(health_app(ReadinessState(), Counters()))) as client:
        response = await client.get("/metrics")
        assert response.status == 200
        assert await response.text() == ""


async def test_metrics_renders_what_has_been_counted() -> None:
    """The voice counters are how a silent capture failure becomes visible.

    Logs and a message in the channel are the other two answers; this one
    is the one that can be alerted on without a human reading anything.
    """
    counters = Counters()
    counters.inc("sturnus_voice_frames_discarded_total", 7.0)

    async with TestClient(TestServer(health_app(ReadinessState(), counters))) as client:
        response = await client.get("/metrics")
        assert response.status == 200
        assert "sturnus_voice_frames_discarded_total 7" in await response.text()
