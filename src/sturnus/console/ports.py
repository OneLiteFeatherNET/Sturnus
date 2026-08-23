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

from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import Any, Protocol

from sturnus.application.collection_mirror import MirroredCollection
from sturnus.application.directory_mirror import (
    MirroredChannel,
    MirroredMember,
    MirroredRole,
)
from sturnus.application.priorities import Placement
from sturnus.console.filters import SessionFilter
from sturnus.console.reporting import RecordedSession
from sturnus.console.statistics import (
    AttendedSession,
    SessionName,
    SessionPage,
    SessionTranscript,
    TagUse,
)
from sturnus.domain.exports import ExportTarget, SessionDocument
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


class ProfileDirectory(Protocol):
    """The name to greet a signed-in person by.

    Deliberately not a second direction on `LinkDirectory`. That protocol
    exists to answer *who is this*, which is an authorisation question and
    the only bridge between an authenticated identity and a Discord id.
    This one answers *what do we call them*, which decides nothing at all
    -- and keeping them apart is what stops a handler that wanted a name
    holding an object that can also resolve identities.

    The provider is the adapter's business. There is exactly one identity
    provider for the console (`sturnus.console.auth.PROVIDER`), so a
    caller that had to name it would be a caller that could name the
    wrong one.

    `None` for somebody with no link row. That should not arise -- a
    session exists because a link was found -- but a link can be removed
    while a cookie is still valid, and a missing name is not an error.
    """

    async def display_name_for(self, discord_user_id: int) -> str | None: ...


class PreferenceDirectory(Protocol):
    """One person's own console preferences, read whole and written one key at a time.

    `guild_config`'s arrangement one layer down, and narrow for the same
    reasons `SettingsStore` is: `snapshot` is read while a page is being
    rendered, so it answers every known key in one query rather than
    inviting a handler to loop over `KNOWN_KEYS`; and the values it
    answers are already layered over `sturnus.domain.preferences.DEFAULTS`,
    so no caller has to know the defaults and none of them can forget to.

    **`set` is where validation lives, and it must stay there.** It
    refuses a key nobody reads and a value outside `ALLOWED_VALUES`, and
    the API's job is to turn that `ValueError` into a 400 -- never to
    check the same thing first. Two copies of a rule is how the two
    drift.

    `None` as a value removes the preference and restores the default. It
    is not the same as storing the default string: an absent row means
    "never expressed", which is what lets a future change to `DEFAULTS`
    reach everybody who never disagreed with it.

    **There is no `discord_user_id` that does not come from a session.**
    Every method names whose preferences these are, and the only caller
    passes the id out of the signed cookie -- see
    `sturnus.console.routes_me` on why no endpoint takes one in a path.
    """

    async def snapshot(self, discord_user_id: int) -> dict[str, str]: ...

    async def set(
        self, discord_user_id: int, key: str, value: str | None, now: datetime
    ) -> None: ...


@dataclass(frozen=True)
class GuildDirectory:
    """The names behind one guild's ids, as the bot last saw them.

    The three mirrors read together and answered as one value, because
    the console renders them together: a settings page resolves a channel
    id, two role ids and a list of people in one paint, and three
    endpoints would be three round trips to draw one form.

    The entries are `sturnus.application.directory_mirror`'s own values
    rather than copies. The mirror was written for this reader -- its
    docstrings say "as the console will eventually offer it" -- and a
    parallel set of identical dataclasses would be two definitions of one
    channel, drifting the first time Discord adds a field.

    `synced_at` is **the oldest** of the three mirrors, not the newest.
    One sweep writes all three on one tick, so they normally agree; when
    they do not, it is because a write failed and the next sweep has not
    come round, and a payload claiming the freshness of its freshest part
    would tell a reader the whole directory is minutes old when half of
    it is a day old. `None` for a guild nothing has been mirrored for
    yet, which is a real state and not an error: the bot sweeps on a
    timer and a freshly configured guild has not had its turn.
    """

    #: What the server itself is called, or `None` while the bot has not
    #: swept it yet. Carried with the three mirrors rather than fetched
    #: separately: a page that resolves a channel id and two role ids in
    #: one paint also has a heading to write, and a second request for
    #: one string is a second request forever.
    name: str | None
    channels: tuple[MirroredChannel, ...]
    roles: tuple[MirroredRole, ...]
    members: tuple[MirroredMember, ...]
    synced_at: datetime | None


@dataclass(frozen=True)
class AdministeredGuild:
    """One guild somebody administers, as far as anything has been able to name it.

    `name` is nullable because the mirror is written by a sweep on a
    timer: a guild the bot joined a minute ago has no row yet, and that
    is a real state rather than an error. It is still a guild this person
    administers, so it is still listed -- dropping it would lock somebody
    out of the very guild they came to configure -- and the caller
    renders the id, which is what every console did for every guild
    before there was a name to render at all.
    """

    guild_id: int
    name: str | None
    icon_url: str | None


class GuildNames(Protocol):
    """A guild's mirrored names, if the person asking administers it.

    `requested_by` is not optional and there is no method here without
    it, for the reason `TrackDirectory`, `QueueControl`, `ConsentDirectory`
    and `QueueOverview` have none: the authorisation rule lives inside the
    call rather than in a handler that could forget to apply it. `None`
    covers "no such guild" and "you do not administer it" alike, because
    from outside those must look the same -- this names the people who
    consented to being recorded.

    `administered` answers the guild switcher: the guilds this person
    administers, named. It is the same authorisation question
    `AdminDirectory.administered_guilds` answers and deliberately not a
    wider one -- a name is not a reason to show somebody a guild they
    have no business with -- so the implementation asks that directory
    first and names what it says, never the other way round.
    """

    async def for_guild(self, guild_id: int, *, requested_by: int) -> GuildDirectory | None: ...

    async def administered(self, *, requested_by: int) -> Sequence[AdministeredGuild]: ...


@dataclass(frozen=True)
class CollectionListing:
    """Outline's collections, as the worker last saw them.

    One `synced_at` for the whole listing rather than one per entry,
    because the mirror is replaced wholesale on every sweep: every row
    carries the same instant, and repeating it per collection would
    suggest they could differ. `None` before the first sweep lands.
    """

    collections: tuple[MirroredCollection, ...]
    synced_at: datetime | None


class CollectionNames(Protocol):
    """The mirrored Outline collections, for somebody who administers a guild.

    The one directory here whose rule is not per guild, because the thing
    it describes is not per guild: one deployment talks to one Outline
    instance, so a collection is a fact about that instance. The question
    that remains is whether the caller administers anything at all -- a
    person who administers nothing never configures a `document_target`
    and has no use for the list.

    `None` rather than an empty sequence for that person, and it is the
    same `None` a directory gives for a guild that does not exist: an
    empty listing is a real answer meaning "the worker has not swept
    yet", and folding the two together would make a refusal look like a
    fresh install.
    """

    async def mirrored(self, *, requested_by: int) -> CollectionListing | None: ...


class ExportTargets(Protocol):
    """A guild's publishing destinations, read and written by its administrators.

    **No `requested_by` here, and that is the exception rather than the
    rule.** Every other directory in this module carries the authorisation
    inside the call, because the thing it returns is somebody's voice or
    somebody's name and a handler that forgot the check would disclose it.
    This one is guild *configuration*, and it is authorised exactly the way
    `SettingsStore` is: `sturnus.console.routes_exports` asks
    `AdminDirectory.is_admin` before it touches this port at all, in a
    single guard every handler calls first. Following `SettingsStore`'s
    shape rather than inventing a third pattern is what lets one store --
    `sturnus.infrastructure.db.export_targets.ExportTargetStore` -- satisfy
    it unchanged, so the API cannot drift from the rules the store enforces.

    **`secret_for` is deliberately absent.** The store has it; this port
    does not, so nothing reachable from an HTTP handler can call it. That
    is the whole design of `ExportTarget`, restated at the layer where it
    would be lost: the read model has nowhere to put a credential, and the
    one method that could produce one is not in the console's hands.
    """

    async def all_for(self, guild_id: int) -> Sequence[ExportTarget]: ...

    async def get(self, guild_id: int, target_id: int) -> ExportTarget | None: ...

    async def save(
        self,
        guild_id: int,
        *,
        format: str,
        name: str,
        target: str,
        config: Mapping[str, Any],
        enabled: bool = True,
        now: datetime,
    ) -> int: ...

    async def delete(self, guild_id: int, target_id: int) -> bool: ...

    async def set_secret(
        self, guild_id: int, target_id: int, secret: str | None, now: datetime
    ) -> bool: ...


class SessionDocumentDirectory(Protocol):
    """What a session published, for somebody already entitled to it.

    **The second port here that does not carry `requested_by`**, and it is
    absent for exactly the reason `TranscriptReader`'s is: the rule that
    governs a session's protocols is the rule that governs the session, so
    it is `SessionReads.session_for` -- called first, in the handler, the
    same call `/api/sessions/{id}` and `/api/sessions/{id}/transcript` are
    both served from. Expressing it as a *second* `WHERE` on
    `session_participant` would be a second place for the three answers to
    diverge, and a protocol is the same meeting the transcript is.

    The consequence is a pair of methods that must never be called without
    that read having already succeeded. `sturnus.console.routes_documents`
    is their only caller and says so at the call site.

    `None` for a session that does not exist and for a destination this
    session never reached. An empty sequence is a different answer and a
    real one: a meeting still being transcribed, or a guild that has
    configured nowhere to publish.
    """

    async def documents_of(self, session_id: int) -> Sequence[SessionDocument] | None: ...

    async def document_of(self, session_id: int, target_id: int) -> SessionDocument | None: ...


class DocumentArtefacts(Protocol):
    """Where a stored protocol's bytes are read from. `S3DocumentStore`.

    `KeyError` for an object that is not there, which is an ordinary
    outcome rather than a fault: a re-export can move nothing, but a
    destination removed from the bucket by hand leaves the row behind.
    """

    async def get(self, key: str) -> bytes: ...


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

    #: Everything this person was in. Unpaged, and its two callers need
    #: it that way: the dashboard's figures are over a whole history, and
    #: the calendar draws a whole year.
    async def sessions_for(self, discord_user_id: int) -> Sequence[AttendedSession]: ...

    #: One window of the same list, with how many there are in all. A
    #: separate method rather than optional arguments on the one above,
    #: because a caller that forgot to pass a window would silently get
    #: the whole history -- and the endpoint that serialises a whole
    #: history is the one this method exists to stop existing.
    #: `matching` narrows inside the statement rather than afterwards,
    #: which is why it is a parameter here and not something a handler
    #: does to the answer: a filter applied to results is a filter that
    #: has already fetched what it is about to discard.
    async def sessions_page(
        self,
        discord_user_id: int,
        *,
        limit: int,
        offset: int,
        matching: SessionFilter,
    ) -> SessionPage: ...

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

    #: Every label this person has put on a recording, with how many
    #: recordings carry it. Theirs and only theirs -- `session_tag` is
    #: keyed by its owner, so there is no reading of it that does not
    #: name whose labels are wanted.
    async def tags_of(self, discord_user_id: int) -> Sequence[TagUse]: ...


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


@dataclass(frozen=True)
class DownloadableTrack:
    """A recording somebody may take a copy of, and how they came by it.

    More than a `Track` because a download is audited and a playback is
    not enough of an event to need it: the log line has to say which guild
    the recording belongs to and whether the person taking the copy was in
    the room. Neither is recoverable afterwards -- `session_participant`
    can change, and nothing else records that a copy was ever made.

    `by_participant` is deliberately an answer from the query rather than
    a second question a handler asks. The statement already knows: it is
    what decided that the person may have the track at all.
    """

    track: Track
    guild_id: int
    #: Whether the person who asked was in the session. False is the case
    #: this whole capability exists for, and the case worth reading a log
    #: for: an administrator reaching a meeting they were not part of.
    by_participant: bool


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

    **Two methods, because there are two rules.** `track_for` is playback
    and its rule has not moved: participants of the session, nobody else.
    `downloadable_track_for` is the wider one the repository owner decided
    to add -- an administrator of the guild may take a copy of any of its
    recordings, and a guild has to switch the capability on first. They
    are separate methods rather than one method with a flag so that a
    handler cannot ask the wide question by accident, and so that the two
    read as the two different acts they are.
    """

    async def track_for(
        self, session_id: int, speaker_id: int, *, requested_by: int
    ) -> Track | None: ...

    #: `None` for every reason a download can be refused, and the caller
    #: must not be able to tell them apart: no such session, no such
    #: recording, the audio was swept, this person is neither a
    #: participant nor an administrator, or the guild has not switched the
    #: capability on. "You are not an administrator" and "there is no such
    #: recording" are the same answer on this path.
    async def downloadable_track_for(
        self, session_id: int, speaker_id: int, *, requested_by: int
    ) -> DownloadableTrack | None: ...


class TagWriter(Protocol):
    """The one way a label is written, and it names its owner.

    Separate from `SessionReads` because it is the only write the console
    has that is not a settings change, and because the read side of tags
    is answered inside the session query rather than through a port of its
    own (see `sturnus.console.queries`).

    `owner` is not optional and there is no method here without it. It is
    both halves of the rule at once: a tag belongs to the person who wrote
    it, and a person may only tag a session they were in. The second half
    is checked by the statement -- `replace` answers `None` for a session
    that does not exist *and* for one this person was not in, because
    from outside those must look the same, which is the same 404 the audio
    endpoint gives for the same reason.
    """

    #: The tags as they were stored, or `None` if this person was not in
    #: that session. The stored form is the answer rather than the
    #: submitted one, because normalisation may have merged two of them.
    async def replace(
        self, session_id: int, *, owner: int, tags: Sequence[str], now: datetime
    ) -> tuple[str, ...] | None: ...


class SessionNaming(Protocol):
    """The one way a meeting's title and description are written.

    Separate from `TagWriter` because they are separate features that
    happen to look alike: a tag is one person's private label and a title
    is the session's shared name (`sturnus.console.naming` argues why).
    They share the authorisation rule and nothing else -- a participant of
    the session, and there is no method here without one.

    `by` is not optional and is checked by the statement, exactly as
    `TagWriter.owner` is: `rename` answers `None` for a session that does
    not exist *and* for one this person was not in, because from outside
    those must look the same. It is not recorded anywhere on the row.
    Who last renamed a meeting is a question this feature deliberately
    does not answer -- it is a shared name, and a name with an author
    attached is a name people argue about.

    What is stored is what `sturnus.console.naming` produced, which is
    what was typed with its whitespace tidied. The refusals live there,
    at the edge, once.
    """

    #: The name as it now stands, or `None` for a session not theirs.
    #: The stored pair rather than the submitted one, for the reason
    #: `TagWriter.replace` returns the stored tags: trimming may have
    #: changed what was sent, and a client shown its own input back would
    #: keep displaying a title the database does not have.
    async def rename(
        self, session_id: int, *, by: int, title: str | None, description: str | None
    ) -> SessionName | None: ...


class TranscriptReader(Protocol):
    """One session's assembled transcript, for somebody already entitled to it.

    **The one port here that does not carry `requested_by`, and why that
    is not the hole it looks like.** Every other read in this package is
    scoped by its own statement, because an authorisation a handler
    applies afterwards is one a handler can forget. This one is scoped by
    `SessionReads.session_for`, called first, in the handler -- which is
    not a second copy of the participant rule but literally the same call
    the session's own metadata endpoint makes. That is the requirement:
    the transcript is already inside the published document, so whoever
    may read the session may read what it said, and expressing that as a
    *second* `WHERE` on `session_participant` would be a second place for
    the two answers to diverge.

    The consequence is a method that must never be called without that
    read having already succeeded. `sturnus.console.routes_recording` is
    its only caller and says so at the call site.

    `None` for a session that does not exist. A session that exists but
    is still recording is not `None`: it answers with no blocks and with
    the count of tracks still to come, because "not yet" and "never" are
    different sentences on a transcript tab.
    """

    async def transcript_of(self, session_id: int) -> SessionTranscript | None: ...


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
    #: The transcription model this re-queue asked the worker for, and
    #: `None` when nothing was written. It is the *request*
    #: (`transcription_job.requested_model`), never a claim about what ran
    #: -- no worker has necessarily touched the job yet, and there may be
    #: none running at all. What actually ran is `transcription_job.model`,
    #: written by `JobQueue.complete` afterwards, and the two columns exist
    #: separately precisely so that they can disagree and be seen to.
    model: str | None


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

    `model` is likewise not optional, and it is a name rather than
    `str | None`. Turning "nobody chose" into a concrete registered name is
    `sturnus.domain.transcription_models.resolve`'s job, at the HTTP
    boundary where a caller who named something nobody has can still be
    told so; below that line an absent choice does not exist, which is what
    keeps `transcription_job.requested_model` a record of what was asked
    for rather than of what was not.
    """

    async def status_for(self, session_id: int, *, requested_by: int) -> QueueSnapshot | None: ...

    async def requeue(
        self, session_id: int, *, requested_by: int, model: str
    ) -> RequeueOutcome | None: ...

    async def place(
        self, session_id: int, *, requested_by: int, placement: Placement
    ) -> QueueOrder | None: ...


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
    #: What the grant covers -- `sturnus.domain.consent.ConsentScope`.
    #: An administrator looking at a roster has to be able to see that
    #: one person's consent is wider than everybody else's, or the
    #: setting that made it possible is a switch with no readout.
    scope: str


@dataclass(frozen=True)
class ConsentPage:
    """One window of a guild's consent roster, and how many people it has.

    The same four fields `sturnus.console.statistics.SessionPage` carries,
    deliberately: this API has one shape for a paged list, and a second
    one would be a second thing for every client to learn. The total
    travels with the rows for the reason it does there -- a count fetched
    by a separate request can be one grant older than the page beside it,
    and a roster reading "1-20 of 47" while holding twenty-one people is
    worse than one that says nothing.

    **The order is the statement's, not the reader's.** Everything below
    the first page depends on it: a listing whose order two statements
    disagree about is a listing that silently skips somebody between page
    one and page two. `ConsoleConsentDirectory.holders` names the key and
    the tiebreak.
    """

    holders: tuple[ConsentHolder, ...]
    #: How many people this guild holds a consent record for, not how many
    #: are on this page. One person who granted, withdrew and granted
    #: again is three `consent` rows and one here.
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class RevocationOutcome:
    """What a revocation did, or why it did nothing."""

    revoked: bool
    #: Why nothing happened, as one of a fixed set of reasons. `None` when
    #: something did.
    refusal: str | None
    #: The instant the consent stops. `None` when nothing was written.
    #: Echoed back rather than assumed by the caller, because a request
    #: that named no instant gets `now` -- and the client showing a
    #: person "withdrawn as of ..." must show what was stored, not what
    #: it guessed would be stored.
    effective_at: datetime | None = None
    #: How many recordings of this person the guild still holds that fall
    #: **on or after** `effective_at`. Not a delete and not a promise of
    #: one: a back-dated revocation is a statement about recordings that
    #: already exist, and this is how many of them the statement is about
    #: so the console can offer the erasure path that does delete
    #: (`/audio purge`). Zero for a revocation dated now or later, which
    #: is the ordinary case.
    recordings_from_effective_at: int = 0


@dataclass(frozen=True)
class PersonRevocation:
    """What one person's withdrawal did, inside a batch of them.

    A pair rather than a wider `RevocationOutcome` with an id on it: the
    outcome of a withdrawal is the same thing whether one person or nine
    were named, and giving it a second shape for the batch case would be
    two definitions of what a revocation answers, agreeing right up until
    one of them changed.
    """

    discord_user_id: int
    outcome: RevocationOutcome


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

    #: One window of the roster. `limit` and `offset` are required and
    #: have no defaults, so there is no call here that reads a whole
    #: guild -- the narrowest thing this protocol can express is already
    #: bounded, and nothing wider exists for a handler to reach for.
    async def holders(
        self, guild_id: int, *, requested_by: int, limit: int, offset: int
    ) -> ConsentPage | None: ...

    #: `effective_at` is the instant the consent stops, and `None` means
    #: now -- which is what every caller meant before it existed, so no
    #: client breaks by not sending it. Any instant not before
    #: `granted_at` is allowed: a future one is a scheduled withdrawal
    #: that the recorder honours on its own, a past one is a statement
    #: about recordings that already exist and **deletes none of them**.
    async def revoke(
        self,
        guild_id: int,
        discord_user_id: int,
        *,
        requested_by: int,
        effective_at: datetime | None = None,
    ) -> RevocationOutcome | None: ...

    #: Several people, one instant, one answer per person **in the order
    #: they were named**. Partial success is the ordinary case rather
    #: than the exception here: one name whose consent a colleague
    #: withdrew five minutes ago must not refuse the eight withdrawals
    #: beside it, so each is decided on its own and each is reported on
    #: its own. Whether a person may be *asked about* is still one
    #: decision for the whole call, and it is the same one `revoke`
    #: makes: `None` for "no such guild" and "not yours" alike.
    async def revoke_many(
        self,
        guild_id: int,
        discord_user_ids: Sequence[int],
        *,
        requested_by: int,
        effective_at: datetime | None = None,
    ) -> Sequence[PersonRevocation] | None: ...


@dataclass(frozen=True)
class OwnConsent:
    """One person's consent in one guild, as that person sees it.

    The same newest-row-per-guild selection `ConsentHolder` makes, from
    the other end: keyed by guild rather than by person, because the
    question here is "where am I recorded" rather than "who consented
    here". No display name -- a person does not need to be told their own
    -- and no `recordings_with_audio`, which the person's own session
    list already answers in far more detail than a count.

    `state` and `active` are both here and neither is redundant. `state`
    names *why*, which is the sentence the interface has to write, and
    the four values are not two pairs: `SCHEDULED` means a withdrawal is
    dated in the future and recording is still happening, which reads as
    "ending" and behaves as "active". A client deriving one from the
    other would be reimplementing
    `sturnus.domain.consent.is_consent_active`, which is exactly what
    `ConsentHolder.active` exists to prevent.
    """

    guild_id: int
    state: str
    active: bool
    scope: str
    #: The version the grant names, which is not necessarily the guild's
    #: current one -- that difference is the whole of `POLICY_SUPERSEDED`.
    policy_version: str
    #: What the guild requires today. Sent alongside rather than compared
    #: here, so a person can be shown "you consented to 2026-01, the
    #: policy is now 2026-06" rather than only the verdict.
    guild_policy_version: str
    granted_at: datetime
    revoked_at: datetime | None
    #: Whether this guild offers the video scope at all
    #: (`settings.VIDEO_CONSENT_OFFERED`). False means the control is
    #: absent from the interface, not disabled: an administrator has not
    #: asserted that the policy document names video, and offering a
    #: choice the API will refuse is worse than not offering it.
    video_consent_offered: bool


@dataclass(frozen=True)
class ScopeOutcome:
    """What a scope change did, or why it did nothing."""

    scope: str
    changed: bool
    #: Why nothing happened, as one of a fixed set of reasons. `None`
    #: when something did.
    refusal: str | None
    #: The policy version the scope now stands under. For a widening this
    #: is the guild's current version, because a widening inserts a new
    #: grant; for a narrowing it is the version the existing grant already
    #: named. `None` when nothing was written.
    policy_version: str | None = None


class PersonalConsents(Protocol):
    """A signed-in person's own consent records, and the two things they may change.

    **Authorisation here is the session and nothing else.** Every method
    takes the person as its first argument and there is no method that
    takes a subject separate from the actor -- which is the difference
    between this protocol and `ConsentDirectory`, and the reason they are
    two protocols rather than one with a flag. A handler cannot
    accidentally act on somebody else through this, because there is no
    argument for somebody else.

    `None` means "you have no consent record in that guild", which is
    also the answer for a guild id that names nothing at all. The person
    is asking about themselves, so there is nothing here to conceal by
    conflating the two -- they are conflated because they are the same
    fact.
    """

    async def for_person(self, discord_user_id: int) -> Sequence[OwnConsent]: ...

    #: Narrowing takes effect at once and modifies the grant. Widening is
    #: a new grant carrying the guild's current `policy_version`, and is
    #: refused outright while the guild does not offer video consent.
    async def set_scope(self, discord_user_id: int, guild_id: int, scope: str) -> ScopeOutcome: ...

    #: The person withdrawing their own consent. It writes the record and
    #: cannot remove the Discord role -- `api` holds no Discord token
    #: (Spec 13.2) -- which is why the outcome says so rather than
    #: leaving them to find out from `/consent status`.
    async def revoke_own(self, discord_user_id: int, guild_id: int) -> RevocationOutcome: ...


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
    #: Where this session sits in its guild's queue, lower first, or
    #: `None` when it has no outstanding jobs to sit anywhere. Zero is the
    #: ordinary priority and a real place; `None` is a meeting that is
    #: still recording, or one that is only still listed because a job of
    #: it died. The distinction is what tells a page which rows can be
    #: dragged.
    priority: int | None


@dataclass(frozen=True)
class QueuePosition:
    """One session's place in its guild's queue, after a reorder."""

    session_id: int
    priority: int


@dataclass(frozen=True)
class QueueOrder:
    """A guild's queue order, and what a reorder did to it.

    `sessions` is the whole outstanding queue in claim order, always --
    on a refusal as much as on a success. A drag is aimed at a list the
    browser was showing, and the two ways it fails (the session finished,
    the session it was dropped beside finished) are both "your list is out
    of date": sending the current one back with the refusal is what lets a
    page redraw instead of asking again and failing again.

    `changed` names the sessions whose priority was written, and is empty
    when the order asked for was the order that already held. It is not
    derivable from `sessions`, and it is the difference between "done" and
    "there was nothing to do" -- which an administrator who dragged
    something two pixels deserves to be told apart.
    """

    accepted: bool
    #: Why it was refused, in one sentence, or `None`. A fixed string, so
    #: a console can key off it without any input being echoed back.
    refusal: str | None
    sessions: tuple[QueuePosition, ...]
    changed: tuple[int, ...]


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

    async def reprioritise(
        self, guild_id: int, *, requested_by: int, rule: str
    ) -> QueueOrder | None: ...


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
