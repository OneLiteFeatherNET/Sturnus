"""The signed cookie that is the console's entire notion of a session.

There is no server-side session store: the cookie *is* the session. That
buys a stateless API -- a restart signs nobody out, and a second replica
needs no shared state -- and it costs exactly one thing, which is that the
signature has to be right. Everything below is that signature.

The threat is not subtle. A forgeable cookie is an unauthenticated person
choosing whose recordings to listen to.
"""

from datetime import UTC, datetime, timedelta

import pytest

from sturnus.console.session import (
    ExpiredSession,
    InvalidSession,
    SessionCookie,
    SignedSession,
)

SECRET = "a" * 32
OTHER_SECRET = "b" * 32
T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
ANNA = 100


@pytest.fixture
def cookie() -> SessionCookie:
    return SessionCookie(SECRET, lifetime=timedelta(hours=12))


def test_a_signed_session_reads_back_as_the_person_it_named(cookie: SessionCookie) -> None:
    token = cookie.issue(SignedSession(discord_user_id=ANNA), now=T0)
    assert cookie.read(token, now=T0).discord_user_id == ANNA


def test_a_token_signed_with_another_secret_is_refused(cookie: SessionCookie) -> None:
    """The forgery case. Everything else here is about handling; this is
    the one that decides whether the console has authentication at all.
    """
    forged = SessionCookie(OTHER_SECRET, lifetime=timedelta(hours=12)).issue(
        SignedSession(discord_user_id=ANNA), now=T0
    )
    with pytest.raises(InvalidSession):
        cookie.read(forged, now=T0)


def test_a_tampered_payload_is_refused(cookie: SessionCookie) -> None:
    """Changing the user id without re-signing must not work -- that edit
    is precisely "listen to somebody else's recordings".
    """
    token = cookie.issue(SignedSession(discord_user_id=ANNA), now=T0)
    payload, _, signature = token.partition(".")
    tampered = payload.replace("MTAw", "MjAw") + "." + signature
    with pytest.raises(InvalidSession):
        cookie.read(tampered, now=T0)


def test_a_token_whose_signature_was_truncated_is_refused(cookie: SessionCookie) -> None:
    token = cookie.issue(SignedSession(discord_user_id=ANNA), now=T0)
    with pytest.raises(InvalidSession):
        cookie.read(token[:-4], now=T0)


def test_a_token_with_no_signature_at_all_is_refused(cookie: SessionCookie) -> None:
    token = cookie.issue(SignedSession(discord_user_id=ANNA), now=T0)
    with pytest.raises(InvalidSession):
        cookie.read(token.partition(".")[0], now=T0)


def test_gibberish_is_refused_rather_than_crashing(cookie: SessionCookie) -> None:
    """A cookie is attacker-controlled input. Every malformed shape has to
    come back as one refusal, not as a stack trace that reveals which
    parsing step it reached.
    """
    for value in ("", ".", "..", "not-base64.not-base64", "a.b.c", "\x00.\x00"):
        with pytest.raises(InvalidSession):
            cookie.read(value, now=T0)


def test_a_session_expires(cookie: SessionCookie) -> None:
    token = cookie.issue(SignedSession(discord_user_id=ANNA), now=T0)
    with pytest.raises(ExpiredSession):
        cookie.read(token, now=T0 + timedelta(hours=12, seconds=1))


def test_a_session_is_valid_right_up_to_its_expiry(cookie: SessionCookie) -> None:
    token = cookie.issue(SignedSession(discord_user_id=ANNA), now=T0)
    assert cookie.read(token, now=T0 + timedelta(hours=11, minutes=59)).discord_user_id == ANNA


def test_expiry_is_distinguishable_from_forgery() -> None:
    """Two different exceptions because they need two different responses:
    an expired session is sent back through the login flow, a forged one is
    refused. Collapsing them would send an attacker to the login page --
    harmless -- but would also make an ordinary user's expiry
    indistinguishable from an attack in the logs.
    """
    assert not issubclass(ExpiredSession, InvalidSession)
    assert not issubclass(InvalidSession, ExpiredSession)


def test_the_expiry_inside_the_token_cannot_be_extended_by_the_holder(
    cookie: SessionCookie,
) -> None:
    """The expiry is inside the signed payload, not beside it.

    A cookie whose lifetime lived only in the browser's `Max-Age` would be
    extended by anyone who kept the value -- and a stolen session would
    then never end.
    """
    token = cookie.issue(SignedSession(discord_user_id=ANNA), now=T0)
    payload, _, signature = token.partition(".")
    import base64
    import json

    decoded = json.loads(base64.urlsafe_b64decode(payload + "=="))
    decoded["exp"] = decoded["exp"] + 86_400
    extended = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
    with pytest.raises(InvalidSession):
        cookie.read(extended + "." + signature, now=T0)


def test_a_secret_shorter_than_the_hash_is_refused_at_construction() -> None:
    """A short secret is a weak signature, and the failure mode is silent.

    Refusing at construction means a misconfigured deployment fails at
    startup rather than serving forgeable sessions.
    """
    with pytest.raises(ValueError, match="32"):
        SessionCookie("too-short", lifetime=timedelta(hours=12))


def test_two_tokens_for_the_same_person_at_the_same_moment_are_equal(
    cookie: SessionCookie,
) -> None:
    """Deterministic, which is what makes the tests above meaningful: a
    random nonce would hide a signature that ignored its payload.
    """
    first = cookie.issue(SignedSession(discord_user_id=ANNA), now=T0)
    second = cookie.issue(SignedSession(discord_user_id=ANNA), now=T0)
    assert first == second
