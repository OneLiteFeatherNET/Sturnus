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

TTL: 5 seconds. The role is still checked directly, per packet, by the
caller before this cache is even consulted (`VoiceReceiveAdapter`), so an
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

from dataclasses import dataclass
from datetime import datetime, timedelta

from sturnus.application.ports import Clock
from sturnus.domain import settings
from sturnus.domain.consent import ConsentRecord, may_record
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.repositories import ConsentRepository

#: See the module docstring for why this value.
DEFAULT_TTL = timedelta(seconds=5)


@dataclass
class _Entry:
    record: ConsentRecord | None
    policy_version: str
    fetched_at: datetime


class ConsentCache:
    """Per-`(guild_id, discord_user_id)` cache of the stored consent record.

    `may_record` is the only entry point: it refreshes the cached
    record/policy pair when missing or stale, then combines it with the
    caller's role check exactly as `sturnus.domain.consent.may_record`
    does. There is no explicit invalidation -- entries simply expire after
    `ttl`, which is enough because the record changes only through slow,
    human-driven paths (`/consent grant`, `/consent revoke`, an
    administrator editing roles, `/config set policy_version`), never
    per-packet.
    """

    def __init__(
        self,
        consent_repo: ConsentRepository,
        config_store: ConfigStore,
        clock: Clock,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        self._consent_repo = consent_repo
        self._config_store = config_store
        self._clock = clock
        self._ttl = ttl
        self._entries: dict[tuple[int, int], _Entry] = {}

    async def may_record(self, guild_id: int, discord_user_id: int, has_consent_role: bool) -> bool:
        """True when a packet from this user may be recorded (Spec 3.1's second layer)."""
        now = self._clock.now()
        key = (guild_id, discord_user_id)
        entry = self._entries.get(key)
        if entry is None or now - entry.fetched_at >= self._ttl:
            record = await self._consent_repo.current(discord_user_id, guild_id)
            policy_version = await self._config_store.get(guild_id, settings.POLICY_VERSION)
            entry = _Entry(record=record, policy_version=policy_version or "", fetched_at=now)
            self._entries[key] = entry
        return may_record(entry.record, entry.policy_version, has_consent_role)
