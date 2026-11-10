"""`begin_link`: the `/link start` decision, reused by the channel prompt.

The state must be issued for the person who pressed, never for the member the
prompt mentions. The fakes record the Discord id each state is issued for.
"""

from datetime import UTC, datetime, timedelta

from sturnus.application.linking import begin_link

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
TTL = timedelta(minutes=15)
PROVIDER = "outline"
PRESSER = 100
MENTIONED = 200


class _Identities:
    def __init__(self, identity: tuple[str, str] | None = None) -> None:
        self._identity = identity

    async def external_identity(
        self, _discord_user_id: int, _provider: str | None = None
    ) -> tuple[str, str] | None:
        return self._identity


class _States:
    def __init__(self) -> None:
        self.issued: list[tuple[int, str, datetime, timedelta]] = []

    async def issue(
        self, discord_user_id: int, provider: str, now: datetime, ttl: timedelta
    ) -> str:
        self.issued.append((discord_user_id, provider, now, ttl))
        return "state-for-presser"


async def test_a_state_is_issued_for_the_presser() -> None:
    states = _States()
    result = await begin_link(
        PRESSER, provider=PROVIDER, identities=_Identities(), states=states, now=T0, ttl=TTL
    )
    assert result.state == "state-for-presser"
    assert states.issued == [(PRESSER, PROVIDER, T0, TTL)]


async def test_no_state_is_issued_for_the_mentioned_member() -> None:
    """Pressing Link account must never bind the mentioned member's identity."""
    states = _States()
    await begin_link(
        PRESSER, provider=PROVIDER, identities=_Identities(), states=states, now=T0, ttl=TTL
    )
    assert all(discord_id != MENTIONED for discord_id, *_ in states.issued)


async def test_an_already_linked_presser_gets_no_new_state() -> None:
    states = _States()
    result = await begin_link(
        PRESSER,
        provider=PROVIDER,
        identities=_Identities(identity=("outline-uuid", "Anna Example")),
        states=states,
        now=T0,
        ttl=TTL,
    )
    assert result.state is None
    assert result.linked_as == "Anna Example"
    assert states.issued == [], "a second state would strand the first one unspent"
