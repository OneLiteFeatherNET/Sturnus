"""What `/queue` shows and what `/queue requeue` actually writes.

The command callbacks are invoked directly (`Command.callback`), the same
way `test_config_commands` and `test_link_cog` do: that is the coroutine
the cog defines, so calling it exercises the decision without a gateway.
The hand-rolled `_Response`/`_Followup`/`_Interaction` fakes are the same
shape as `test_config_commands`'s, and enforce the same contract Discord
really does -- an interaction is answered once, and a deferred one must
answer through `followup`.

The writes run against a real ephemeral PostgreSQL through the
`clean_database` fixture, because everything worth pinning about a
re-queue is the exact column values it leaves behind and the row lock it
takes on the way -- neither survives a fake. These tests are not marked
`slow`; only tests that download a model are.

The permission check is asserted by looking for `_has_admin_access` among
each command's installed checks rather than by driving Discord's check
machinery: `Command.callback` deliberately bypasses checks, so a test that
only ever calls the callback would keep passing if `@require_admin()` were
deleted from a command -- which is the regression that matters most here,
since even `/queue status` reports who was recorded and how much they said.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import discord
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.application.requeue import RequeuePlan
from sturnus.infrastructure.db.models import Base, Session, TranscriptionJob
from sturnus.infrastructure.db.repositories import JobRepository, SessionRepository
from sturnus.infrastructure.discord.permissions import _has_admin_access
from sturnus.infrastructure.discord.queue_cog import (
    NO_SUCH_SESSION,
    QueueCog,
    RequeueConfirmView,
    SessionSummary,
    _apply_requeue,
    render_requeue_confirmation,
)

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
NOW = T0 + timedelta(hours=2)

GUILD, OTHER_GUILD, CHANNEL = 1, 2, 77
ANNA, BEN, CLARA = 100, 200, 300
ADMIN = 999
NAMES = {ANNA: "anna", BEN: "ben", CLARA: "clara"}
DOC_URL = "https://outline.example/doc/session-1"


class _Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


# ---------------------------------------------------------------------------
# Interaction fakes -- the same contract `test_config_commands` enforces.
# ---------------------------------------------------------------------------


class _Message:
    """Just enough of `discord.Message` for a view to disable its buttons."""

    def __init__(self) -> None:
        self.edits: list[discord.ui.View | None] = []

    async def edit(self, view: discord.ui.View | None = None) -> None:
        self.edits.append(view)


class _Response:
    """Discord's initial-response slot: usable exactly once, in one way."""

    def __init__(self) -> None:
        self.deferred = False
        self.deferred_ephemeral: bool | None = None
        self.messages: list[tuple[str, bool]] = []
        self.views: list[discord.ui.View | None] = []

    async def defer(self, ephemeral: bool = False, thinking: bool = False) -> None:
        assert not self.deferred, "an interaction can only be deferred once"
        assert not self.messages, "already answered; there is nothing left to defer"
        assert thinking, "a deferral with no thinking indicator shows the user nothing"
        self.deferred = True
        self.deferred_ephemeral = ephemeral

    async def send_message(
        self, content: str, ephemeral: bool = False, view: discord.ui.View | None = None
    ) -> None:
        assert not self.deferred, "a deferred interaction must answer through followup"
        self.messages.append((content, ephemeral))
        self.views.append(view)


class _Followup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.views: list[discord.ui.View | None] = []

    async def send(
        self, content: str, ephemeral: bool = False, view: discord.ui.View | None = None
    ) -> None:
        self.messages.append((content, ephemeral))
        self.views.append(view)


class _User:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _Interaction:
    """Only the attributes these commands and the confirm view touch."""

    def __init__(self, guild_id: int | None = GUILD, user_id: int = ADMIN) -> None:
        self.guild_id = guild_id
        self.user = _User(user_id)
        self.response = _Response()
        self.followup = _Followup()
        self.message = _Message()

    async def original_response(self) -> _Message:
        return self.message

    @property
    def reply(self) -> str:
        """The single answer the user actually saw, whichever way it went out."""
        answers = self.response.messages + self.followup.messages
        assert len(answers) == 1, f"expected exactly one reply, got {answers}"
        return answers[0][0]

    @property
    def ephemeral(self) -> bool:
        answers = self.response.messages + self.followup.messages
        assert len(answers) == 1
        return answers[0][1]

    @property
    def view(self) -> discord.ui.View | None:
        """The components attached to the single answer, whichever slot sent it."""
        views = self.response.views + self.followup.views
        assert len(views) == 1, f"expected exactly one reply, got {views}"
        return views[0]


def _as_interaction(fake: _Interaction) -> discord.Interaction:
    """The commands are typed against `discord.Interaction`; the fake is not.

    Kept in one place rather than repeated at every call site, exactly as
    `test_config_commands._invoke` keeps its own signature mismatch in one
    place.
    """
    return cast(discord.Interaction, cast(object, fake))


async def _invoke(cog: QueueCog, command: str, interaction: _Interaction, *args: object) -> None:
    """Calls one command's own coroutine, bypassing Discord's dispatch."""
    callback = getattr(cog, command).callback
    await callback(cog, _as_interaction(interaction), *args)


async def _press(view: discord.ui.View, label: str, interaction: _Interaction) -> None:
    """Presses one of the view's buttons the way Discord dispatches it."""
    for item in view.children:
        if isinstance(item, discord.ui.Button) and item.label == label:
            await item.callback(_as_interaction(interaction))
            return
    raise AssertionError(f"no button labelled {label!r} in {view.children}")


# ---------------------------------------------------------------------------
# Database fixtures -- copied from `tests/infrastructure/test_queue.py`.
# ---------------------------------------------------------------------------


@pytest.fixture
async def factory(clean_database: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@dataclass
class Speaker:
    """One seeded job row, in whatever state the test needs it."""

    user_id: int
    status: str = "done"
    transcript: str | None = "old hallucinated text"
    audio_deleted_at: datetime | None = None
    attempts: int = 2
    error: str | None = "something went wrong last time"
    claimed_at: datetime | None = T1


async def seed(
    factory: async_sessionmaker[AsyncSession],
    speakers: list[Speaker],
    *,
    guild: int = GUILD,
    session_status: str = "documented",
    document_url: str | None = DOC_URL,
    announced_at: datetime | None = T1,
) -> int:
    """Builds one finished, documented, announced session and its jobs."""
    sessions = SessionRepository(factory)
    jobs = JobRepository(factory)
    session_id = await sessions.open_session(guild, CHANNEL, "meeting-raum", T0)
    for speaker in speakers:
        await sessions.add_participant(session_id, speaker.user_id, NAMES[speaker.user_id], T0)
        job_id = await jobs.enqueue(
            session_id=session_id,
            discord_user_id=speaker.user_id,
            s3_key=f"sessions/{session_id}/speakers/{speaker.user_id}.enc",
            encryption_key_id="k1",
            wrapped_data_key=b"wrapped",
            retention_until=T0 + timedelta(days=30),
        )
        async with factory() as db:
            await db.execute(
                update(TranscriptionJob)
                .where(TranscriptionJob.id == job_id)
                .values(
                    status=speaker.status,
                    transcript=speaker.transcript,
                    audio_deleted_at=speaker.audio_deleted_at,
                    attempts=speaker.attempts,
                    error=speaker.error,
                    claimed_at=speaker.claimed_at,
                )
            )
            await db.commit()
    await sessions.close_session(session_id, T1, "empty")
    async with factory() as db:
        await db.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(
                status=session_status,
                document_provider="outline",
                document_id="doc-1",
                document_url=document_url,
                announced_at=announced_at,
            )
        )
        await db.commit()
    return session_id


async def read_jobs(
    factory: async_sessionmaker[AsyncSession], session_id: int
) -> dict[int, TranscriptionJob]:
    async with factory() as db:
        rows = await db.execute(
            select(TranscriptionJob).where(TranscriptionJob.session_id == session_id)
        )
        return {job.discord_user_id: job for job in rows.scalars()}


async def read_session(factory: async_sessionmaker[AsyncSession], session_id: int) -> Session:
    async with factory() as db:
        row = await db.get(Session, session_id)
        assert row is not None
        return row


async def set_job_status(
    factory: async_sessionmaker[AsyncSession], session_id: int, user_id: int, status: str
) -> None:
    async with factory() as db:
        await db.execute(
            update(TranscriptionJob)
            .where(
                TranscriptionJob.session_id == session_id,
                TranscriptionJob.discord_user_id == user_id,
            )
            .values(status=status)
        )
        await db.commit()


def cog(factory: async_sessionmaker[AsyncSession], now: datetime = NOW) -> QueueCog:
    return QueueCog(factory, _Clock(now))


# ---------------------------------------------------------------------------
# Permission gate. Every subcommand, including the read-only ones.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["status", "session", "requeue"])
def test_every_subcommand_is_admin_only(command: str) -> None:
    """Even `/queue status` reports who was recorded and how much they said.

    `Command.callback` bypasses checks, so nothing else in this file would
    notice `@require_admin()` disappearing from a command.
    """
    checks = getattr(QueueCog, command).checks
    assert _has_admin_access in checks, f"/queue {command} is not admin-gated"


# ---------------------------------------------------------------------------
# `/queue status`
# ---------------------------------------------------------------------------


async def test_status_counts_only_this_guilds_jobs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An administrator of one guild must not see another guild's queue."""
    await seed(factory, [Speaker(ANNA), Speaker(BEN, status="dead")])
    await seed(
        factory,
        [Speaker(CLARA, status="pending", claimed_at=None)],
        guild=OTHER_GUILD,
    )
    interaction = _Interaction()

    await _invoke(cog(factory), "status", interaction)

    assert "done: 1" in interaction.reply
    assert "dead: 1" in interaction.reply
    assert "pending: 0" in interaction.reply, "the other guild's pending job must not be counted"
    assert interaction.ephemeral is True


async def test_status_reports_a_running_job_past_its_lease(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A worker killed mid-job leaves a `running` row nothing else reports.

    That is the state an administrator is looking for when a session has
    quietly stopped moving, so the count is the reason this subcommand
    exists at all.
    """
    await seed(factory, [Speaker(ANNA, status="running", claimed_at=NOW - timedelta(hours=5))])
    interaction = _Interaction()

    await _invoke(cog(factory), "status", interaction)

    assert "running: 1" in interaction.reply
    assert "1 running job past the default" in interaction.reply


async def test_status_reports_a_closed_session_that_never_got_documented(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The exact condition `retry_pending_documents` sweeps, per guild."""
    await seed(factory, [Speaker(ANNA)], session_status="closed", document_url=None)
    interaction = _Interaction()

    await _invoke(cog(factory), "status", interaction)

    assert "1 closed session" in interaction.reply


async def test_status_dates_the_oldest_pending_job_by_its_sessions_end(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`transcription_job` has no enqueue timestamp, so this is the proxy.

    `RecordingService.close` enqueues every speaker and only then calls
    `close_session`, so `ended_at` is within seconds of when the job was
    created. It is not the same thing, and the reply must not claim it is
    -- a re-queued job keeps its session's original end and would
    otherwise be reported as hours old the instant it was reset.
    """
    await seed(factory, [Speaker(ANNA, status="pending", claimed_at=None)])
    interaction = _Interaction()

    await _invoke(cog(factory), "status", interaction)

    assert "a session that ended 2026-08-19 21:00 UTC" in interaction.reply
    assert "(1h ago)" in interaction.reply
    assert "re-queued job keeps its session's original end time" in interaction.reply


async def test_status_says_plainly_when_nothing_is_waiting(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed(factory, [Speaker(ANNA)])
    interaction = _Interaction()

    await _invoke(cog(factory), "status", interaction)

    assert "Oldest pending job: none" in interaction.reply


# ---------------------------------------------------------------------------
# `/queue session`
# ---------------------------------------------------------------------------


async def test_session_reports_the_transcript_length_and_never_its_text(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 100-minute recording with a 24-character transcript is the tell.

    The length is enough to decide whether a re-queue is warranted; the
    text is meeting content, and a slash command must not become a way to
    read it out of the document system.
    """
    session_id = await seed(factory, [Speaker(ANNA, transcript=" Copyright WDR 2021")])
    interaction = _Interaction()

    await _invoke(cog(factory), "session", interaction, session_id)

    assert "anna" in interaction.reply
    assert "19 characters" in interaction.reply
    assert "Copyright WDR" not in interaction.reply, "the transcript text must never be echoed"


async def test_session_reports_whether_the_audio_still_exists(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed(factory, [Speaker(ANNA), Speaker(BEN, audio_deleted_at=T1)])
    interaction = _Interaction()

    await _invoke(cog(factory), "session", interaction, session_id)

    assert "audio: present" in interaction.reply
    assert "audio: erased" in interaction.reply


async def test_session_from_another_guild_reads_as_not_existing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Identical to the reply for an id that exists nowhere at all.

    A different answer would turn the command into a way to probe whether
    a session id exists in some other server.
    """
    session_id = await seed(factory, [Speaker(ANNA)], guild=OTHER_GUILD)
    seen = _Interaction()
    unseen = _Interaction()

    await _invoke(cog(factory), "session", seen, session_id)
    await _invoke(cog(factory), "session", unseen, 999_999)

    assert seen.reply == NO_SUCH_SESSION
    assert unseen.reply == NO_SUCH_SESSION


async def test_a_command_outside_a_guild_is_refused(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    interaction = _Interaction(guild_id=None)

    await _invoke(cog(factory), "session", interaction, 1)

    assert "only be used in a server" in interaction.reply


# ---------------------------------------------------------------------------
# `/queue requeue` -- refusals, which never reach a confirmation at all.
# ---------------------------------------------------------------------------


async def test_requeue_defers_before_touching_the_database(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reading and locking a session's jobs can exceed Discord's three seconds."""
    session_id = await seed(factory, [Speaker(ANNA)])
    interaction = _Interaction()

    await _invoke(cog(factory), "requeue", interaction, session_id)

    assert interaction.response.deferred is True
    assert interaction.response.deferred_ephemeral is True
    assert interaction.followup.messages, "the answer arrives as a followup"
    assert interaction.ephemeral is True


async def test_requeue_refuses_a_session_with_a_running_job(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The worker holding it would overwrite the reset when it completes."""
    session_id = await seed(factory, [Speaker(ANNA), Speaker(BEN, status="running")])
    interaction = _Interaction()

    await _invoke(cog(factory), "requeue", interaction, session_id)

    assert "Refused" in interaction.reply
    assert "ben" in interaction.reply
    assert interaction.view is None, "a refusal must not offer a Confirm button"
    jobs = await read_jobs(factory, session_id)
    assert jobs[ANNA].status == "done", "nothing may change on a refusal"


async def test_requeue_refuses_when_every_speakers_audio_is_erased(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Making no change while reporting success is the failure to avoid."""
    session_id = await seed(
        factory,
        [Speaker(ANNA, audio_deleted_at=T1), Speaker(BEN, audio_deleted_at=T1)],
    )
    interaction = _Interaction()

    await _invoke(cog(factory), "requeue", interaction, session_id)

    assert "Nothing to do" in interaction.reply
    assert "erased" in interaction.reply
    assert interaction.view is None
    jobs = await read_jobs(factory, session_id)
    assert jobs[ANNA].transcript == "old hallucinated text"


async def test_requeue_refuses_a_session_that_has_no_jobs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed(factory, [])
    interaction = _Interaction()

    await _invoke(cog(factory), "requeue", interaction, session_id)

    assert "Nothing to do" in interaction.reply
    assert interaction.view is None


async def test_requeue_of_another_guilds_session_reads_as_not_existing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed(factory, [Speaker(ANNA)], guild=OTHER_GUILD)
    interaction = _Interaction()

    await _invoke(cog(factory), "requeue", interaction, session_id)

    assert interaction.reply == NO_SUCH_SESSION
    jobs = await read_jobs(factory, session_id)
    assert jobs[ANNA].status == "done"


# ---------------------------------------------------------------------------
# `/queue requeue` -- the confirmation, and what it says before writing.
# ---------------------------------------------------------------------------


async def test_requeue_writes_nothing_until_the_confirmation_is_pressed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The command itself is a question. Only the button is an action."""
    session_id = await seed(factory, [Speaker(ANNA)])
    interaction = _Interaction()

    await _invoke(cog(factory), "requeue", interaction, session_id)

    assert isinstance(interaction.view, RequeueConfirmView)
    jobs = await read_jobs(factory, session_id)
    assert jobs[ANNA].status == "done"
    assert jobs[ANNA].transcript == "old hallucinated text"
    assert (await read_session(factory, session_id)).status == "documented"


async def test_the_confirmation_names_every_consequence_an_admin_cannot_undo(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three of them are irreversible or visible to people who are not admins.

    The stored transcripts are discarded, a second Outline document is
    created and the first is orphaned, and a second link is posted into
    the voice channel where non-admins will see it.
    """
    session_id = await seed(factory, [Speaker(ANNA), Speaker(BEN, audio_deleted_at=T1)])
    interaction = _Interaction()

    await _invoke(cog(factory), "requeue", interaction, session_id)

    reply = interaction.reply
    assert "1 speaker" in reply and "discarded" in reply
    assert "ben" in reply and "erased" in reply and "carried" in reply
    assert DOC_URL in reply and "not updated" in reply
    assert f"<#{CHANNEL}>" in reply


async def test_only_the_invoker_may_press_confirm(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Discord components are clickable by anyone who can see the message."""
    session_id = await seed(factory, [Speaker(ANNA)])
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    intruder = _Interaction(user_id=ADMIN + 1)
    allowed = await view.interaction_check(_as_interaction(intruder))

    assert allowed is False
    assert "Only" in intruder.reply


async def test_a_timeout_disables_the_buttons(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A stale prompt must not still be able to re-queue a session tomorrow."""
    session_id = await seed(factory, [Speaker(ANNA)])
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    await view.on_timeout()

    assert all(item.disabled for item in view.children if isinstance(item, discord.ui.Button))
    assert interaction.message.edits, "the message carrying the buttons must be updated"
    jobs = await read_jobs(factory, session_id)
    assert jobs[ANNA].status == "done"


async def test_cancelling_changes_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed(factory, [Speaker(ANNA)])
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    pressed = _Interaction()
    await _press(view, "Cancel", pressed)

    assert "Cancelled" in pressed.reply
    jobs = await read_jobs(factory, session_id)
    assert jobs[ANNA].status == "done"
    assert jobs[ANNA].transcript == "old hallucinated text"
    assert (await read_session(factory, session_id)).announced_at == T1


# ---------------------------------------------------------------------------
# `/queue requeue` -- the write itself.
# ---------------------------------------------------------------------------


async def test_confirming_resets_every_column_the_redo_depends_on(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`claim` selects only `pending`, so nothing less than this resurrects it.

    `attempts` and `error` describe the old run and `claimed_at` is a
    lease that no longer means anything; leaving any of them would make
    `/queue status` misreport the redo before it has even started.
    """
    session_id = await seed(factory, [Speaker(ANNA)])
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    pressed = _Interaction()
    await _press(view, "Confirm", pressed)

    job = (await read_jobs(factory, session_id))[ANNA]
    assert job.status == "pending"
    assert job.claimed_at is None
    assert job.attempts == 0
    assert job.error is None
    assert job.transcript is None


async def test_confirming_clears_the_transcript_so_a_half_done_redo_cannot_lie(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`assemble` reads every job of the session, not only the last to finish.

    A reset job that kept its old text would put the very hallucinations
    the re-queue exists to remove into the new document if the session
    were re-documented before the redo finished. Clearing it makes a
    half-done redo visibly incomplete instead of plausibly wrong.
    """
    session_id = await seed(factory, [Speaker(ANNA), Speaker(BEN)])
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    await _press(view, "Confirm", _Interaction())

    jobs = await read_jobs(factory, session_id)
    assert jobs[ANNA].transcript is None
    assert jobs[BEN].transcript is None


async def test_confirming_returns_the_session_to_closed_and_not_to_open(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`closed` is exactly the post-`close_session`, pre-documentation state.

    `open` would make `find_open_session` believe this guild has a live
    recording; `closed` is what makes `complete`'s last-job rule
    (`remaining == 0 and session_status == "closed"`) fire a second time,
    and it keeps `candidates_for_announcement` from selecting the session
    while the redo is still running.
    """
    session_id = await seed(factory, [Speaker(ANNA)])
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    await _press(view, "Confirm", _Interaction())

    row = await read_session(factory, session_id)
    assert row.status == "closed"
    assert row.ended_at == T1, "a re-closed session keeps the end it already had"


async def test_confirming_clears_announced_at_so_the_new_link_is_posted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`mark_documented` never touches `announced_at`, and nothing else does.

    Leaving it set produces a session that transcribes, re-documents with
    a fresh URL, and is then never announced -- nothing logged, nothing
    raised, the channel simply never sees the new link. A corrected
    transcript nobody is told about is indistinguishable from no
    transcript, which is the defect being fixed.
    """
    session_id = await seed(factory, [Speaker(ANNA)])
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    await _press(view, "Confirm", _Interaction())

    assert (await read_session(factory, session_id)).announced_at is None


async def test_confirming_leaves_the_old_document_on_the_session_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The next `mark_documented` overwrites them; clearing them early would
    only stop `/queue session` showing which document is being superseded."""
    session_id = await seed(factory, [Speaker(ANNA)])
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    await _press(view, "Confirm", _Interaction())

    row = await read_session(factory, session_id)
    assert row.document_url == DOC_URL
    assert row.document_id == "doc-1"
    assert row.document_provider == "outline"


async def test_confirming_skips_an_erased_speaker_and_keeps_their_transcript(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Their `done` status keeps them terminal for `complete`'s count, and
    their old text is what `assemble` puts in the new document for them."""
    session_id = await seed(factory, [Speaker(ANNA), Speaker(BEN, audio_deleted_at=T1)])
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    pressed = _Interaction()
    await _press(view, "Confirm", pressed)

    jobs = await read_jobs(factory, session_id)
    assert jobs[ANNA].status == "pending"
    assert jobs[BEN].status == "done"
    assert jobs[BEN].transcript == "old hallucinated text"
    assert jobs[BEN].audio_deleted_at == T1
    assert "ben" in pressed.reply and "carried" in pressed.reply


async def test_confirming_reports_how_many_of_how_many_speakers_were_requeued(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed(
        factory, [Speaker(ANNA), Speaker(BEN), Speaker(CLARA, audio_deleted_at=T1)]
    )
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    pressed = _Interaction()
    await _press(view, "Confirm", pressed)

    assert "2 of 3" in pressed.reply


async def test_confirming_touches_no_other_session(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed(factory, [Speaker(ANNA)])
    other = await seed(factory, [Speaker(BEN)])
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    await _press(view, "Confirm", _Interaction())

    assert (await read_jobs(factory, other))[BEN].status == "done"
    assert (await read_session(factory, other)).announced_at == T1


async def test_the_plan_is_recomputed_when_confirm_is_pressed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A worker can claim a sibling job between the question and the answer.

    The confirmation shows a plan built before the button existed; the
    write must not act on it. Building the plan again inside the row lock
    is what stops a re-queue racing a `complete()` that is already in
    flight -- here simulated by a job going `running` while the prompt is
    on screen.
    """
    session_id = await seed(factory, [Speaker(ANNA), Speaker(BEN)])
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    await set_job_status(factory, session_id, BEN, "running")
    pressed = _Interaction()
    await _press(view, "Confirm", pressed)

    assert "Refused" in pressed.reply
    jobs = await read_jobs(factory, session_id)
    assert jobs[ANNA].status == "done", "the stale plan must not have been applied"
    assert jobs[ANNA].transcript == "old hallucinated text"
    assert (await read_session(factory, session_id)).status == "documented"


async def test_pressing_confirm_twice_cannot_reset_the_session_again(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The second press finds the jobs `pending` and refuses on that alone.

    Nothing depends on the buttons being disabled in time: after the first
    write the session no longer qualifies, which is what stops a double
    press turning into a second document and a second public post.
    """
    session_id = await seed(factory, [Speaker(ANNA)])
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    await _press(view, "Confirm", _Interaction())
    second = _Interaction()
    await _press(view, "Confirm", second)

    assert "Refused" in second.reply
    assert (await read_session(factory, session_id)).status == "closed"


async def test_confirming_disables_the_buttons(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    session_id = await seed(factory, [Speaker(ANNA)])
    interaction = _Interaction()
    await _invoke(cog(factory), "requeue", interaction, session_id)
    view = interaction.view
    assert isinstance(view, RequeueConfirmView)

    await _press(view, "Confirm", _Interaction())

    assert all(item.disabled for item in view.children if isinstance(item, discord.ui.Button))


# ---------------------------------------------------------------------------
# `_apply_requeue` on its own. Both of these cover a guarantee no path
# through the cog can reach, which is exactly why they exist: a mutation
# run showed the suite passing in full with either one removed from the
# implementation.
# ---------------------------------------------------------------------------


async def test_the_write_refuses_another_guilds_session_on_its_own(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The second lock on the door, and the one nothing else turns.

    `/queue requeue` already refuses a foreign session before a Confirm
    button ever exists, so no test that goes through the cog can reach
    this check -- which means dropping it from the write would look
    entirely safe. It is not: the view holds a `guild_id` and a
    `session_id` for the length of the prompt, and the guarantee that the
    write applies to a session of *that* guild is the one this command
    must never lose.
    """
    session_id = await seed(factory, [Speaker(ANNA)], guild=OTHER_GUILD)

    assert await _apply_requeue(factory, GUILD, session_id) is None
    assert (await read_jobs(factory, session_id))[ANNA].status == "done"


async def test_the_write_waits_for_a_worker_holding_the_sessions_jobs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The plan must be built inside the row lock, not merely before the write.

    This drives the interleaving `JobQueue.complete` really produces: a
    worker takes `SELECT ... FOR UPDATE` over every job of the session,
    flips one of them, and only then commits. Under READ COMMITTED a
    re-queue that read the job rows *without* first taking that same lock
    would see the pre-commit snapshot -- every job still `done` -- decide
    the whole session is resettable, and then block on the UPDATE instead;
    when the worker's transaction landed, the reset would go through and
    overwrite a job that is `running`, which is precisely the state the
    refusal exists to protect.

    Taking the lock first turns that into a wait: the plan is not built
    until the worker has committed, so it sees the `running` job and
    refuses. The lock statement here is character-for-character the one
    `JobQueue.complete` takes, `ORDER BY id` included, because matching
    lock-acquisition order is what keeps the two from deadlocking.
    """
    session_id = await seed(factory, [Speaker(ANNA), Speaker(BEN)])

    async with factory() as worker:
        await worker.execute(
            select(TranscriptionJob.id)
            .where(TranscriptionJob.session_id == session_id)
            .order_by(TranscriptionJob.id)
            .with_for_update()
        )
        await worker.execute(
            update(TranscriptionJob)
            .where(
                TranscriptionJob.session_id == session_id,
                TranscriptionJob.discord_user_id == BEN,
            )
            .values(status="running")
        )
        task = asyncio.create_task(_apply_requeue(factory, GUILD, session_id))
        # Long enough for the task to reach the database and stop there.
        await asyncio.sleep(0.3)
        assert not task.done(), "the re-queue must wait on the lock, not race it"
        await worker.commit()

    view = await asyncio.wait_for(task, timeout=10)

    assert view is not None
    assert view.plan.is_blocked, "the plan was built from a snapshot taken before the commit"
    jobs = await read_jobs(factory, session_id)
    assert jobs[BEN].status == "running"
    assert jobs[ANNA].status == "done"
    assert jobs[ANNA].transcript == "old hallucinated text"
    assert (await read_session(factory, session_id)).status == "documented"


# ---------------------------------------------------------------------------
# Reply rendering, without an `Interaction` at all.
# ---------------------------------------------------------------------------


def _summary(**overrides: Any) -> SessionSummary:
    defaults: dict[str, Any] = {
        "id": 4,
        "channel_id": CHANNEL,
        "status": "documented",
        "ended_at": T1,
        "end_reason": "empty",
        "document_url": DOC_URL,
        "announced_at": T1,
    }
    return SessionSummary(**{**defaults, **overrides})


def test_a_confirmation_for_a_session_that_was_never_documented_says_so() -> None:
    """There is no first document to orphan, so promising one would be wrong."""
    text = render_requeue_confirmation(
        _summary(status="closed", document_url=None, announced_at=None),
        RequeuePlan(resettable_job_ids=(1,), erased_user_ids=(), active_user_ids=()),
        names={},
    )

    assert "no document" in text
    assert "not updated" not in text
