"""`ConsentCache`: Spec 3.1's second layer, made cheap enough for the voice-receive path.

The role check itself (an administrator bypassing channel permissions
without holding the role) is exercised in `test_sink.py` -- this suite
instead covers everything that check alone cannot: a role held with no
active consent record behind it, in either of the two ways that can happen
(Spec 3.2's policy-version change, and a hand-granted role), plus the
cache's own expiry behaviour.

And one property that is not about consent at all: `verdict()` must never
await. It is called from the single task draining captured audio, so a
database read there stalls every speaker's audio, not just the packet in
hand. Every test below therefore reads the verdict synchronously and only
then lets the refresh run.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from sturnus.domain.consent import ConsentRecord
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.repositories import ConsentRepository
from sturnus.infrastructure.discord.consent_cache import DEFAULT_STALE_AFTER, ConsentCache

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
POLICY = "2026-08-01"
GUILD_ID, USER_ID = 1, 100


class FakeClock:
    """Satisfies the `Clock` port with a value the test controls."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _granted(version: str = POLICY) -> ConsentRecord:
    return ConsentRecord(granted_at=T0, revoked_at=None, policy_version=version)


def _cache(
    clock: FakeClock,
    record: ConsentRecord | None,
    policy_version: str | None = POLICY,
    ttl: timedelta = timedelta(seconds=5),
) -> tuple[ConsentCache, AsyncMock, AsyncMock]:
    consent_repo = MagicMock(spec=ConsentRepository)
    consent_repo.current = AsyncMock(return_value=record)
    config_store = MagicMock(spec=ConfigStore)
    config_store.get = AsyncMock(return_value=policy_version)
    cache = ConsentCache(consent_repo, config_store, clock, ttl=ttl)
    return cache, consent_repo.current, config_store.get


async def _settled(cache: ConsentCache) -> None:
    """Lets whatever refresh `verdict()` scheduled actually run."""
    while cache.refresh_pending:
        await asyncio.sleep(0)


async def _warm(cache: ConsentCache, *, user_id: int = USER_ID) -> None:
    """Fills the cache the way the first frames of an utterance do."""
    cache.verdict(GUILD_ID, user_id, has_consent_role=True)
    await _settled(cache)


# --- the policy ---


async def test_role_without_a_consent_record_is_not_recorded() -> None:
    """A hand-granted role with no consent row behind it must not be enough."""
    cache, _, _ = _cache(FakeClock(T0), record=None)
    await _warm(cache)

    assert cache.verdict(GUILD_ID, USER_ID, has_consent_role=True) is False


async def test_record_predating_the_current_policy_version_is_not_recorded() -> None:
    """Spec 3.2: a policy-version bump invalidates old grants, even though
    nothing removes the role.
    """
    cache, _, _ = _cache(FakeClock(T0), record=_granted("2025-01-01"), policy_version=POLICY)
    await _warm(cache)

    assert cache.verdict(GUILD_ID, USER_ID, has_consent_role=True) is False


async def test_role_and_valid_record_is_recorded() -> None:
    cache, _, _ = _cache(FakeClock(T0), record=_granted(POLICY), policy_version=POLICY)
    await _warm(cache)

    assert cache.verdict(GUILD_ID, USER_ID, has_consent_role=True) is True


async def test_a_valid_record_without_the_role_is_not_recorded() -> None:
    """The record alone is not enough either -- both layers must agree."""
    cache, _, _ = _cache(FakeClock(T0), record=_granted(POLICY), policy_version=POLICY)
    await _warm(cache)

    assert cache.verdict(GUILD_ID, USER_ID, has_consent_role=False) is False


# --- the cache ---


async def test_an_unknown_user_answers_unknown_rather_than_guessing() -> None:
    """`None` is "not known yet", and the caller drops the frame.

    Audio whose consent we cannot vouch for is not recorded -- the same
    rule unattributed audio already follows.
    """
    cache, _, _ = _cache(FakeClock(T0), record=_granted(POLICY))

    assert cache.verdict(GUILD_ID, USER_ID, has_consent_role=True) is None


async def test_the_verdict_never_awaits_the_database() -> None:
    """The drain must not block on I/O; everything behind it is somebody's audio.

    The repository here never returns. A `verdict()` that awaited it would
    hang this test rather than answer.
    """

    async def never_answers(*_args: object) -> None:
        await asyncio.Event().wait()

    consent_repo = MagicMock(spec=ConsentRepository)
    consent_repo.current = AsyncMock(side_effect=never_answers)
    config_store = MagicMock(spec=ConfigStore)
    config_store.get = AsyncMock(return_value=POLICY)
    cache = ConsentCache(consent_repo, config_store, FakeClock(T0))

    for _ in range(50):  # a second of one speaker
        assert cache.verdict(GUILD_ID, USER_ID, has_consent_role=True) is None
    await asyncio.sleep(0)

    assert consent_repo.current.await_count == 1, "one refresh in flight, not one per frame"
    cache.cancel_refreshes()


async def test_repeated_calls_within_the_ttl_read_the_record_once() -> None:
    clock = FakeClock(T0)
    cache, current, get = _cache(clock, record=_granted(POLICY), ttl=timedelta(seconds=5))
    await _warm(cache)

    clock.advance(timedelta(seconds=1))
    assert cache.verdict(GUILD_ID, USER_ID, has_consent_role=True) is True
    clock.advance(timedelta(seconds=1))
    assert cache.verdict(GUILD_ID, USER_ID, has_consent_role=True) is True
    await _settled(cache)

    assert current.call_count == 1
    assert get.call_count == 1


async def test_a_stale_entry_answers_at_once_and_refreshes_behind_itself() -> None:
    """A revoked-by-policy-change user cannot be recorded forever just because
    an earlier call cached an active record -- but re-reading on the drain
    is what stalls every speaker, so the stale answer is served and the
    read happens beside it.
    """
    clock = FakeClock(T0)
    cache, current, _ = _cache(clock, record=_granted(POLICY), ttl=timedelta(seconds=5))
    await _warm(cache)
    assert current.call_count == 1

    clock.advance(timedelta(seconds=5))
    assert cache.verdict(GUILD_ID, USER_ID, has_consent_role=True) is True
    await _settled(cache)

    assert current.call_count == 2, "a stale entry must be refreshed, not reused forever"


async def test_a_failing_refresh_backs_off_instead_of_retrying_every_frame() -> None:
    """50 frames a second must not become 50 failing queries a second."""
    clock = FakeClock(T0)
    consent_repo = MagicMock(spec=ConsentRepository)
    consent_repo.current = AsyncMock(side_effect=RuntimeError("database gone"))
    config_store = MagicMock(spec=ConfigStore)
    config_store.get = AsyncMock(return_value=POLICY)
    cache = ConsentCache(consent_repo, config_store, clock, retry_backoff=timedelta(seconds=5))

    for _ in range(50):
        assert cache.verdict(GUILD_ID, USER_ID, has_consent_role=True) is None
        await _settled(cache)

    assert consent_repo.current.await_count == 1

    clock.advance(timedelta(seconds=5))
    cache.verdict(GUILD_ID, USER_ID, has_consent_role=True)
    await _settled(cache)

    assert consent_repo.current.await_count == 2, "the backoff expires; it is not a giving up"


async def test_different_users_are_cached_independently() -> None:
    clock = FakeClock(T0)
    consent_repo = MagicMock(spec=ConsentRepository)
    consent_repo.current = AsyncMock(side_effect=[_granted(POLICY), None])
    config_store = MagicMock(spec=ConfigStore)
    config_store.get = AsyncMock(return_value=POLICY)
    cache = ConsentCache(consent_repo, config_store, clock)

    await _warm(cache)
    await _warm(cache, user_id=USER_ID + 1)

    assert cache.verdict(GUILD_ID, USER_ID, has_consent_role=True) is True
    assert cache.verdict(GUILD_ID, USER_ID + 1, has_consent_role=True) is False
    assert consent_repo.current.call_count == 2


async def test_a_verdict_too_old_to_confirm_stops_being_served() -> None:
    """Serving a stale answer is bounded, and the bound is a consent property.

    With the database unreachable the cached verdict is all there is, and
    riding out a blip on it is the point of serving it at all. But an
    hour-old verdict authorising a recording nobody can still vouch for is
    not a thing to be relaxed about in a file people were told is a record
    of what they said, so past `stale_after` the answer becomes "not
    known" and the speaker stops being recorded.
    """
    clock = FakeClock(T0)
    cache, current, _ = _cache(clock, record=_granted(POLICY))
    await _warm(cache)
    assert cache.verdict(GUILD_ID, USER_ID, has_consent_role=True) is True

    current.side_effect = RuntimeError("database gone")
    clock.advance(DEFAULT_STALE_AFTER)

    assert cache.verdict(GUILD_ID, USER_ID, has_consent_role=True) is None
    await _settled(cache)


async def test_stale_after_shorter_than_the_ttl_is_refused() -> None:
    """A cache that expires its answers before it refreshes them can never serve one."""
    with pytest.raises(ValueError, match="stale_after"):
        ConsentCache(
            MagicMock(spec=ConsentRepository),
            MagicMock(spec=ConfigStore),
            FakeClock(T0),
            ttl=timedelta(seconds=5),
            stale_after=timedelta(seconds=1),
        )
