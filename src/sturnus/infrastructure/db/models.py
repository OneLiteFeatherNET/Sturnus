"""SQLAlchemy models. The system's only data access path."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
)
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

    __table_args__ = (
        UniqueConstraint("session_id", "discord_user_id", name="uq_job_per_speaker"),
        Index("ix_job_status", "status"),
        Index("ix_job_retention", "retention_until"),
    )
