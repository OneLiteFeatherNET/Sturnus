"""SQLAlchemy models. The system's only data access path."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class GuildConfig(Base):
    __tablename__ = "guild_config"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserPreference(Base):
    """What one person decided about their own view of the console.

    `GuildConfig` one layer down: the same key/value pair, the same
    nullable value, keyed by person instead of by guild. The shape is
    copied deliberately -- adding a second preference is a write rather
    than a migration, a preference nobody expressed is an absent row
    rather than a column full of nulls, and the registry of which keys
    and values are legal lives in `sturnus.domain.preferences` where the
    writer and the reader both read it from.

    No foreign key: a preference belongs to a Discord identity, and this
    system holds no table of Discord identities. `account_link` is about
    Outline accounts and `session_participant` is about meetings somebody
    attended; neither is the set of people who may open the console.
    """

    __tablename__ = "user_preference"

    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Guild(Base):
    """The name of a guild, mirrored so `api` can say what it is called.

    The mirror nobody built. `guild_channel`, `guild_role` and
    `guild_member` have existed since 0011 and answer "what is this
    channel called"; nothing answered "what is this *server* called", so
    `GET /api/guilds` returns ids and every guild switcher in the console
    renders "Server 1289374650912837465" -- on every admin page, which
    makes it the most visible instance of exactly the problem the other
    three mirrors were written to solve.

    Written by `bot` on the same sweep that writes the other three, for
    the same reason: `api` has no gateway and must not be given one
    (Spec 13.2). Read by `api` and nobody else.

    **There is no way to clear a name here, and that is deliberate.** The
    other mirrors are lists, so they need a replacement that can shrink;
    a guild's name is a single fact, and the only honest representation
    of "the bot cannot currently see this guild" is the row staying as it
    was. That is the skip-versus-clear distinction
    (`sturnus.application.admin_mirror`) applied to a single row: the
    caller skips a guild it could not read, and this table offers nothing
    that could empty one on a gateway hiccup.

    `icon_url` is nullable and is carried because the gateway object that
    has the name has it too -- adding it later would mean a second
    migration for a field the sweep was already holding. Nothing renders
    it yet.
    """

    __tablename__ = "guild"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    icon_url: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GuildChannel(Base):
    """One channel of one guild, mirrored so `api` can name it.

    The console makes an administrator paste a raw snowflake into
    `voice_channel_id` and then shows it back as a snowflake, because
    `api` has no Discord token to ask what that channel is called and
    must not be given one (the console design's Section 2.1). `bot`,
    which has the gateway, writes the name here instead -- the same
    arrangement `AdminMember` already established.

    `kind` is a plain string rather than an enum because Discord keeps
    adding channel types. A type this code has never seen must be a row
    a reader ignores, not a failed write that takes the rest of the
    sweep with it.
    """

    __tablename__ = "guild_channel"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GuildRole(Base):
    """One role of one guild, mirrored so `api` can name it.

    `consent_role_id` and `admin_role_id` are snowflakes an administrator
    typed; this is what turns them back into the words that were on
    screen when they copied them. `position` is carried because it is the
    order Discord itself shows roles in, and a picker that reorders them
    is a picker that does not look like the server it configures.
    """

    __tablename__ = "guild_role"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GuildMember(Base):
    """The display name of somebody the console has reason to name.

    **Deliberately not every member of the guild.** The sweep writes
    exactly the holders of the consent role and of the admin role, which
    is the bounded set every page that names a person draws from: a
    consent roster, the speakers in a queue, an administrator list.
    Mirroring the whole member list would copy a Discord user directory
    into a database that exists to hold recordings, for people who never
    joined a recorded channel and consented to nothing.

    A person this table does not hold is a person the console shows as an
    id. That is the intended outcome, not a gap to be closed later.
    """

    __tablename__ = "guild_member"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutlineCollection(Base):
    """One Outline collection, mirrored so `api` can name it.

    The same argument as `GuildChannel`, with a different credential:
    `document_target` is a collection UUID an administrator pasted, and
    the Outline API token that could resolve it belongs to `worker`, not
    to `api`. `worker` sweeps the collection list and writes it here.

    Not keyed by guild: one deployment talks to one Outline instance
    (`OutlineSink` holds one `base_url`), so a collection is a fact about
    that instance rather than about any guild that might point at it.
    """

    __tablename__ = "outline_collection"

    collection_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AccountLink(Base):
    __tablename__ = "account_link"

    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    external_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Consent(Base):
    """One decision about being recorded, kept forever.

    Append-only: a grant inserts a row, `ConsentRepository.current` reads
    the newest by `granted_at`, and nothing is ever deleted -- the row is
    the evidence that consent was once given.

    `revoked_at` is an **effective instant**, not a tombstone. Any value
    used to mean "not active"; it now means "not active from then on", so
    a withdrawal can be dated to the end of the month or to last
    Tuesday's meeting. `sturnus.domain.consent.is_consent_active` is the
    one place that rule is written down.
    """

    __tablename__ = "consent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    #: What this grant covers -- `sturnus.domain.consent.ConsentScope`.
    #: Text with a server default rather than an enum, so a writer that
    #: forgets it produces the narrow scope instead of failing, and a
    #: value this code cannot name is read as narrow rather than
    #: refusing the row (`consent.scope_of`).
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="audio")

    __table_args__ = (Index("ix_consent_user_guild", "discord_user_id", "guild_id"),)


class OAuthState(Base):
    __tablename__ = "oauth_state"

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConsoleState(Base):
    """A single-use OAuth state for a console sign-in.

    A table of its own rather than a row in `oauth_state`, for a reason
    that is about identity rather than tidiness: `/link` knows who is
    linking before the browser ever leaves -- a slash command was run by
    somebody -- so its state carries a `discord_user_id`. A console
    sign-in does not. Who this is only becomes known when the provider
    answers, which is after the round trip.

    Squeezing that into `oauth_state` would have meant either a nullable
    owner or a placeholder id, and both make "a pending account link" and
    "a pending console sign-in" the same row shape. The link callback's
    consumer does not filter by provider, so it would then consume a
    console state and mint an account link for a user id that does not
    exist.
    """

    __tablename__ = "console_state"

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Which guild's OAuth client this sign-in was started against, or
    #: null for the environment-configured one.
    #:
    #: **The state is what selects the client for the code exchange.**
    #: `/api/auth/login` takes no parameters and reads no cookie, so it
    #: cannot choose a guild's client from an identity it does not have
    #: yet; `/g/{slug}/sign-in` puts the guild in the URL, this column
    #: carries it across the round trip, and the callback reads it back.
    #:
    #: Nullable because a sign-in with no guild is the ordinary case and
    #: stays supported exactly as it is: a deployment that never
    #: configures a per-guild client behaves identically to v0.15.0.
    guild_id: Mapped[int | None] = mapped_column(BigInteger)


class Session(Base):
    __tablename__ = "session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: The channel's name when the session opened. The worker writes the
    #: protocol and has no Discord connection, so it cannot look this up;
    #: the bot records it here. Nullable: sessions from before this column
    #: existed have none, and a later rename must not rewrite old protocols.
    channel_name: Mapped[str | None] = mapped_column(Text)
    #: What a participant called this meeting, and what they said about
    #: it. Free text, editable by anybody who was in the room, and null
    #: for every session nobody has titled -- which is all of them until
    #: somebody types one. An empty string and a null would be the same
    #: fact told two ways, so the write path stores null.
    #:
    #: Searchable, and not indexed here. See migration 0013: an `ILIKE
    #: '%…%'` over free text is answered by a GIN trigram index and by no
    #: btree, and a trigram index needs `CREATE EXTENSION pg_trgm` --
    #: a privileged statement in a migration the worker runs in-process
    #: at startup. It belongs to the branch that writes the search query,
    #: where failing to create it is a feature that will not switch on
    #: rather than a deployment that will not start.
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    document_provider: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[str | None] = mapped_column(Text)
    document_url: Mapped[str | None] = mapped_column(Text)
    announced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The session's own data key -- the source of truth for which key
    # encrypted this session's recordings. Written once, when the session
    # opens, so crash recovery can read back the key that actually
    # encrypted whatever `.enc` files are left on disk instead of
    # generating one that cannot decrypt them. Nullable because sessions
    # predating this column, and sessions that crashed before this row was
    # even written, have neither value.
    encryption_key_id: Mapped[str | None] = mapped_column(Text)
    wrapped_data_key: Mapped[bytes | None] = mapped_column(LargeBinary)

    __table_args__ = (Index("ix_session_status", "status"),)


class SessionParticipant(Base):
    __tablename__ = "session_participant"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("session.id", ondelete="CASCADE"))
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_display_name: Mapped[str] = mapped_column(Text, nullable=False)
    detected_language: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    audio_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When this speaker's audio was first seen to be arriving with no
    #: audible level in it -- packets received and decoded, every sample at
    #: the noise floor (`sturnus.domain.silence`). The bot writes it during
    #: the session, at the same moment it says so in the channel, because
    #: the message is gone by the next meeting and this is what an operator
    #: can still read afterwards: it is what separates "we could not hear
    #: them" from "they said nothing", which two empty transcripts left
    #: unanswerable. Nullable, and null for nearly everybody: being quiet
    #: is normal, so a value here means something on its own.
    silent_audio_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("session_id", "discord_user_id", name="uq_participant_per_session"),
    )


class SessionTag(Base):
    """One label one person put on one meeting they were in.

    **The owner is part of the primary key, and that is the privacy
    story rather than a detail of it.** A tag is not a property of the
    meeting: it is a remark about a conversation other people were also
    in. Keying it by `(session_id, discord_user_id, tag)` means two
    participants can label the same session differently without
    overwriting each other, and every read names `discord_user_id` -- so
    nobody ever sees anybody else's labels, and no query can be written
    that returns a tag without saying whose it is.

    Tags shared between a session's participants were considered and not
    built. Sharing is the irreversible direction: private tags can be
    made visible later by a decision, and tags people have already read
    cannot be made private again.

    `created_at` is here so a tag list can eventually be ordered by when
    somebody started using a label. There is no `updated_at`: a tag has
    no content to edit, and the write path replaces one owner's whole set
    for a session rather than editing a row.
    """

    __tablename__ = "session_tag"

    session_id: Mapped[int] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), primary_key=True
    )
    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tag: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Owner first, so that the index answering "which labels do I use"
    # cannot be walked usefully without naming whose labels are wanted.
    __table_args__ = (Index("ix_session_tag_owner", "discord_user_id", "tag"),)


class AdminMember(Base):
    """One Discord administrator of one guild, mirrored for `api` to read.

    `admin_role_id` is a Discord role, and the console's API process has no
    gateway to ask about role membership -- deliberately: a process that
    can decrypt every recording in the system is not one to also hand the
    ability to act as the bot (Spec 13.2). `bot`, which does hold the
    members intent, writes this table; `api` only ever reads it.

    Rows are replaced per guild rather than merged, because a revoked role
    that leaves a stale row behind is a privilege outliving its grant --
    and nothing downstream would ever show it, since every caller asks
    "is this person an admin" and never "why".
    """

    __tablename__ = "admin_member"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TranscriptionJob(Base):
    __tablename__ = "transcription_job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("session.id", ondelete="CASCADE"))
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    encryption_key_id: Mapped[str] = mapped_column(Text, nullable=False)
    wrapped_data_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    audio_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Set every time `claim` hands this job to a worker (including a
    # reclaim). `claim` reads this back to decide whether a `running` job's
    # lease has expired -- a worker killed mid-job (SIGKILL, an evicted pod)
    # would otherwise strand it `running` forever, since `claim` only ever
    # selects `pending` jobs and nothing else ever moves it out of
    # `running`. Nullable because a job that has never been claimed yet
    # (still `pending`) has no lease to speak of.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    # What the job measured about its recording, written on completion.
    # Nullable because every row that predates the column has audio that
    # may already be deleted -- there is nothing to backfill from, and a
    # zero would be a claim rather than an absence. See
    # `sturnus.domain.measurements.JobMeasurements` for why the three
    # belong together.
    audio_seconds: Mapped[float | None] = mapped_column(Float)
    speech_seconds: Mapped[float | None] = mapped_column(Float)
    segment_count: Mapped[int | None] = mapped_column(Integer)
    #: What produced the three above. Null for a job finished before this
    #: column existed -- there is nothing to backfill it from, and naming
    #: the current default would turn an absence into a claim.
    model: Mapped[str | None] = mapped_column(Text)

    #: What a re-queue asked this job to run with, or null for the
    #: worker's own default. Kept apart from `model` because the two
    #: answer different questions -- "what was asked for" survives a
    #: failure, "what ran" does not exist until one succeeds, and a
    #: comparison is only worth as much as the certainty about which
    #: engine produced each side of it.
    requested_model: Mapped[str | None] = mapped_column(Text)

    #: Where this job sits in the queue, **lower first**.
    #:
    #: Zero is normal and is what every job written before this column
    #: existed carries, so raising a job above the ordinary run means a
    #: negative number and holding one back means a positive one -- the
    #: sense `nice(1)` uses, and the sense Delayed::Job and Que use for
    #: exactly this table. The console never shows the number; it shows
    #: an order, and writes what that order implies.
    #:
    #: Lower-first is not arbitrary. The claim reads
    #: `ORDER BY priority, id`, and mixed directions cannot be served by
    #: one btree scan: `priority DESC, id ASC` would need an index with a
    #: descending column, where `priority ASC, id ASC` is a plain forward
    #: scan of `ix_job_claim_order` that also keeps first-in-first-out
    #: within a priority for free.
    #:
    #: Nothing reads it yet -- `JobQueue.claim` is unchanged in the
    #: migration that adds this. The column and its index land first so
    #: that the branch which adds the ordering adds a query and not a
    #: schema.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: What the recording is, as its own RIFF header declares it.
    #:
    #: Read live out of the object store on every request today
    #: (`sturnus.console.spectrogram.parse_track_format`), which means a
    #: ranged S3 GET and a chunk decrypt to answer "how many channels".
    #: The worker has the plaintext WAV on disk at the moment it
    #: transcribes and can simply write it down.
    #:
    #: Nullable with no backfill, exactly as `audio_seconds` and its two
    #: neighbours were: a row that predates the column has audio that may
    #: already have been deleted, so there is nothing to read it from,
    #: and stamping a default would turn an absence into a claim.
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    channels: Mapped[int | None] = mapped_column(Integer)
    #: The size of the stored object, `BigInteger` because a long meeting
    #: is hundreds of megabytes and a queue page that sums a guild's
    #: recordings is summing these.
    stored_bytes: Mapped[int | None] = mapped_column(BigInteger)

    #: The object-store key of this track's stored spectrogram, or null.
    #:
    #: **The rule attached to this column, which is not implemented
    #: here:** a stored spectrogram is deleted when its audio is deleted.
    #: The retention sweep deletes the S3 object and nothing else, so
    #: without that rule switching spectrograms on would create a
    #: retained rendering of a person's voice activity that outlives the
    #: retention window their audio was subject to. The console design
    #: already argues a spectrogram "is less than the audio and it is not
    #: nothing"; it must therefore not quietly become the thing that
    #: survives. Whoever next touches retention needs to know that before
    #: they read the sweep, which is why it is written here rather than
    #: only there.
    spectrogram_key: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("session_id", "discord_user_id", name="uq_job_per_speaker"),
        Index("ix_job_status", "status"),
        Index("ix_job_retention", "retention_until"),
        # The claim's order, in the claim's order: status narrows to the
        # pending jobs, priority orders them, and id breaks ties by age.
        # All ascending, which is what keeps it one forward index scan --
        # see `priority`.
        Index("ix_job_claim_order", "status", "priority", "id"),
    )


class GuildExportTarget(Base):
    """One destination one guild publishes its protocols to.

    **Not a `guild_config` key, and the reason is not tidiness.** The
    settings API renders every value in that registry straight back to
    whichever administrator asks for it -- that is what the settings page
    is -- so a Confluence token stored there would be a token the API
    hands out on request. It is also the wrong shape: a destination has
    structure (a base URL, a space key, a credential) and `guild_config`
    is a flat text registry.

    A guild may have several enabled targets. Publishing writes to each
    and records each outcome in `session_document`, because one failing
    destination must not lose the others.

    `format` names a renderer *and* a sink, never a sink alone:
    `render_transcript` emits Outline's `mention://` chips and escapes
    Markdown specials, so a PDF or HTML sink handed that string gets the
    mention syntax as literal text. A plain string rather than an enum,
    for the reason `guild_channel.kind` is one.

    `wrapped_secret` is wrapped **to this guild and to this purpose**
    (`sturnus.infrastructure.crypto.secret_context`). `KeyWrapper` alone
    seals bytes under the master key and says nothing about which row
    they came from, so an unbound blob moved into another guild's row
    would decrypt cleanly and publish under a credential that guild was
    never given. `encryption_key_id` names the master key that wrapped
    it, so rotation works the way it already does for audio data keys.

    Unique on `(guild_id, name)` because a name is how an administrator
    refers to a destination, and two destinations of one guild called the
    same thing make "publish to Wiki" ambiguous. Across guilds it is free.
    """

    __tablename__ = "guild_export_target"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    #: Whatever else this format needs -- a base URL, a parent page id.
    #: One JSON column rather than a column per format, which would be
    #: four nulls per row and a migration per destination type.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Null for a destination that needs no credential, and for one whose
    #: credential has not been supplied yet. Both are ordinary.
    wrapped_secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_key_id: Mapped[str | None] = mapped_column(Text)
    #: Switching a destination off is not forgetting how it was
    #: configured, so this is a column and not a deletion.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("guild_id", "name", name="uq_export_target_name"),)


class SessionDocument(Base):
    """One document one session produced, at one destination.

    `session.document_url` is **not** replaced by this and must not be.
    It is what the announcement posts and what everything already reading
    a session reads; this table is what the second, third and fourth
    destination get, so that a guild publishing to two places does not
    have to choose which one the session remembers.

    `target_id` is `ON DELETE SET NULL` rather than `CASCADE`, and that
    is the decision worth stating. Removing a destination is an
    administrator saying "stop publishing here", not "forget what was
    published": the document still exists in the other system, and the
    URL is what somebody follows when they go looking for last quarter's
    minutes. Cascading would delete the only pointer to it.

    Unique on `(session_id, target_id)`, so re-exporting to a destination
    overwrites its own row and nothing else -- appending would leave a
    session pointing at two documents in the same place, one of them
    stale, with nothing saying which. Postgres treats nulls as distinct
    in a unique index, so the rows whose destination has been removed
    accumulate rather than collide, which is the right behaviour for
    what they now are: history.
    """

    __tablename__ = "session_document"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    target_id: Mapped[int | None] = mapped_column(
        ForeignKey("guild_export_target.id", ondelete="SET NULL")
    )
    #: Copied from the target rather than joined, because it must outlive
    #: the target: a row whose `target_id` has gone to null would
    #: otherwise be unable to say what kind of document it points at.
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("session_id", "target_id", name="uq_document_per_target"),)


class GuildOAuthClient(Base):
    """One guild's console sign-in client, selected by the slug in its link.

    `GET /api/auth/login` takes no parameters and reads no cookie -- there
    is no session yet, that is what login is for -- so it cannot look up a
    guild's client from an identity it does not have. `/g/{slug}/sign-in`
    carries the guild in the URL instead.

    **`slug` is unique across the deployment, not per guild.** It is a
    public path segment and has to name exactly one guild; two guilds
    behind `/g/acme/sign-in` would send one of them through the other's
    identity provider. The alternative -- a public page listing every
    guild Sturnus serves -- discloses which organisations use the service
    to anyone, signed in or not, so an administrator distributes their
    own link.

    Keyed by `guild_id` because a guild has one client. Two would make
    "which one does this state select" a question the callback cannot
    answer, since `console_state.guild_id` is what selects it.

    `wrapped_client_secret` is wrapped to this guild and this purpose,
    exactly as `guild_export_target.wrapped_secret` is, and never read
    back out to an administrator: a `GET` on an OAuth configuration
    returns the client id, the base URL, the redirect URI and whether a
    secret is set. Nullable so that registering the client and supplying
    its secret can be two steps -- an administrator copies the id out of
    one screen and the secret out of another.

    `redirect_uri` nullable means "the one this deployment is configured
    with", which is what nearly every guild will want.

    **Console sign-in only.** `api` holds the master key and `link` does
    not; the chart's `_helpers.tpl` actively prevents adding it. The
    Discord account-link flow stays on the environment-configured client,
    and saying so here is what stops somebody "fixing the asymmetry"
    later by handing `link` the master key.
    """

    __tablename__ = "guild_oauth_client"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    wrapped_client_secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_key_id: Mapped[str | None] = mapped_column(Text)
    redirect_uri: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GuildSetupIntent(Base):
    """What the console asked the bot to do to a guild, and what happened.

    `api` must never hold a Discord token, so it cannot create the
    consent role or set the Speak overwrites. It writes down what should
    be true instead, and the bot's existing ten-second reconcile tick
    makes it true through the same `plan_setup` the slash command uses --
    the mirror arrangement run backwards.

    `channel_ids` is stored in exactly the format `guild_config` holds it
    in, the comma-separated list `settings.parse_channel_ids` reads, so
    applying an intent is a write of the value rather than a second
    serialisation nobody would keep in step with the first.
    `consent_role_name` is a name rather than an id because the role does
    not exist yet -- naming it is the request.

    `applied_at`, `outcome` and `error` are null while the intent is
    pending and written together when it settles. **A failure settles
    it.** The tick runs six times a minute forever: an intent left
    pending after being applied would re-create the role for the life of
    the guild, and one left pending after failing would retry a
    permission error against Discord's rate limiter just as often. An
    administrator who has fixed the permission asks again, which is a new
    row that says who asked and when.

    Indexed by `(guild_id, requested_at)` -- the tick reads one guild's
    intents oldest first, and applying them in request order is what
    makes a correction win over the mistake it corrected. No partial
    index on "still pending": the row count per guild is a handful, and a
    partial index would be a `WHERE` clause in two places that have to
    agree.
    """

    __tablename__ = "guild_setup_intent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    channel_ids: Mapped[str | None] = mapped_column(Text)
    consent_role_name: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: `sturnus.domain.onboarding.OUTCOMES`, as text rather than an enum
    #: so that an outcome this code has never seen is a row a reader
    #: ignores instead of a write that fails inside a reconcile tick.
    outcome: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_setup_intent_guild", "guild_id", "requested_at"),)
