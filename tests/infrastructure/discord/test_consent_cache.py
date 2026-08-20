"""`ConsentCache`: Spec 3.1's second layer, made cheap enough for the voice-receive path.

The role check itself (an administrator bypassing channel permissions
without holding the role) is exercised as part of `VoiceReceiveAdapter`'s
packet callback, which has no unit tests (see `voice.py`'s module
docstring) -- this suite instead covers everything that check alone
cannot: a role held with no active consent record behind it, in either
of the two ways that can happen (Spec 3.2's policy-version change, and a
hand-granted role), plus the cache's own expiry behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from sturnus.domain.consent import ConsentRecord
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.repositories import ConsentRepository
from sturnus.infrastructure.discord.consent_cache import ConsentCache

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


async def test_role_without_a_consent_record_is_not_recorded() -> None:
    """A hand-granted role with no consent row behind it must not be enough."""
    cache, _, _ = _cache(FakeClock(T0), record=None)
    assert await cache.may_record(GUILD_ID, USER_ID, has_consent_role=True) is False


async def test_record_predating_the_current_policy_version_is_not_recorded() -> None:
    """Spec 3.2: a policy-version bump invalidates old grants, even though
    nothing removes the role.
    """
    cache, _, _ = _cache(FakeClock(T0), record=_granted("2025-01-01"), policy_version=POLICY)
    assert await cache.may_record(GUILD_ID, USER_ID, has_consent_role=True) is False


async def test_role_and_valid_record_is_recorded() -> None:
    cache, _, _ = _cache(FakeClock(T0), record=_granted(POLICY), policy_version=POLICY)
    assert await cache.may_record(GUILD_ID, USER_ID, has_consent_role=True) is True


async def test_a_valid_record_without_the_role_is_not_recorded() -> None:
    """The record alone is not enough either -- both layers must agree."""
    cache, _, _ = _cache(FakeClock(T0), record=_granted(POLICY), policy_version=POLICY)
    assert await cache.may_record(GUILD_ID, USER_ID, has_consent_role=False) is False


async def test_repeated_calls_within_the_ttl_read_the_record_once() -> None:
    clock = FakeClock(T0)
    cache, current, get = _cache(clock, record=_granted(POLICY), ttl=timedelta(seconds=5))

    clock.advance(timedelta(seconds=1))
    assert await cache.may_record(GUILD_ID, USER_ID, has_consent_role=True) is True
    clock.advance(timedelta(seconds=1))
    assert await cache.may_record(GUILD_ID, USER_ID, has_consent_role=True) is True

    assert current.call_count == 1
    assert get.call_count == 1


async def test_the_cache_expires_and_refreshes_after_the_ttl() -> None:
    """A revoked-by-policy-change user cannot be recorded forever just because
    an earlier call cached an active record -- the cache must re-read after
    `ttl` elapses so the next fetch can pick up the change.
    """
    clock = FakeClock(T0)
    cache, current, _ = _cache(clock, record=_granted(POLICY), ttl=timedelta(seconds=5))

    assert await cache.may_record(GUILD_ID, USER_ID, has_consent_role=True) is True
    assert current.call_count == 1

    clock.advance(timedelta(seconds=5))
    assert await cache.may_record(GUILD_ID, USER_ID, has_consent_role=True) is True
    assert current.call_count == 2, "a stale entry must be refreshed, not reused forever"


async def test_different_users_are_cached_independently() -> None:
    clock = FakeClock(T0)
    consent_repo = MagicMock(spec=ConsentRepository)
    consent_repo.current = AsyncMock(side_effect=[_granted(POLICY), None])
    config_store = MagicMock(spec=ConfigStore)
    config_store.get = AsyncMock(return_value=POLICY)
    cache = ConsentCache(consent_repo, config_store, clock)

    assert await cache.may_record(GUILD_ID, USER_ID, has_consent_role=True) is True
    assert await cache.may_record(GUILD_ID, USER_ID + 1, has_consent_role=True) is False
    assert consent_repo.current.call_count == 2
