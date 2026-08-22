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
from datetime import date, datetime
from typing import Protocol

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
