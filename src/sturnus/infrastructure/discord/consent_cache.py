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
from sturnus.domain.consent import ConsentRecord, may_record, may_record_video
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

    `may_record` and `may_record_video` are the only entry points: each
    refreshes the cached record/policy pair when missing or stale, then
    combines it with the caller's role check exactly as the matching
    function in `sturnus.domain.consent` does. There is no explicit
    invalidation -- entries simply expire after `ttl`, which is enough
    because the record changes only through slow, human-driven paths
    (`/consent grant`, `/consent revoke`, an administrator editing roles,
    `/config set policy_version`), never per-packet.

    **What is cached is the record, not a verdict, and that is what makes
    a scheduled revocation work with no new machinery.** `revoked_at` is
    an effective instant: a withdrawal dated for the end of the month
    sits in the row for weeks while consent stays in force. Because the
    domain rule is applied against `self._clock.now()` on every call and
    only the *row* is held here, the instant passing turns the verdict
    over on the next packet -- not on the next refresh. The five second
    TTL therefore bounds how stale the row may be, never how late the
    schedule may fire. A cache that stored the boolean would have got
    this exactly wrong: the withdrawal would arrive up to five seconds
    late, silently, and nothing in the log would say why.
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
        entry = await self._entry(guild_id, discord_user_id, now)
        return may_record(entry.record, entry.policy_version, has_consent_role, now)

    async def may_record_video(
        self, guild_id: int, discord_user_id: int, has_consent_role: bool
    ) -> bool:
        """True when this person's consent names video as well as audio.

        Nothing records video. What this gates is whether the bot asks
        Discord for the stream at all
        (`sturnus.infrastructure.discord.voice`), which is the part that
        is visible to the person on the other end and therefore the part
        that has to be right first.

        It shares the entry `may_record` fills, so a session in which
        somebody speaks and shares costs one database read every five
        seconds rather than two.
        """
        now = self._clock.now()
        entry = await self._entry(guild_id, discord_user_id, now)
        return may_record_video(entry.record, entry.policy_version, has_consent_role, now)

    async def _entry(self, guild_id: int, discord_user_id: int, now: datetime) -> _Entry:
        """The cached row for this pair, refreshed when missing or stale."""
        key = (guild_id, discord_user_id)
        entry = self._entries.get(key)
        if entry is None or now - entry.fetched_at >= self._ttl:
            record = await self._consent_repo.current(discord_user_id, guild_id)
            policy_version = await self._config_store.get(guild_id, settings.POLICY_VERSION)
            entry = _Entry(record=record, policy_version=policy_version or "", fetched_at=now)
            self._entries[key] = entry
        return entry
