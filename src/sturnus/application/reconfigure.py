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
* **Identity** -- `voice_channel_ids`, `consent_role_id`. These decide
  which channels a session may be opened in, which channel a session's row
  names, and which role the headcount and the per-packet filter agree on.
  They cannot move under a running
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
IDENTITY_KEYS: tuple[str, ...] = (settings.VOICE_CHANNEL_IDS, settings.CONSENT_ROLE_ID)

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

    #: Every channel this guild allows Sturnus to record in, sorted and
    #: never empty (an empty list is spelled "no configuration at all", so
    #: `_desired_config` returns `None` instead of a config holding one).
    #:
    #: A *list*, and still one connection: a Discord bot holds one voice
    #: connection per guild, so this says where Sturnus may record, not
    #: how many rooms it records at once. Which of them is being served is
    #: decided per pass by `sturnus.application.channel_choice`, from who
    #: is actually sitting in them -- it is a fact about the moment, not
    #: about the configuration, so it is deliberately not stored here.
    channel_ids: tuple[int, ...]
    role_id: int
    timeouts: SessionTimeouts
    retention_days: int

    @property
    def identity(self) -> tuple[tuple[int, ...], int]:
        return (self.channel_ids, self.role_id)

    def identity_changes_from(self, other: GuildRuntimeConfig) -> tuple[str, ...]:
        """Names the identity keys whose value differs from `other`'s.

        The whole tuple is compared, so adding a second allowed channel is
        an identity change with exactly the same defer-while-recording
        semantics a channel *move* has always had. It has to be: the list
        decides which voice-state updates the client is interested in and
        which channel a new session may open against, and both of those
        are read on the edge a session opens on.

        Order is not a change, because `settings.parse_channel_ids` sorts
        -- re-typing the same list differently must not retarget a guild.
        """
        changed: list[str] = []
        if self.channel_ids != other.channel_ids:
            changed.append(settings.VOICE_CHANNEL_IDS)
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
    #: The rooms whose sessions are the reason `deferred_keys` has to
    #: wait, in the order they were given. Empty whenever nothing is
    #: deferred.
    #:
    #: A *list* because the runtime is keyed per room now, and because the
    #: answer this field gives is the one `_apply_pending` needs: the
    #: change may land when **every** room named here is idle, not when
    #: the first of them finishes. The configuration it carries is the
    #: guild's -- one row set, read by every room -- so landing it while a
    #: second room is still recording would move that room's consent role
    #: or its allowed list out from under a session in progress, which is
    #: the exact thing the deferral exists to prevent.
    #:
    #: With `MAX_CONCURRENT_SESSIONS_PER_GUILD` at one it always holds
    #: either nothing or one room, and saying so in the log line is
    #: already worth it: "waiting for the recording in #standup" beats
    #: "waiting for the recording".
    deferred_for_channel_ids: tuple[int, ...] = ()


def plan_reconfigure(
    *,
    current: GuildRuntimeConfig | None,
    desired: GuildRuntimeConfig | None,
    recording_channel_ids: tuple[int, ...],
) -> ReconfigurePlan:
    """Compares what the process is doing against what the database says.

    `current` is `None` when the process holds no pipeline for the guild,
    `desired` is `None` when the guild has no usable configuration (no
    recording channel is named, or `consent_role_id` is unset).

    `recording_channel_ids` names the guild's rooms that have a session in
    progress -- empty when it is idle. It replaced a bare `is_recording`
    flag when the runtime became keyed per room, and the difference is not
    cosmetic: the caller now has to *collect* it across the guild's rooms,
    so a second room recording cannot be overlooked, and the plan can name
    the rooms a deferral is waiting on rather than gesturing at "the
    guild".

    What did **not** change is which changes defer. Every identity key is
    the guild's, not a room's: `consent_role_id` decides whose voice is
    recorded in every room at once, and `voice_channel_ids` decides which
    rooms exist to record in at all -- including whether the room a
    session is already open in is still one of them. So any of them
    changing while any room records still waits for that room, and a list
    change mid-session is still applied at session end and not before.
    """
    if desired is None:
        if current is None:
            return ReconfigurePlan(ReconfigureAction.NOTHING, False, (), ())
        # Nothing to retune with: the configuration is gone, not different.
        if recording_channel_ids:
            return ReconfigurePlan(
                ReconfigureAction.DEFER_TEARDOWN, False, (), IDENTITY_KEYS, recording_channel_ids
            )
        return ReconfigurePlan(ReconfigureAction.TEARDOWN, False, IDENTITY_KEYS, ())

    if current is None:
        # A fresh pipeline is constructed with the desired values, so every
        # key is in force the moment it exists -- there is nothing to
        # defer and nothing to retune afterwards.
        #
        # Deliberately not conditioned on `recording_channel_ids`: a room
        # this process holds no pipeline for cannot be recording, so a
        # session elsewhere in the guild is no reason to refuse to build.
        # What *does* refuse is the connection limit, and the caller asks
        # that (`SturnusClient._may_open_another`) rather than this
        # function pretending a configuration question has been answered.
        return ReconfigurePlan(ReconfigureAction.BUILD, False, IDENTITY_KEYS + TUNABLE_KEYS, ())

    identity_changes = desired.identity_changes_from(current)
    tunable_changes = desired.tunable_changes_from(current)

    if not identity_changes:
        if not tunable_changes:
            return ReconfigurePlan(ReconfigureAction.NOTHING, False, (), ())
        return ReconfigurePlan(ReconfigureAction.NOTHING, True, tunable_changes, ())

    if recording_channel_ids:
        # The identity waits for the sessions to end; the tunables do not.
        # Splitting them is the whole point: a shortened idle timeout must
        # not have to wait four hours behind a channel move.
        return ReconfigurePlan(
            ReconfigureAction.DEFER_RETARGET,
            True,
            tunable_changes,
            identity_changes,
            recording_channel_ids,
        )
    return ReconfigurePlan(ReconfigureAction.RETARGET, True, identity_changes + tunable_changes, ())


@dataclass(frozen=True)
class ReconfigureResult:
    """What a reconcile pass actually did, for the command that triggered it.

    Exists so `/config set` can say what took effect instead of confirming
    a write and implying the rest. A reply that reads `voice_channel_ids
    set to 123` when the process is still recording the old channel is the same
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
    #: The allowed channels currently being served -- the rooms a session
    #: is open in, or the ones the next sessions would open against. Empty
    #: when the guild has no pipeline at all.
    #:
    #: A collection rather than the single id it was, because a session is
    #: a property of a room now rather than of a server. It holds at most
    #: `channel_choice.MAX_CONCURRENT_SESSIONS_PER_GUILD` entries, which is
    #: one -- and `session_limit` below is what lets `/config show` say
    #: *why* it is one instead of leaving the reader to infer that the
    #: other rooms are idle by choice.
    channel_ids: tuple[int, ...]
    #: How many of this guild's rooms may be recorded at the same moment.
    #: Carried on the record rather than read from the constant by every
    #: reader, so the sentence `/config show` prints and the number the
    #: runtime enforced are the same number.
    session_limit: int
    #: Every channel the guild allows, the served ones included.
    #: `/config show` names the others so a person waiting in one of them
    #: is not left to guess why nothing is happening.
    allowed_channel_ids: tuple[int, ...]
    #: Allowed channels that hold consenting members and are not being
    #: served, as of the last headcount. In-memory bookkeeping like the
    #: rest of this record -- reading it costs no I/O.
    waiting_channel_ids: tuple[int, ...]
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
