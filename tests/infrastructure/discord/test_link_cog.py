"""Tests for the `/link` commands (Plan 4 Task 4, Spec 8.4).

Each command's callback is invoked directly rather than through Discord's
dispatch machinery -- `app_commands.Command.callback` is the coroutine the
cog actually defines, so calling it exercises the decision the command
makes without standing up a gateway connection.

The assertion these tests exist for most is `ephemeral=True`. The
authorization URL carries a state that grants linking to the *invoking*
user's Discord identity, so anyone else who reads it can link their own
Outline account to that identity instead. The module docstring of
`link_cog` calls the ephemeral reply "the whole protection"; nothing
asserted it until now.
"""

from datetime import UTC, datetime, timedelta

import pytest

from sturnus.infrastructure.discord.link_cog import PROVIDER, STATE_TTL, LinkCog

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
ANNA = 100


class _Clock:
    def now(self) -> datetime:
        return T0


class _Response:
    """Records what the command replied with, and how."""

    def __init__(self) -> None:
        self.content: str | None = None
        self.ephemeral: bool | None = None

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        self.content = content
        self.ephemeral = ephemeral


class _User:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _Interaction:
    """Only the two attributes the cog touches."""

    def __init__(self, user_id: int = ANNA) -> None:
        self.user = _User(user_id)
        self.response = _Response()


class _Links:
    """Stands in for `AccountLinkRepository`."""

    def __init__(self, identity: tuple[str, str] | None = None) -> None:
        self._identity = identity
        self.deleted: list[tuple[int, str]] = []

    async def external_identity(
        self, _discord_user_id: int, _provider: str | None = None
    ) -> tuple[str, str] | None:
        return self._identity

    async def delete(self, discord_user_id: int, provider: str) -> bool:
        self.deleted.append((discord_user_id, provider))
        return self._identity is not None


class _States:
    """Stands in for `LinkStateStore`, recording what a state was issued for."""

    def __init__(self) -> None:
        self.issued: list[tuple[int, str, datetime, timedelta]] = []

    async def issue(
        self, discord_user_id: int, provider: str, now: datetime, ttl: timedelta
    ) -> str:
        self.issued.append((discord_user_id, provider, now, ttl))
        return "state-for-anna"


class _OAuth:
    """Stands in for `OutlineOAuth`, echoing the state into the URL."""

    def authorize_url(self, state: str) -> str:
        return f"https://outline.example/oauth/authorize?state={state}"


def cog(
    links: _Links | None = None, states: _States | None = None
) -> tuple[LinkCog, _Links, _States]:
    links = links or _Links()
    states = states or _States()
    return (
        LinkCog(oauth=_OAuth(), states=states, links=links, clock=_Clock()),  # type: ignore[arg-type]
        links,
        states,
    )


async def invoke(cog: LinkCog, command: str, interaction: _Interaction) -> None:
    """Calls one command's own coroutine, bypassing Discord's dispatch.

    `app_commands.Command.callback` is typed as though `self` were already
    bound, but on a cog's command it is the plain function and takes the cog.
    Reaching it by name keeps that mismatch in one place instead of at every
    call site.
    """
    await getattr(cog, command).callback(cog, interaction)


async def test_an_unlinked_user_is_given_an_authorization_url() -> None:
    command, _, _ = cog()
    interaction = _Interaction()

    await invoke(command, "start", interaction)

    assert interaction.response.content is not None
    assert "https://outline.example/oauth/authorize?state=state-for-anna" in (
        interaction.response.content
    )


async def test_the_authorization_url_is_never_shown_to_the_channel() -> None:
    """The URL grants linking to the invoking identity -- see the module docstring."""
    command, _, _ = cog()
    interaction = _Interaction()

    await invoke(command, "start", interaction)

    assert interaction.response.ephemeral is True


async def test_the_state_is_issued_for_the_invoking_user() -> None:
    """A state issued for anyone else would let them link as this user."""
    command, _, states = cog()
    interaction = _Interaction(user_id=ANNA)

    await invoke(command, "start", interaction)

    assert states.issued == [(ANNA, PROVIDER, T0, STATE_TTL)]


async def test_an_already_linked_user_is_told_so_and_pointed_at_remove() -> None:
    """Issuing a second state here would strand the first one unspent."""
    command, _, states = cog(links=_Links(identity=("outline-uuid", "Anna Example")))
    interaction = _Interaction()

    await invoke(command, "start", interaction)

    assert interaction.response.content is not None
    assert "Anna Example" in interaction.response.content
    assert "/link remove" in interaction.response.content
    assert interaction.response.ephemeral is True
    assert states.issued == []


async def test_removing_an_existing_link_reports_it_gone() -> None:
    command, links, _ = cog(links=_Links(identity=("outline-uuid", "Anna Example")))
    interaction = _Interaction()

    await invoke(command, "remove", interaction)

    assert links.deleted == [(ANNA, PROVIDER)]
    assert interaction.response.content is not None
    assert "removed" in interaction.response.content.lower()
    assert interaction.response.ephemeral is True


async def test_removing_a_link_says_what_it_does_not_undo() -> None:
    """Documents already published keep the name they were written with."""
    command, _, _ = cog(links=_Links(identity=("outline-uuid", "Anna Example")))
    interaction = _Interaction()

    await invoke(command, "remove", interaction)

    assert interaction.response.content is not None
    assert "already published" in interaction.response.content


async def test_removing_when_nothing_was_linked_says_so() -> None:
    """Reporting success here would leave someone believing a link was undone."""
    command, _, _ = cog()
    interaction = _Interaction()

    await invoke(command, "remove", interaction)

    assert interaction.response.content is not None
    assert "nothing to remove" in interaction.response.content
    assert interaction.response.ephemeral is True


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        (None, "not linked yet"),
        (("outline-uuid", "Anna Example"), "Anna Example"),
    ],
)
async def test_status_reports_the_stored_link(
    identity: tuple[str, str] | None, expected: str
) -> None:
    command, _, _ = cog(links=_Links(identity=identity))
    interaction = _Interaction()

    await invoke(command, "status", interaction)

    assert interaction.response.content is not None
    assert expected in interaction.response.content
    assert interaction.response.ephemeral is True
