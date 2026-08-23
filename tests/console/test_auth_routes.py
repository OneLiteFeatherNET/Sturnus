"""Signing in to the console, end to end through the real routes.

The flow has one property that carries all the others: **an Outline
identity is not an identity here until `account_link` says which Discord
user it is.** Every query the console will ever run is scoped by Discord
id, because that is what `session_participant` names -- so a login that
issued a session without that lookup would be a session that can be
scoped to nobody, or worse, to everybody.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiohttp import web

from sturnus.console.audio import AudioDelivery
from sturnus.console.session import SessionCookie, SignedSession
from sturnus.infrastructure.documents.outline_oauth import ExternalIdentity
from tests.console.conftest import (
    ANNA,
    ANNA_OUTLINE,
    SECRET,
    T0,
    AiohttpClientFactory,
    FakeAdmins,
    FakeAudioSource,
    FakeKeys,
    FakeLinks,
    FakeOAuth,
    FakeReads,
    FakeSignInClients,
    FakeStates,
    FakeTracks,
    build_test_api,
    now_at,
)

SESSION_COOKIE = "sturnus_session"


def app(
    oauth: FakeOAuth | None = None,
    states: FakeStates | None = None,
    links: FakeLinks | None = None,
    admins: FakeAdmins | None = None,
    schema_ready: bool = True,
) -> web.Application:
    return build_test_api(
        oauth=oauth or FakeOAuth(),
        states=states or FakeStates(),
        links=links or FakeLinks(),
        admins=admins or FakeAdmins(),
        reads=FakeReads(),
        sessions=SessionCookie(SECRET, timedelta(hours=12)),
        now=now_at(),
        schema_ready=schema_ready,
        audio=AudioDelivery(tracks=FakeTracks(), source=FakeAudioSource(), keys=FakeKeys()),
    )


def token(discord_user_id: int = ANNA) -> str:
    return SessionCookie(SECRET, timedelta(hours=12)).issue(SignedSession(discord_user_id), now=T0)


# ---------------------------------------------------------------------------
# Health, which Kubernetes reaches before anything else does
# ---------------------------------------------------------------------------


async def test_healthz_answers_before_the_schema_exists(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Liveness must not depend on the worker having migrated yet, or a
    fresh deploy restarts this process forever while it waits.
    """
    client = await aiohttp_client(app(schema_ready=False))
    assert (await client.get("/healthz")).status == 200


async def test_readyz_waits_for_the_schema(aiohttp_client: AiohttpClientFactory) -> None:
    client = await aiohttp_client(app(schema_ready=False))
    assert (await client.get("/readyz")).status == 503


async def test_readyz_passes_once_the_schema_is_there(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(app(schema_ready=True))
    assert (await client.get("/readyz")).status == 200


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


async def test_login_redirects_to_the_provider(aiohttp_client: AiohttpClientFactory) -> None:
    oauth = FakeOAuth()
    client = await aiohttp_client(app(oauth=oauth))
    response = await client.get("/api/auth/login", allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"].startswith("https://outline.example/oauth/authorize")


async def test_login_issues_a_single_use_state(aiohttp_client: AiohttpClientFactory) -> None:
    """The state is what ties a callback to a login this server started.

    Without it, anyone can deliver a code to the callback and have a
    session minted from it.
    """
    states, oauth = FakeStates(), FakeOAuth()
    client = await aiohttp_client(app(oauth=oauth, states=states))
    await client.get("/api/auth/login", allow_redirects=False)
    assert len(states.issued) == 1
    assert oauth.authorize_calls == states.issued


async def test_two_logins_do_not_share_a_state(aiohttp_client: AiohttpClientFactory) -> None:
    states = FakeStates()
    client = await aiohttp_client(app(states=states))
    await client.get("/api/auth/login", allow_redirects=False)
    await client.get("/api/auth/login", allow_redirects=False)
    assert len(set(states.issued)) == 2


async def test_a_successful_callback_sets_a_session_cookie(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    states = FakeStates()
    client = await aiohttp_client(app(states=states))
    await client.get("/api/auth/login", allow_redirects=False)

    response = await client.get(
        f"/api/auth/callback?code=abc&state={states.issued[0]}", allow_redirects=False
    )

    assert response.status == 302
    assert SESSION_COOKIE in response.cookies


async def test_the_session_cookie_is_not_readable_by_scripts(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """`HttpOnly` is what keeps one cross-site scripting bug anywhere on
    the origin from becoming "every recording this person was in".
    """
    states = FakeStates()
    client = await aiohttp_client(app(states=states))
    await client.get("/api/auth/login", allow_redirects=False)
    response = await client.get(
        f"/api/auth/callback?code=abc&state={states.issued[0]}", allow_redirects=False
    )
    cookie = response.cookies[SESSION_COOKIE]
    assert cookie["httponly"]
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/"


async def test_a_callback_with_an_unknown_state_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A code delivered without a state this server issued is not a login."""
    client = await aiohttp_client(app())
    response = await client.get(
        "/api/auth/callback?code=abc&state=never-issued", allow_redirects=False
    )
    assert response.status == 400
    assert SESSION_COOKIE not in response.cookies


async def test_a_state_cannot_be_replayed(aiohttp_client: AiohttpClientFactory) -> None:
    """Single use, or a captured callback URL is a session forever."""
    states = FakeStates()
    client = await aiohttp_client(app(states=states))
    await client.get("/api/auth/login", allow_redirects=False)
    url = f"/api/auth/callback?code=abc&state={states.issued[0]}"

    assert (await client.get(url, allow_redirects=False)).status == 302
    assert (await client.get(url, allow_redirects=False)).status == 400


async def test_a_callback_missing_its_parameters_is_refused(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(app())
    for url in ("/api/auth/callback", "/api/auth/callback?code=abc", "/api/auth/callback?state=x"):
        assert (await client.get(url, allow_redirects=False)).status == 400


async def test_a_provider_that_refuses_the_exchange_yields_no_session(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    states = FakeStates()
    client = await aiohttp_client(app(oauth=FakeOAuth(fail=True), states=states))
    await client.get("/api/auth/login", allow_redirects=False)
    response = await client.get(
        f"/api/auth/callback?code=abc&state={states.issued[0]}", allow_redirects=False
    )
    assert response.status == 403
    assert SESSION_COOKIE not in response.cookies


async def test_an_identity_with_no_linked_discord_account_gets_no_session(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The authorisation model in one test.

    Every console query is scoped by Discord id, and the only bridge from
    an Outline identity to one is a link the person made themselves with
    `/link`. Without it there is nobody to scope to -- so there is no
    session, rather than a session scoped to nothing.
    """
    states = FakeStates()
    client = await aiohttp_client(app(states=states, links=FakeLinks(mapping={})))
    await client.get("/api/auth/login", allow_redirects=False)

    response = await client.get(
        f"/api/auth/callback?code=abc&state={states.issued[0]}", allow_redirects=False
    )

    assert response.status == 403
    assert SESSION_COOKIE not in response.cookies


async def test_the_session_names_the_discord_user_not_the_outline_one(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    states = FakeStates()
    links = FakeLinks(mapping={ANNA_OUTLINE: ANNA})
    client = await aiohttp_client(
        app(states=states, links=links, oauth=FakeOAuth(ExternalIdentity(ANNA_OUTLINE, "Anna")))
    )
    await client.get("/api/auth/login", allow_redirects=False)
    issued = await client.get(
        f"/api/auth/callback?code=abc&state={states.issued[0]}", allow_redirects=False
    )

    # Carried across by hand: the cookie is `Secure`, and aiohttp's jar
    # correctly refuses to store one over the test server's plain http.
    # That is the jar behaving properly, not the server misbehaving -- in
    # production this hop is TLS all the way.
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: issued.cookies[SESSION_COOKIE].value})

    me = await (await client.get("/api/me")).json()
    assert me["discord_user_id"] == str(ANNA)


# ---------------------------------------------------------------------------
# Who the session says you are
# ---------------------------------------------------------------------------


async def test_me_without_a_session_is_unauthorised(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(app())
    assert (await client.get("/api/me")).status == 401


async def test_me_with_a_forged_cookie_is_unauthorised(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(app())
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: "forged.token"})
    assert (await client.get("/api/me")).status == 401


async def test_me_reports_whether_the_person_administers_anything(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The console hides the settings section on this, and the API refuses
    it independently -- a hidden section is a courtesy, never a control.
    """
    client = await aiohttp_client(app(admins=FakeAdmins({ANNA})))
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(ANNA)})
    assert (await (await client.get("/api/me")).json())["is_admin"] is True


async def test_someone_who_administers_nothing_is_not_an_admin(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    client = await aiohttp_client(app(admins=FakeAdmins(set())))
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(ANNA)})
    assert (await (await client.get("/api/me")).json())["is_admin"] is False


async def test_a_discord_id_is_serialised_as_a_string(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A Discord snowflake exceeds JavaScript's safe integer range, and a
    JSON number silently loses its last digits there -- producing an id
    that looks right and names nobody.
    """
    big = 386950399101370374
    client = await aiohttp_client(app())
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token(big)})
    body = await (await client.get("/api/me")).json()
    assert body["discord_user_id"] == str(big)


# ---------------------------------------------------------------------------
# Signing out
# ---------------------------------------------------------------------------


async def test_logout_clears_the_cookie(aiohttp_client: AiohttpClientFactory) -> None:
    client = await aiohttp_client(app())
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: token()})
    response = await client.post("/api/auth/logout", allow_redirects=False)
    assert response.status == 204
    assert response.cookies[SESSION_COOKIE].value == ""


async def test_logout_without_a_session_is_not_an_error(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """Signing out of nothing is what a stale tab does, and answering 401
    would make the console show an error for a successful outcome.
    """
    client = await aiohttp_client(app())
    assert (await client.post("/api/auth/logout")).status == 204


@pytest.mark.parametrize("path", ["/api/me"])
async def test_an_expired_session_is_unauthorised_rather_than_a_server_error(
    aiohttp_client: AiohttpClientFactory, path: str
) -> None:
    client = await aiohttp_client(app())
    expired = SessionCookie(SECRET, timedelta(seconds=-1)).issue(SignedSession(ANNA), now=T0)
    client.session.cookie_jar.update_cookies({SESSION_COOKIE: expired})
    assert (await client.get(path)).status == 401


# ---------------------------------------------------------------------------
# Signing in through a guild's own link
# ---------------------------------------------------------------------------


ACME, OTHER = 4711, 9911


def guild_app(
    guilds: dict[str, tuple[int, FakeOAuth]] | None = None,
    environment: FakeOAuth | None = None,
    states: FakeStates | None = None,
    links: FakeLinks | None = None,
) -> web.Application:
    """An application whose sign-in can resolve a guild's own client."""
    return build_test_api(
        clients=FakeSignInClients(environment=environment or FakeOAuth(), guilds=guilds or {}),
        states=states or FakeStates(),
        links=links or FakeLinks(),
        sessions=SessionCookie(SECRET, timedelta(hours=12)),
        now=now_at(),
    )


def acme_oauth() -> FakeOAuth:
    """A client recognisable in a `Location` header as not the default one."""
    return FakeOAuth(base_url="https://outline.acme.example")


async def test_a_guilds_link_redirects_to_that_guilds_provider(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The whole of 2.2 in one request.

    `/api/auth/login` takes no parameters and reads no cookie, so the
    slug in the URL is the only thing that can name a guild before the
    round trip begins -- and here it does.
    """
    client = await aiohttp_client(guild_app(guilds={"acme": (ACME, acme_oauth())}))

    response = await client.get("/api/auth/login?guild=acme", allow_redirects=False)

    assert response.status == 302
    assert response.headers["Location"].startswith("https://outline.acme.example/")


async def test_a_guilds_sign_in_issues_a_state_that_names_the_guild(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The state is what selects the client for the code exchange.

    Nothing else survives the round trip: the callback has no session to
    read a guild from, which is the problem the guild-specific link
    exists to solve.
    """
    states = FakeStates()
    client = await aiohttp_client(guild_app(guilds={"acme": (ACME, acme_oauth())}, states=states))

    await client.get("/api/auth/login?guild=acme", allow_redirects=False)

    assert states.issued_for[states.issued[0]] == ACME


async def test_a_sign_in_with_no_guild_behaves_exactly_as_it_did(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The compatibility promise 2.2 makes, pinned.

    A deployment that never configures a per-guild client must behave
    identically to v0.15.0: the environment-configured client, and a
    state that names no guild.
    """
    states, environment = FakeStates(), FakeOAuth()
    client = await aiohttp_client(
        guild_app(guilds={"acme": (ACME, acme_oauth())}, environment=environment, states=states)
    )

    response = await client.get("/api/auth/login", allow_redirects=False)

    assert response.status == 302
    assert response.headers["Location"].startswith("https://outline.example/oauth/authorize")
    assert environment.authorize_calls == states.issued
    assert states.issued_for[states.issued[0]] is None


async def test_an_unknown_slug_answers_exactly_as_a_misconfigured_one(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The property the owner chose this design to keep.

    A slug that names nothing and a slug that names a guild whose client
    cannot complete a sign-in must be one answer, byte for byte.
    Otherwise anybody with a browser can walk a list of names and learn
    which organisations use this service -- which is precisely what the
    rejected alternative, a public list of guilds, would have done.
    """
    # `half-configured` is registered to no usable client, exactly as a
    # guild whose secret has not been supplied yet resolves.
    client = await aiohttp_client(guild_app(guilds={"acme": (ACME, acme_oauth())}))

    unknown = await client.get("/api/auth/login?guild=no-such-guild", allow_redirects=False)
    misconfigured = await client.get("/api/auth/login?guild=half-configured", allow_redirects=False)

    assert unknown.status == misconfigured.status == 404
    assert await unknown.json() == await misconfigured.json()
    assert "location" not in {header.lower() for header in unknown.headers}


@pytest.mark.parametrize(
    "slug", ["API", "acme%2f..%2fx", "1289374650912837465", "api", "a", "acme--x"]
)
async def test_a_slug_that_is_not_a_slug_answers_the_same_404(
    aiohttp_client: AiohttpClientFactory, slug: str
) -> None:
    """A refused shape must not be a different reply from a refused name.

    A 400 for "that is not a valid slug" and a 404 for "no such slug"
    would together tell an attacker which of the names they tried were
    well-formed but unregistered, which is half the enumeration back.
    """
    client = await aiohttp_client(guild_app(guilds={"acme": (ACME, acme_oauth())}))
    response = await client.get(f"/api/auth/login?guild={slug}", allow_redirects=False)
    assert response.status == 404
    assert await response.json() == {"error": "no such sign-in link"}


async def test_the_refusal_reflects_nothing_of_what_was_asked_for(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """The rule the whole of `sturnus.console.app` follows.

    This endpoint is unauthenticated and takes a string straight out of a
    URL; an error body that echoed it would be an XSS sink reachable by
    anybody.
    """
    client = await aiohttp_client(guild_app())
    response = await client.get(
        "/api/auth/login?guild=%3Cscript%3Ealert(1)%3C/script%3E", allow_redirects=False
    )
    assert response.status == 404
    assert "script" not in await response.text()


async def test_the_state_and_not_the_callback_selects_the_client(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """A code is exchanged against the client the sign-in began with.

    Two guilds are configured; the sign-in begins on one of them, and the
    exchange must happen there. Nothing in the callback URL names a
    guild, which is what stops a caller asking this process to spend one
    guild's client secret on another guild's code.
    """
    acme, other = acme_oauth(), acme_oauth()
    acme.identity = ExternalIdentity(ANNA_OUTLINE, "Anna")
    states = FakeStates()
    client = await aiohttp_client(
        guild_app(guilds={"acme": (ACME, acme), "other": (OTHER, other)}, states=states)
    )
    await client.get("/api/auth/login?guild=acme", allow_redirects=False)

    response = await client.get(
        f"/api/auth/callback?code=abc&state={states.issued[0]}", allow_redirects=False
    )

    assert response.status == 302
    assert acme.exchanges == ["abc"]
    assert other.exchanges == []


async def test_a_sign_in_with_no_guild_still_exchanges_against_the_environment(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    environment, acme = FakeOAuth(), acme_oauth()
    states = FakeStates()
    client = await aiohttp_client(
        guild_app(guilds={"acme": (ACME, acme)}, environment=environment, states=states)
    )
    await client.get("/api/auth/login", allow_redirects=False)

    await client.get(f"/api/auth/callback?code=abc&state={states.issued[0]}", allow_redirects=False)

    assert environment.exchanges == ["abc"]
    assert acme.exchanges == []


async def test_a_callback_for_a_registration_that_went_away_mints_no_session(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """An administrator can delete a registration mid-login.

    The state is valid and there is nothing to exchange with, so it
    answers as an unknown sign-in attempt does -- and above all it does
    not fall back to the environment-configured client, which would sign
    somebody in through a client their guild deliberately replaced.
    """
    states = FakeStates()
    clients = FakeSignInClients(guilds={"acme": (ACME, acme_oauth())})
    app_with_removal = build_test_api(
        clients=clients,
        states=states,
        sessions=SessionCookie(SECRET, timedelta(hours=12)),
        now=now_at(),
    )
    client = await aiohttp_client(app_with_removal)
    await client.get("/api/auth/login?guild=acme", allow_redirects=False)
    clients.guilds.clear()

    response = await client.get(
        f"/api/auth/callback?code=abc&state={states.issued[0]}", allow_redirects=False
    )

    assert response.status == 400
    assert SESSION_COOKIE not in response.cookies


async def test_an_empty_guild_parameter_is_not_the_same_as_no_guild(
    aiohttp_client: AiohttpClientFactory,
) -> None:
    """`?guild=` asked for a guild and named none of them.

    Treating it as "no guild" would sign somebody in against the
    deployment's own client on a link that was meant to select a guild's
    -- which is the one substitution this whole design exists to prevent.
    It is refused like any other slug that resolves to nothing.
    """
    client = await aiohttp_client(guild_app(guilds={"acme": (ACME, acme_oauth())}))
    response = await client.get("/api/auth/login?guild=", allow_redirects=False)
    assert response.status == 404
    assert await response.json() == {"error": "no such sign-in link"}
