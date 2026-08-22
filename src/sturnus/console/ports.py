"""What the console's API needs from the world, as narrow protocols.

Each of these is satisfied by an adapter wired in by
`sturnus.entrypoints.api`, and by a fake in the tests. They are declared
here rather than imported from the concrete classes so this package
depends on shapes rather than on `sturnus.infrastructure` -- the same rule
`sturnus.application` follows, for the same reason: a console module that
imports an adapter is a console module that cannot be tested without one.

They are also narrow on purpose. `LinkDirectory` exposes one method, not
the whole of `AccountLinkRepository`, because one method is what the login
flow uses -- and a protocol that offers more than its consumer needs is an
invitation for the next handler to reach for something it should not have.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import Protocol

from sturnus.console.reporting import RecordedSession
from sturnus.console.statistics import AttendedSession
from sturnus.infrastructure.documents.outline_oauth import ExternalIdentity


class OAuthClient(Protocol):
    """The identity provider the console authenticates against."""

    def authorize_url(self, state: str) -> str: ...

    async def identity_from_code(self, code: str) -> ExternalIdentity: ...


class StateStore(Protocol):
    """Single-use OAuth states, tying a callback to a login this server began."""

    async def issue(self, state: str, now: datetime) -> None: ...

    #: `False` for a state that was never issued, has already been used, or
    #: has expired -- the caller treats all three identically, because from
    #: the outside they are the same event: this is not a callback for a
    #: login we started.
    async def consume(self, state: str, now: datetime) -> bool: ...


class LinkDirectory(Protocol):
    """The bridge from an external identity to the Discord user it belongs to.

    This is the whole authorisation model: every console query is scoped by
    Discord id, because that is what `session_participant` names, and the
    only bridge to one is a link the person made themselves with `/link`.
    """

    async def discord_user_for(self, provider: str, external_user_id: str) -> int | None: ...


class AdminDirectory(Protocol):
    """Who administers what, as far as the console is allowed to know.

    Three questions rather than one, because the console asks three
    genuinely different ones and answering the narrow one with the wide
    one is the failure this protocol exists to make hard to write:

    * `is_admin_anywhere` decides whether the settings section is offered
      at all. It is a rendering hint and never a control.
    * `administered_guilds` is what the guild picker lists.
    * `is_admin` is the only one that authorises anything. Every settings
      read and every settings write goes through it, per guild, because
      an administrator of one guild is nobody in another.

    Read from `admin_member`, which `bot` mirrors on its sweep. The API
    process has no gateway to ask Discord directly, deliberately: a
    process that can decrypt every recording ever made is not one to also
    hand the ability to act as the bot (Spec 13.2).
    """

    async def is_admin_anywhere(self, discord_user_id: int) -> bool: ...

    async def administered_guilds(self, discord_user_id: int) -> Sequence[int]: ...

    async def is_admin(self, guild_id: int, discord_user_id: int) -> bool: ...


class SessionReads(Protocol):
    """Everything the console reads, already narrowed to one Discord user.

    Every method takes `discord_user_id` first, and that is the whole
    point of the shape: there is no method here that can be called
    without naming whose data is being asked for, so a handler cannot
    accidentally ask a wider question than it is entitled to. The
    narrowing is done by the statement itself in
    `sturnus.console.queries` -- not by a filter afterwards, which is a
    filter somebody can forget.
    """

    async def sessions_for(self, discord_user_id: int) -> Sequence[AttendedSession]: ...

    #: `None` for a session that does not exist *and* for one this person
    #: was not in. The handler answers 404 to both, deliberately.
    async def session_for(
        self, discord_user_id: int, session_id: int
    ) -> AttendedSession | None: ...

    async def sessions_in_year(
        self, discord_user_id: int, year: int
    ) -> Sequence[AttendedSession]: ...

    async def sessions_on_day(
        self, discord_user_id: int, day: date
    ) -> Sequence[AttendedSession]: ...

    #: This person's own transcripts, encoded as the column stores them.
    #: Their own and never the session's: the dashboard's word count says
    #: how much *they* said, and the transcript is the protected content.
    async def transcripts_of(self, discord_user_id: int) -> Sequence[str]: ...


@dataclass(frozen=True)
class Track:
    """Where one speaker's recording is, and what unlocks it.

    Deliberately not the whole `transcription_job` row. What the audio
    endpoint needs is the object key and the wrapped key; the transcript,
    the status and the attempt count are somebody else's business, and a
    value that carried them would put a transcript one attribute access
    away from a response body.
    """

    s3_key: str
    encryption_key_id: str
    wrapped_data_key: bytes


class TrackDirectory(Protocol):
    """One speaker's recording, if the person asking is allowed to hear it.

    `requested_by` is not an afterthought and not optional: the whole
    authorisation rule for audio lives inside this one call. An
    implementation answers `None` both for "there is no such track" and for
    "you were not in that session", because from outside they must look the
    same -- the existence of a session somebody was not in is not
    information they are owed.

    Consequently there is no `track` method without a `requested_by`, and
    no way to filter afterwards in a handler. A filter that can be
    forgotten is a filter that will be.
    """

    async def track_for(
        self, session_id: int, speaker_id: int, *, requested_by: int
    ) -> Track | None: ...


class KeyUnwrapper(Protocol):
    """Unwraps a recording's data key with the process's master key.

    `sturnus.infrastructure.crypto.KeyWrapper` satisfies this. `key_id`
    is here because a recording names the master key that wrapped it, and
    a mismatch is a configuration error worth reporting as one rather than
    an authentication-tag failure three layers down.
    """

    key_id: str

    def unwrap(self, wrapped: bytes) -> bytes: ...


class EncryptedAudioSource(Protocol):
    """The object store, read by byte range.

    Three methods because the audio endpoint makes three different kinds of
    request and no more: how big the object is (to declare the track's
    length), the fixed-size file header (to get the nonce prefix), and the
    body from a chosen chunk boundary onwards.

    `stream` is an async *generator* rather than a plain iterator so the
    handler can close it: a listener who stops playing halfway through
    should stop the transfer from S3 in the same breath, and a suspended
    generator nobody closed holds the connection until the loop gets round
    to finalising it.

    A key that is not in the store raises `KeyError` -- the ordinary case
    is a recording the retention sweep already erased while its row lives
    on, which is a 404 and not an error.
    """

    async def size(self, key: str) -> int: ...

    async def read(self, key: str, start: int, length: int) -> bytes: ...

    def stream(self, key: str, start: int) -> AsyncGenerator[bytes, None]: ...


class SettingsStore(Protocol):
    """Per-guild runtime configuration, read whole and written one key at a time.

    Narrow to two methods on purpose. In particular there is no
    `get`/`get_stored` here: the listing endpoint reads a guild's whole
    configuration in one query rather than one per key, and a protocol
    that offered the per-key read would be an invitation for the next
    handler to loop over `KNOWN_KEYS` doing seventeen round trips.

    **`set` is where value validation lives, and it must stay there.** It
    refuses an unknown key and refuses a non-positive-integer for an
    integer key, and the API's job is to turn that `ValueError` into a
    400 -- never to check the same thing first. Two copies of a
    validation rule is how the two drift.
    """

    async def snapshot(self, guild_id: int) -> dict[str, str]: ...

    async def set(self, guild_id: int, key: str, value: str | None, now: datetime) -> None: ...


@dataclass(frozen=True)
class QueueSpeaker:
    """One speaker's transcription job, as the console reports it."""

    discord_user_id: int
    display_name: str | None
    status: str
    attempts: int
    #: `str(exc)` from the last failed attempt, already shortened. `None`
    #: while nothing has failed.
    error: str | None


@dataclass(frozen=True)
class QueueSnapshot:
    """Where a session's transcription has got to, right now.

    This is the progress view. It exists because a re-queue is not
    instantaneous and a button that reports nothing after being pressed is
    a button people press twice: what an administrator needs after asking
    for a redo is to watch `pending` become `running` become `done`.

    `session_status` is the row's own status, which moves in step: a
    re-queue resets it to `closed`, and it becomes `documented` again only
    once every job has finished and the document has been written. So
    "the redo is complete" is a fact about this value, not a guess from
    counting jobs.
    """

    session_status: str
    document_url: str | None
    speakers: tuple[QueueSpeaker, ...]
    #: Whether a re-queue would be accepted right now, and if not, why.
    #: Derived from the same `plan_requeue` the write itself re-derives
    #: under a lock, so the button is disabled for the same reasons the
    #: write would refuse -- rather than for a second, drifting set.
    can_requeue: bool
    refusal: str | None


@dataclass(frozen=True)
class RequeueOutcome:
    """What a re-queue did, or why it did nothing."""

    accepted: bool
    #: Speakers whose job was reset to `pending`.
    requeued_user_ids: tuple[int, ...]
    #: Speakers skipped because their audio is erased. Their old
    #: transcript is carried into the new document unchanged, and saying
    #: so is not optional: an administrator told "3 speakers re-queued"
    #: and not told this would reasonably assume the whole document had
    #: been regenerated.
    erased_user_ids: tuple[int, ...]
    refusal: str | None


class QueueControl(Protocol):
    """A session's transcription queue, if the person asking administers it.

    `requested_by` is not optional and there is no method here without
    it, for exactly the reason `TrackDirectory` has none: the authorisation
    rule lives inside the call rather than in a handler that could forget
    to apply it.

    Both methods answer `None` for "no such session" *and* for "you do not
    administer that guild", because from outside those must look the same.
    Unlike audio, the rule here is administrator-of-the-guild rather than
    participant-of-the-session: re-running a transcription is an operation
    on the system, not a use of one's own recording.
    """

    async def status_for(self, session_id: int, *, requested_by: int) -> QueueSnapshot | None: ...

    async def requeue(self, session_id: int, *, requested_by: int) -> RequeueOutcome | None: ...


@dataclass(frozen=True)
class ConsentHolder:
    """One person's standing consent in one guild, as an administrator sees it.

    The newest `consent` row for that person in that guild, which is the
    same selection `ConsentRepository.current` makes and the same one the
    recorder acts on -- showing an administrator an older row would show
    them a decision nothing enforces.

    `active` is not `revoked_at is None`. Consent also expires when the
    guild's `policy_version` moves on, because a grant names the version
    it was given under (`sturnus.domain.consent.is_consent_active`). Both
    states are reported separately rather than folded into one flag: "they
    withdrew it" and "we changed the policy under them" are different
    facts about a person and lead to different conversations.

    `recordings_with_audio` is here for one purpose, and it is not a link
    to a delete button. Withdrawing consent stops future recording; it
    does not erase what is already stored. An administrator who is not
    shown that number would reasonably assume it does.
    """

    discord_user_id: int
    #: From `session_participant`, which is the only place a name is
    #: stored -- `consent` has none. `None` for somebody who consented and
    #: has not yet been in a recorded session, which is exactly the state
    #: a well-run guild onboards people into.
    display_name: str | None
    policy_version: str
    granted_at: datetime
    revoked_at: datetime | None
    active: bool
    recordings_with_audio: int


@dataclass(frozen=True)
class RevocationOutcome:
    """What a revocation did, or why it did nothing."""

    revoked: bool
    #: Why nothing happened, as one of a fixed set of reasons. `None` when
    #: something did.
    refusal: str | None


class ConsentDirectory(Protocol):
    """Who has consented in a guild, and the power to withdraw it for them.

    `requested_by` is not optional and there is no method here without it,
    for the reason `TrackDirectory` and `QueueControl` have none: the
    authorisation rule lives inside the call rather than in a handler that
    could forget to apply it. Both methods answer `None` for "no such
    guild" and for "you do not administer it" alike.

    **What a revocation from here can and cannot do.** Consent is two
    layers (Spec 3.1): a Discord role, checked synchronously on every
    frame, and a stored record, checked on every frame through a five
    second cache. This process holds no Discord token and never will
    (Spec 13.2) -- it can decrypt every recording ever made, and a process
    with that reach is not one to also give the ability to act as the bot.
    So it writes the record and cannot touch the role.

    That is enough to stop the recording: the stored record is the layer
    that exists precisely because the role can be bypassed, and a
    revocation takes effect within the cache's five seconds, mid-session.
    What it leaves behind is a role the person still holds, which is
    visible in Discord and misleading if nobody says so. The console says
    so, in the interface, next to the button.
    """

    async def holders(
        self, guild_id: int, *, requested_by: int
    ) -> Sequence[ConsentHolder] | None: ...

    async def revoke(
        self, guild_id: int, discord_user_id: int, *, requested_by: int
    ) -> RevocationOutcome | None: ...


@dataclass(frozen=True)
class QueuedSession:
    """One session the transcription pipeline has not finished with."""

    id: int
    channel_id: int
    channel_name: str | None
    started_at: datetime
    ended_at: datetime | None
    status: str
    document_url: str | None
    pending: int
    running: int
    done: int
    dead: int


@dataclass(frozen=True)
class GuildQueue:
    """Where a guild's transcription work stands, right now.

    The guild-wide counts and the unfinished sessions in one value, read
    together, because a page that showed "3 pending" beside a list read a
    moment later would occasionally show three pending jobs and no session
    they could belong to.

    `running_past_lease` is the number worth reading first: a `running`
    job whose lease expired is one whose worker died holding it, which no
    amount of waiting fixes.

    `lease_seconds` travels with it because that count is derived from an
    assumed lease, and the lease that actually applies is the *worker's*
    `job_lease_seconds`. The console says which number it used rather than
    presenting a derived count as a fact -- the same caveat `/queue
    status` prints, for the same reason.
    """

    pending: int
    running: int
    done: int
    dead: int
    running_past_lease: int
    #: When the session owning the oldest `pending` job ended.
    #: `transcription_job` has no enqueue timestamp at all, and a session's
    #: end is within seconds of when its jobs were created -- close enough
    #: to answer "has something been sitting here for hours?". It is *not*
    #: the age of a re-queued job, which keeps its session's original end,
    #: and the console says so rather than calling it a job age.
    oldest_pending_session_ended_at: datetime | None
    #: Sessions that are closed, have no unfinished jobs, and still have no
    #: document. Nothing is queued for them and nothing will happen on its
    #: own.
    closed_undocumented: int
    lease_seconds: float
    sessions: tuple[QueuedSession, ...]
    #: Whether the list above was cut short. Sent so that a page showing
    #: twenty sessions never reads as "there are twenty".
    truncated: bool


class QueueOverview(Protocol):
    """A guild's transcription queue, if the person asking administers it.

    The guild-wide companion to `QueueControl`, which answers about one
    session. Same rule, same shape: `requested_by` is not optional, there
    is no method here without it, and `None` covers "no such guild" and
    "you do not administer it" alike -- because from outside those must
    look the same.
    """

    async def for_guild(self, guild_id: int, *, requested_by: int) -> GuildQueue | None: ...


@dataclass(frozen=True)
class GuildRecording:
    """A guild's recorded sessions, and how many distinct people were in them.

    The two are read together and returned as one value because the second
    cannot be derived from the first: `sessions` carries per-session
    participant *counts*, never identities, so "how many different people
    has this guild recorded" has to be counted in the statement. Keeping
    the identities out of the value is the point -- see
    `sturnus.console.reporting` on why this feature stops at aggregates.
    """

    sessions: tuple[RecordedSession, ...]
    distinct_participants: int
    #: The guild's `timezone` setting, or `UTC` when it is unset or
    #: unusable. Months are cut in it, the same calendar the protocols are
    #: written in.
    zone: tzinfo
    zone_name: str


class GuildReports(Protocol):
    """What a guild's recording adds up to, if the person asking administers it.

    `requested_by` is not optional and there is no method here without it,
    for the reason the other three directories have none. `None` covers
    "no such guild" and "you do not administer it" alike.
    """

    async def recording_of(self, guild_id: int, *, requested_by: int) -> GuildRecording | None: ...
