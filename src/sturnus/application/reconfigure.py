"""The decision behind reloading a guild's configuration without a restart.

`/config set` writes to the database; something has to notice. Before this
module the answer was "the next process start" -- `_configure_guild` ran
once, at `on_ready`, and a guild configured after that stayed unwatched
until the pod was restarted, while `/config show` cheerfully reported the
value as set.

The hard part is not noticing the change, it is applying it to a guild
that is *recording right now*. So the keys are split into two classes and
treated differently:

* **Tunables** -- `empty_grace_seconds`, `idle_timeout_minutes`,
  `max_session_hours`, `audio_retention_days`. Each is read at exactly one
  point, on the next tick or in `close()`, and captured nowhere. They are
  applied in place, immediately, mid-session included (see
  `RecordingService.apply_tunables`).
* **Identity** -- `voice_channel_id`, `consent_role_id`. These decide
  which channel a session's row names and which role the headcount and
  the per-packet filter agree on. They cannot move under a running
  session without either lying about where the audio came from or
  dropping a live voice connection with unflushed writers behind it, so
  when a session is in progress they are *deferred* to the moment it
  ends -- after `close()` has encrypted, uploaded and enqueued
  everything. A recording is never discarded to make a configuration
  change land sooner.

Only the comparison lives here, never the execution: no Discord object,
no database handle, no `RecordingService`. That is what lets every row of
the table below be tested without a gateway
(`tests/application/test_reconfigure.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sturnus.domain import settings
from sturnus.domain.session import SessionTimeouts

#: The keys that decide *which* channel and *whose* voice -- the ones that
#: cannot move under a running session.
IDENTITY_KEYS: tuple[str, ...] = (settings.VOICE_CHANNEL_ID, settings.CONSENT_ROLE_ID)

#: The keys that only ever influence the *next* decision, so they can be
#: swapped in place at any moment.
TUNABLE_KEYS: tuple[str, ...] = (
    settings.EMPTY_GRACE_SECONDS,
    settings.IDLE_TIMEOUT_MINUTES,
    settings.MAX_SESSION_HOURS,
    settings.AUDIO_RETENTION_DAYS,
)

#: The keys a reconcile can never help with, because the process reads them
#: exactly once at start. Today that is only `publish_poll_seconds`: the
#: publish sweep runs on one process-wide interval taken from the setting's
#: default rather than per-guild scheduling -- a deliberate simplification,
#: see the comment above `_PUBLISH_POLL_SECONDS` in `sturnus.entrypoints.bot`.
#:
#: It lives here rather than beside the `/config` replies that first needed
#: it because it is a fact about how the bot *reads* a key, exactly like the
#: two tuples above -- and because it now has a second reader. The console's
#: API process cannot reconcile at all (no gateway, Spec 13.2), so it has to
#: tell an operator which of these three classes their write falls into, and
#: a second hand-maintained list of restart-only keys would be a list that
#: disagrees with this one the day a fourth class appears.
RESTART_REQUIRED_KEYS: frozenset[str] = frozenset({settings.PUBLISH_POLL_SECONDS})


@dataclass(frozen=True)
class GuildRuntimeConfig:
    """Everything about a guild that the bot process holds in memory.

    Deliberately *not* every configuration key: `admin_role_id`,
    `policy_url`, `policy_version`, `document_target`, `merge_gap_seconds`,
    `document_provider`, `transcription_language` and
    `transcription_prompt` are read per use (by a permission check, by the
    consent cache, or by the worker process entirely) and were never stale
    to begin with. Only what the bot caches needs reconciling.
    """

    channel_id: int
    role_id: int
    timeouts: SessionTimeouts
    retention_days: int

    @property
    def identity(self) -> tuple[int, int]:
        return (self.channel_id, self.role_id)

    def identity_changes_from(self, other: GuildRuntimeConfig) -> tuple[str, ...]:
        """Names the identity keys whose value differs from `other`'s."""
        changed: list[str] = []
        if self.channel_id != other.channel_id:
            changed.append(settings.VOICE_CHANNEL_ID)
        if self.role_id != other.role_id:
            changed.append(settings.CONSENT_ROLE_ID)
        return tuple(changed)

    def tunable_changes_from(self, other: GuildRuntimeConfig) -> tuple[str, ...]:
        """Names the tunable keys whose value differs from `other`'s."""
        changed: list[str] = []
        if self.timeouts.empty_grace_seconds != other.timeouts.empty_grace_seconds:
            changed.append(settings.EMPTY_GRACE_SECONDS)
        if self.timeouts.idle_timeout_minutes != other.timeouts.idle_timeout_minutes:
            changed.append(settings.IDLE_TIMEOUT_MINUTES)
        if self.timeouts.max_session_hours != other.timeouts.max_session_hours:
            changed.append(settings.MAX_SESSION_HOURS)
        if self.retention_days != other.retention_days:
            changed.append(settings.AUDIO_RETENTION_DAYS)
        return tuple(changed)


class ReconfigureAction(StrEnum):
    #: Nothing to do -- the overwhelmingly common outcome, once every ten
    #: seconds per guild, and the one that must stay silent.
    NOTHING = "nothing"
    #: No pipeline exists and the required keys are now present: build one.
    #: This is the path the reported defect needed and never had.
    BUILD = "build"
    #: Identity changed and the guild is idle: point it at the new channel
    #: and role now.
    RETARGET = "retarget"
    #: Identity changed while a session is recording: hold it until that
    #: session has finished closing.
    DEFER_RETARGET = "defer_retarget"
    #: A required key was cleared and the guild is idle: stop watching.
    TEARDOWN = "teardown"
    #: A required key was cleared while a session is recording: let the
    #: recording finish and upload, then stop watching.
    DEFER_TEARDOWN = "defer_teardown"


@dataclass(frozen=True)
class ReconfigurePlan:
    """What to do, and which keys land now versus later."""

    action: ReconfigureAction
    #: Apply the tunables in place, right now, whatever else happens.
    retune: bool
    applied_keys: tuple[str, ...]
    deferred_keys: tuple[str, ...]


def plan_reconfigure(
    *,
    current: GuildRuntimeConfig | None,
    desired: GuildRuntimeConfig | None,
    is_recording: bool,
) -> ReconfigurePlan:
    """Compares what the process is doing against what the database says.

    `current` is `None` when the process holds no pipeline for the guild,
    `desired` is `None` when the guild has no usable configuration (either
    `voice_channel_id` or `consent_role_id` is unset). `is_recording` is
    the one fact that turns an immediate change into a deferred one.
    """
    if desired is None:
        if current is None:
            return ReconfigurePlan(ReconfigureAction.NOTHING, False, (), ())
        # Nothing to retune with: the configuration is gone, not different.
        if is_recording:
            return ReconfigurePlan(ReconfigureAction.DEFER_TEARDOWN, False, (), IDENTITY_KEYS)
        return ReconfigurePlan(ReconfigureAction.TEARDOWN, False, IDENTITY_KEYS, ())

    if current is None:
        # A fresh pipeline is constructed with the desired values, so every
        # key is in force the moment it exists -- there is nothing to
        # defer and nothing to retune afterwards.
        return ReconfigurePlan(ReconfigureAction.BUILD, False, IDENTITY_KEYS + TUNABLE_KEYS, ())

    identity_changes = desired.identity_changes_from(current)
    tunable_changes = desired.tunable_changes_from(current)

    if not identity_changes:
        if not tunable_changes:
            return ReconfigurePlan(ReconfigureAction.NOTHING, False, (), ())
        return ReconfigurePlan(ReconfigureAction.NOTHING, True, tunable_changes, ())

    if is_recording:
        # The identity waits for the session to end; the tunables do not.
        # Splitting them is the whole point: a shortened idle timeout must
        # not have to wait four hours behind a channel move.
        return ReconfigurePlan(
            ReconfigureAction.DEFER_RETARGET, True, tunable_changes, identity_changes
        )
    return ReconfigurePlan(ReconfigureAction.RETARGET, True, identity_changes + tunable_changes, ())


@dataclass(frozen=True)
class ReconfigureResult:
    """What a reconcile pass actually did, for the command that triggered it.

    Exists so `/config set` can say what took effect instead of confirming
    a write and implying the rest. A reply that reads `voice_channel_id set
    to 123` when the process is still recording the old channel is the same
    defect this whole module fixes, one layer up.
    """

    action: ReconfigureAction
    applied_keys: tuple[str, ...]
    deferred_keys: tuple[str, ...]
    #: The guild has a live pipeline after this pass.
    is_live: bool
    #: A session was in progress during this pass.
    is_recording: bool
    #: This pass is what made the guild live (BUILD).
    became_live: bool
    #: The session in progress already exceeds the timeouts now in force
    #: and will close on the next tick -- normally, uploading everything.
    session_exceeds_timeouts: bool


@dataclass(frozen=True)
class RunningState:
    """What the process is actually doing for a guild, for `/config show`."""

    is_live: bool
    is_recording: bool
    channel_id: int | None
    #: Identity keys stored but not yet in force, waiting for the session.
    pending_keys: tuple[str, ...]
    pending_teardown: bool


class Reconfigure(Protocol):
    """The reconcile entry point, as the cogs see it.

    A bound method of the Discord client, passed in rather than reached
    for: a cog importing the client would close an import cycle, and a cog
    holding the client would be able to do considerably more than ask it
    to re-read configuration.
    """

    async def __call__(self, guild_id: int, *, force: bool = False) -> ReconfigureResult: ...


class RunningStateReader(Protocol):
    """Reads the process's live state for one guild. Synchronous by design:
    it inspects in-memory bookkeeping only and must never wait on I/O."""

    def __call__(self, guild_id: int) -> RunningState: ...


__all__ = [
    "IDENTITY_KEYS",
    "TUNABLE_KEYS",
    "GuildRuntimeConfig",
    "Reconfigure",
    "ReconfigureAction",
    "ReconfigurePlan",
    "ReconfigureResult",
    "RunningState",
    "RunningStateReader",
    "plan_reconfigure",
]
