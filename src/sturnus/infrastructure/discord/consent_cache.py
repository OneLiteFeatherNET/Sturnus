"""A short-lived cache of the stored consent record, for the voice-receive path.

Spec 3.1's second layer requires every packet to be checked against the
stored consent record, not just the Discord role -- an administrator
bypasses channel permissions and can speak without the role, and (Spec
3.2) a policy-version change must retroactively invalidate a grant even
though nothing removes the role. But voice packets arrive every 20ms per
active speaker, and a database read on that path is out of the question.

This cache holds each `(guild_id, discord_user_id)` pair's `ConsentRecord`
and the guild's current `policy_version` in memory, refreshed only after
`ttl` has elapsed, so most packets are checked against an in-memory value
instead of the database.

**`verdict()` never awaits.** It is called from the single task that
drains captured audio, and that task must not block on I/O: everything
behind it is every speaker's audio, so one slow query stalls the whole
channel's recording, not just the packet in hand. The refresh therefore
happens *beside* the drain, in a task of its own, and `verdict()` answers
from whatever is currently cached:

* a cached entry answers immediately, and a stale one additionally
  schedules its own refresh (read-through would have been a database round
  trip on the drain every `ttl` per speaker);
* nothing cached answers `None`, meaning "not known yet". The caller does
  not record that frame. Audio whose consent we cannot vouch for is not
  written, which is the same rule unattributed audio already follows, and
  the honest direction to err in. It costs the first frames of a
  speaker's first utterance in a session -- ~20-60 ms against a healthy
  database -- and nothing after that, because the entry then exists.

Serving a stale answer is bounded, and that bound is a consent property
rather than a performance one. An entry older than `stale_after` is not
served at all -- it answers "not known" and the speaker stops being
recorded. Otherwise a database outage would leave a verdict from an hour
ago authorising a recording nobody could still vouch for, which is not a
thing to be relaxed about in a file people were told is a record of what
they said. A refresh that fails also backs off rather than being retried on
the next frame, so an outage cannot turn 50 frames a second into 50 failing
queries a second.

TTL: 5 seconds. The role is still checked directly, per packet, by the
caller before this cache is even consulted (`RecordingSink`), so an
explicit `/consent revoke` -- which also removes the role -- is caught on
the very next packet, unaffected by this cache's staleness. This cache
only guards the two paths where the role alone lies: a `policy_version`
bump invalidating an old grant, and an administrator handing out the role
by hand with no consent row behind it. Neither is a per-packet, click-a-
button event -- both are slow, deliberate administrative actions -- so a
bounded few-second delay before the change takes effect is an acceptable
trade for turning up to 50 queries/second/speaker into one query every
five seconds. Shorter would buy little (a policy rollout is not
seconds-sensitive) while pushing database load toward per-packet; longer
would widen the window in which a hand-granted, never-consenting member
stays recordable after being caught by an audit.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sturnus.application.ports import Clock
from sturnus.domain import settings
from sturnus.domain.consent import ConsentRecord, may_record
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.repositories import ConsentRepository

log = logging.getLogger(__name__)

#: See the module docstring for why this value.
DEFAULT_TTL = timedelta(seconds=5)

#: How long a failed refresh waits before another is attempted for the same
#: pair. Without it, a database that is down would be asked again by every
#: single frame.
DEFAULT_RETRY_BACKOFF = timedelta(seconds=5)

#: How stale a cached verdict may get before it stops being served at all.
#: Twelve refresh intervals: long enough to ride out a database blip or a
#: failover without interrupting a recording, short enough that nobody is
#: recorded for minutes on the strength of a verdict that can no longer be
#: confirmed.
DEFAULT_STALE_AFTER = timedelta(seconds=60)


@dataclass
class _Entry:
    record: ConsentRecord | None
    policy_version: str
    fetched_at: datetime


class ConsentCache:
    """Per-`(guild_id, discord_user_id)` cache of the stored consent record.

    `verdict` is the entry point and never blocks; `refresh_pending`
    reports whether any refresh is still in flight, which is what a test
    waits on. There is no explicit invalidation -- entries simply expire
    after `ttl`, which is enough because the record changes only through
    slow, human-driven paths (`/consent grant`, `/consent revoke`, an
    administrator editing roles, `/config set policy_version`), never
    per-packet.
    """

    def __init__(
        self,
        consent_repo: ConsentRepository,
        config_store: ConfigStore,
        clock: Clock,
        ttl: timedelta = DEFAULT_TTL,
        retry_backoff: timedelta = DEFAULT_RETRY_BACKOFF,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
    ) -> None:
        if stale_after < ttl:
            raise ValueError("stale_after must not be shorter than ttl")
        self._consent_repo = consent_repo
        self._config_store = config_store
        self._clock = clock
        self._ttl = ttl
        self._retry_backoff = retry_backoff
        self._stale_after = stale_after
        self._entries: dict[tuple[int, int], _Entry] = {}
        self._refreshing: set[tuple[int, int]] = set()
        self._retry_after: dict[tuple[int, int], datetime] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def verdict(
        self, guild_id: int, discord_user_id: int, *, has_consent_role: bool
    ) -> bool | None:
        """Whether this user's packets may be recorded, or `None` if not known yet.

        Never awaits. Schedules a refresh when the cached entry is missing
        or stale; see the module docstring for why `None` is a drop rather
        than a wait, and why an entry older than `stale_after` stops being
        served.
        """
        key = (guild_id, discord_user_id)
        now = self._clock.now()
        entry = self._entries.get(key)
        if entry is None or now - entry.fetched_at >= self._ttl:
            self._schedule_refresh(key, now)
        if entry is None or now - entry.fetched_at >= self._stale_after:
            return None
        return may_record(entry.record, entry.policy_version, has_consent_role)

    @property
    def refresh_pending(self) -> bool:
        """Whether a background refresh is currently in flight."""
        return bool(self._refreshing)

    async def refresh(self, guild_id: int, discord_user_id: int) -> None:
        """Reads this pair's consent record and the guild's policy version.

        Awaits the database, so it is called from a task of its own,
        never from the audio drain.
        """
        key = (guild_id, discord_user_id)
        try:
            record = await self._consent_repo.current(discord_user_id, guild_id)
            policy_version = await self._config_store.get(guild_id, settings.POLICY_VERSION)
        except Exception:
            # Not fatal and not silent: the verdict simply stays whatever
            # it already was (or unknown, in which case nothing is
            # recorded for this speaker), and the backoff keeps the next
            # frame from asking again immediately.
            self._retry_after[key] = self._clock.now() + self._retry_backoff
            log.warning(
                "Could not refresh the consent record for guild=%s user=%s; "
                "retrying no sooner than %s from now",
                guild_id,
                discord_user_id,
                self._retry_backoff,
                exc_info=True,
            )
            return
        self._entries[key] = _Entry(
            record=record, policy_version=policy_version or "", fetched_at=self._clock.now()
        )
        self._retry_after.pop(key, None)

    def cancel_refreshes(self) -> None:
        """Cancels anything still in flight. Called when the adapter leaves."""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._refreshing.clear()

    def _schedule_refresh(self, key: tuple[int, int], now: datetime) -> None:
        if key in self._refreshing:
            return
        retry_after = self._retry_after.get(key)
        if retry_after is not None and now < retry_after:
            return
        self._refreshing.add(key)
        task = asyncio.create_task(self._run_refresh(key))
        # A task nobody holds a reference to can be garbage collected
        # mid-flight, which would silently lose the refresh.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_refresh(self, key: tuple[int, int]) -> None:
        guild_id, discord_user_id = key
        try:
            await self.refresh(guild_id, discord_user_id)
        finally:
            self._refreshing.discard(key)
