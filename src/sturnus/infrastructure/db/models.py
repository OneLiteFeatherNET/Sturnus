"""SQLAlchemy models. The system's only data access path."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
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


class AccountLink(Base):
    __tablename__ = "account_link"

    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    external_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Consent(Base):
    __tablename__ = "consent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_consent_user_guild", "discord_user_id", "guild_id"),)


class OAuthState(Base):
    __tablename__ = "oauth_state"

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
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

    __table_args__ = (
        UniqueConstraint("session_id", "discord_user_id", name="uq_job_per_speaker"),
        Index("ix_job_status", "status"),
        Index("ix_job_retention", "retention_until"),
    )
