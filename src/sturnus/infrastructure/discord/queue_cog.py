"""Admin commands for looking at the transcription queue and re-running a session.

Three subcommands, and no more. `/queue status` and `/queue session` are
read-only; `/queue requeue` is the only one that writes, and it is the
reason this cog exists: a session transcribed by a code path that has since
been fixed is worthless until something puts its jobs back on the queue,
and `JobQueue.claim` selects only `pending` jobs -- never `done`, never
`dead` -- so nothing short of an explicit write resurrects one.

**What a re-queue is.** A state reset and nothing else. It returns the
session to exactly the state it was in immediately after `close_session`
and before documentation, so that `claim`, `complete`,
`sturnus.application.worker._create_session_document` and
`sturnus.application.publishing.announce_ready_sessions` carry it forward a
second time on their own. Nothing here orchestrates the redo. Which jobs
may be reset is decided by `sturnus.application.requeue.plan_requeue`, a
pure function tested without a database; read its module docstring first,
because two of the three rules that keep this command from being
destructive live there rather than here. The third is `SessionView.
is_settled`: only a `documented` session may be re-queued at all, because
that is the one status in which nothing else in the pipeline is still
working on the session -- a rule about the session row rather than about
its jobs, which is why it lives here beside the write and not in that
pure function over job rows.

**Why the SQL is inline instead of in a repository.** This selection is
specific to these three commands and used nowhere else, so
`SessionRepository` and `JobQueue` do not grow a method that only an admin
slash command calls. `sturnus.infrastructure.discord.audio_cog._erase_audio`
does the same thing for the same stated reason, and this module follows it
deliberately.

**Everything is scoped to `interaction.guild_id`.** Every query joins
`transcription_job` to `session` and filters on `session.guild_id`, and a
session id belonging to another guild produces the *same* reply as one that
does not exist anywhere, so the command cannot be used to probe whether an
id exists elsewhere. Note the contrast with `audio_cog._erase_audio`, which
is deliberately cross-guild: that is a GDPR erasure serving a data subject
across the whole deployment, and a status readout is not.

**Every reply is ephemeral, and every subcommand is admin-gated** --
including the read-only ones. Even `/queue status` reports who was recorded
and how much they said, which is not an ordinary-member fact. The one
public thing a re-queue produces is the announcement
`announce_ready_sessions` posts once the redo is documented, and the
confirmation says so before anything is written, because that post is the
part of a re-queue an ordinary member sees.

**The confirmation is a plan, not a yes/no prompt.** `/queue requeue` does
three things that are irreversible or publicly visible: it discards stored
transcripts that cannot be recovered if the redo fails; it causes a second
Outline document to be created, leaving the first orphaned (there is no
update path anywhere in the codebase -- `DocumentSink` exposes only
`create`); and it causes a second announcement in the voice channel. All
three are named in the confirmation text before the button is pressed. A
`force: bool = False` flag would be typed from muscle memory and presents
no plan to read, so this uses buttons instead.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sturnus.application.ports import Clock
from sturnus.application.publishing import DOCUMENTED_STATUS
from sturnus.application.requeue import RequeuePlan
from sturnus.infrastructure.db.queue import DEFAULT_LEASE_SECONDS
from sturnus.infrastructure.db.requeue import (
    REPORTED_STATUSES,
    JobLine,
    QueueStatus,
    SessionSummary,
    apply_requeue,
    load_requeue_view,
    load_session,
    load_status,
)
from sturnus.infrastructure.discord.permissions import require_admin
from sturnus.observability.events import Event, log_exception

log = logging.getLogger(__name__)

#: The single answer for "that session is not yours to look at", used both
#: for an id that exists in another guild and for one that exists nowhere.
#: Identical on purpose: two different replies would turn this command into
#: a way to discover whether a session id is in use somewhere else.
NO_SUCH_SESSION = "No session with that id in this server."

#: How long the Confirm/Cancel prompt stays live. Short, because the plan
#: it displays is a snapshot: a worker can claim a sibling job while it
#: sits on screen. The write re-derives the plan under a row lock anyway
#: (`apply_requeue`), so a stale press is refused rather than obeyed --
#: this timeout only keeps a forgotten prompt from lingering.
CONFIRM_TIMEOUT_SECONDS = 60.0

#: The longest message body Discord accepts. Not advisory: `followup.send`
#: raises `HTTPException` on anything longer, and every reply this cog
#: sends goes out *after* a `thinking=True` defer -- so an over-long reply
#: is not a truncated answer, it is no answer at all, on commands whose
#: whole job is to explain a queue that has gone wrong. Every render
#: function here therefore ends inside `_capped`.
DISCORD_MESSAGE_LIMIT = 2000

#: How much of one job's stored `error` a `/queue session` line may carry.
#: `transcription_job.error` is `str(exc)` -- arbitrary text of arbitrary
#: length, never truncated on the way in -- and one such string is easily
#: longer than the whole message budget. Enough to recognise a failure
#: ("An error occurred (AccessDenied) when calling the GetObject
#: operation..."), and `docs/operations.md` section 5 says where to read
#: the untruncated row.
MAX_ERROR_CHARS = 160


def _stamp(when: datetime | None) -> str:
    if when is None:
        return "never"
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _age(delta: timedelta) -> str:
    """A duration a human reads at a glance, e.g. `2d 3h` or `14m`."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "under a minute"
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def _named(user_ids: tuple[int, ...], names: dict[int, str]) -> str:
    """Display names for a plan's speakers, falling back to the raw id.

    Never a `<@id>` mention: these replies list people who were recorded
    and who may have asked for their audio to be erased, and a mention
    would ping them into a conversation they are not part of.
    """
    return ", ".join(names.get(user_id, f"user {user_id}") for user_id in user_ids)


def _speakers(count: int) -> str:
    return "1 speaker" if count == 1 else f"{count} speakers"


def _one_line(text: str) -> str:
    """Collapses every run of whitespace, so one value stays one line.

    `job.error` can carry newlines -- a wrapped traceback, an XML error
    body -- and the `/queue session` readout is a bullet per speaker,
    read as such. Left alone, one error would break its speaker into a
    dozen lines that look like speakers of their own, and each of those
    newlines would also spend budget that belongs to a speaker who then
    does not get shown.
    """
    return " ".join(text.split())


def _shortened(text: str, limit: int) -> str:
    """`text` cut to `limit` characters, ending in an ellipsis when it was cut."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _capped(text: str) -> str:
    """The last line of defence: never hand Discord a body it will reject.

    The render functions bound themselves where the length actually comes
    from -- errors, speaker lists -- and that structured bound is what
    keeps a truncated reply *readable*. This is the crude backstop under
    it, for the lengths nobody budgeted: a session document URL a
    self-hosted Outline made 900 characters long, a display name that is
    all combining marks. A reply cut off mid-sentence is a poor answer;
    an `HTTPException` behind a `thinking=True` defer is no answer at all,
    and this cog is what an administrator reaches for when they already
    cannot see what is happening.
    """
    if len(text) <= DISCORD_MESSAGE_LIMIT:
        return text
    marker = f"\n… (cut off at Discord's {DISCORD_MESSAGE_LIMIT}-character limit)"
    return text[: DISCORD_MESSAGE_LIMIT - len(marker)] + marker


def _running_jobs(count: int) -> str:
    return "1 running job" if count == 1 else f"{count} running jobs"


def _closed_sessions(count: int) -> str:
    return "1 closed session" if count == 1 else f"{count} closed sessions"


def render_status(status: QueueStatus, now: datetime, lease_seconds: float) -> str:
    counts = ", ".join(f"{name}: {status.counts.get(name, 0)}" for name in REPORTED_STATUSES)
    lines = ["**Transcription queue for this server**", f"Jobs — {counts}"]
    if status.running_past_lease:
        # Stated as a possibility rather than a fact: the lease that
        # actually applies is `job_lease_seconds` in the *worker's*
        # environment, and this process cannot see it. A long-running job
        # under a raised lease is perfectly healthy and must not be
        # reported as abandoned.
        lines.append(
            f"⚠️ {_running_jobs(status.running_past_lease)} past the default "
            f"{int(lease_seconds)}s lease — if the worker's `job_lease_seconds` is not "
            "higher than that, another worker may already have reclaimed the job."
        )
    if status.oldest_pending_session_ended_at is None:
        lines.append("Oldest pending job: none — nothing is waiting.")
    else:
        ended = status.oldest_pending_session_ended_at
        lines.append(
            f"Oldest pending job: from a session that ended {_stamp(ended)} "
            f"({_age(now - ended)} ago). A re-queued job keeps its session's original "
            "end time, so this reads older than the job itself after a `/queue requeue`."
        )
    if status.closed_undocumented:
        lines.append(
            f"{_closed_sessions(status.closed_undocumented)} with every job finished but "
            "no document yet; the worker retries those on its own sweep."
        )
    return _capped("\n".join(lines))


def _speaker_line(job: JobLine, names: dict[int, str]) -> str:
    """One speaker's bullet, bounded so no single job can eat the reply."""
    name = names.get(job.discord_user_id, f"user {job.discord_user_id}")
    length = (
        "transcript: none stored"
        if job.transcript_length is None
        else f"transcript: {job.transcript_length} characters"
    )
    audio = "audio: present" if job.audio_present else "audio: erased"
    line = f"- {name} — status: `{job.status}`, attempts: {job.attempts}, {audio}, {length}"
    if job.error:
        line += f", last error: {_shortened(_one_line(job.error), MAX_ERROR_CHARS)}"
    return line


def _omitted(count: int) -> str:
    noun = "speaker" if count == 1 else "speakers"
    return (
        f"…and {count} more {noun} not shown — the full readout is longer than the "
        f"{DISCORD_MESSAGE_LIMIT} characters Discord allows in one message. Query "
        "`transcription_job` directly for the rest (docs/operations.md, section 5)."
    )


def _fitted(header: list[str], speakers: list[str]) -> list[str]:
    """`header` plus as many speaker lines as fit, then a line saying how many did not.

    Truncation rather than an attached file, and the choice is about what
    a truncated answer is *for*. This readout is scanned for one thing --
    a speaker whose transcript length is absurd for the length of the
    session -- and the first speakers are as good a sample of that as any;
    an attachment would answer completely but reaches the administrator as
    a download to open, on a command whose value is that the answer is on
    screen in a second. Nothing is lost silently either way: the omitted
    count is stated, and section 5 of `docs/operations.md` already
    documents the SQL for the rows behind this command.

    Dropping the *tail* rather than the middle keeps the list in job-id
    order, which is the order every other `/queue` reply and the document
    itself use.
    """
    kept: list[str] = []
    # `+ 1` per line for the newline `join` will add. One more than it
    # really needs, which is the safe direction to be wrong in.
    used = sum(len(line) + 1 for line in header)
    for index, line in enumerate(speakers):
        omitted = _omitted(len(speakers) - index)
        if used + len(line) + 1 + len(omitted) + 1 > DISCORD_MESSAGE_LIMIT:
            return [*header, *kept, omitted]
        used += len(line) + 1
        kept.append(line)
    return [*header, *kept]


def render_session(summary: SessionSummary, jobs: list[JobLine], names: dict[int, str]) -> str:
    header = [
        f"**Session {summary.id}** in <#{summary.channel_id}>",
        f"Status: `{summary.status}` — ended {_stamp(summary.ended_at)}"
        f" ({summary.end_reason or 'no reason recorded'})",
        f"Document: {summary.document_url or '*(none)*'}",
        f"Announced: {_stamp(summary.announced_at)}",
    ]
    if not jobs:
        header.append("No transcription jobs — nobody spoke in this session.")
        return _capped("\n".join(header))
    header.append(f"{_speakers(len(jobs))}:")
    speakers = [_speaker_line(job, names) for job in jobs]
    return _capped("\n".join(_fitted(header, speakers)))


def _document_line(summary: SessionSummary, *, future: bool) -> str:
    """What happens to the document, stated before and after the write alike.

    `DocumentSink` has exactly one method, `create` -- no update, no patch,
    nowhere in the codebase -- so a redo that reaches
    `_create_session_document` always makes a brand-new Outline document
    and `mark_documented` overwrites the session's `document_url`. The
    previously published page stays in Outline, unlinked and unmentioned.
    That is accepted and chosen, but an administrator who is not told about
    it cannot make the cleanup a deliberate act, so it is spelled out here
    both in the confirmation and in the result.
    """
    verb = "will be created" if future else "is created when the redo finishes"
    if summary.document_url is None:
        return f"- A new document {verb}; this session has no document yet, so none is superseded."
    return (
        f"- A **new** document {verb}. The existing one (<{summary.document_url}>) "
        "stays where it is and is not updated or deleted."
    )


def render_requeue_confirmation(
    summary: SessionSummary, plan: RequeuePlan, names: dict[int, str]
) -> str:
    """The prompt shown above the Confirm/Cancel buttons.

    Deliberately the plan rather than a yes/no question: everything the
    administrator cannot undo afterwards is on screen before they press
    anything.
    """
    lines = [
        f"Re-queue session {summary.id} (<#{summary.channel_id}>, "
        f"ended {_stamp(summary.ended_at)})?",
        f"- {_speakers(len(plan.resettable_job_ids))} will be re-transcribed; their "
        "stored transcripts are discarded and cannot be recovered if the redo fails.",
    ]
    if plan.erased_user_ids:
        lines.append(
            f"- {_speakers(len(plan.erased_user_ids))} skipped because their audio has "
            f"been erased ({_named(plan.erased_user_ids, names)}). Their existing "
            "transcript is carried into the new document unchanged."
        )
    lines.append(_document_line(summary, future=True))
    lines.append(
        f"- A new link is posted in <#{summary.channel_id}> once it is ready. Everyone "
        "who can see that channel sees the post, not only administrators."
    )
    return _capped("\n".join(lines))


def render_requeue_applied(
    summary: SessionSummary, plan: RequeuePlan, names: dict[int, str]
) -> str:
    total = len(plan.resettable_job_ids) + len(plan.erased_user_ids)
    lines = [
        f"Re-queued {len(plan.resettable_job_ids)} of {_speakers(total)} in session "
        f"{summary.id}. Their stored transcripts have been discarded; a worker picks "
        "the jobs up on its next poll.",
    ]
    if plan.erased_user_ids:
        lines.append(
            f"- Skipped: {_named(plan.erased_user_ids, names)} — their audio has been "
            "erased, so their existing transcript is carried into the new document "
            "unchanged rather than re-transcribed."
        )
    lines.append(_document_line(summary, future=False))
    lines.append(f"- A new link is posted in <#{summary.channel_id}> when it is ready.")
    return _capped("\n".join(lines))


def _unsettled_refusal(summary: SessionSummary) -> str:
    """Why a session that is not `documented` cannot be re-queued yet.

    One sentence per status about what is still holding the session, then
    the same instruction in both cases: wait for `documented`. The
    reasoning behind each is on `SessionView.is_settled`; what an
    administrator needs from the reply is that the pipeline is not
    finished with this session and that waiting is the whole remedy --
    said plainly enough that the obvious next move is not to try again
    immediately.
    """
    if summary.status == "open":
        return (
            f"Refused: session {summary.id} is still open — the recording has not "
            "finished, or the bot is still uploading the speakers it recorded. A "
            "re-queue would close the session while speakers are still being added to "
            "it, and the first job to finish afterwards would be taken for the "
            "session's last: the document would then be built from part of the "
            "meeting. Stop the recording and wait until `/queue session "
            f"{summary.id}` reports `documented`."
        )
    if summary.status == "closed":
        return (
            f"Refused: session {summary.id} has finished transcribing but has no "
            "document yet. The worker's own retry sweep still owns it and creates "
            "that document on its next pass; a re-queue landing in the middle of the "
            "sweep can leave the session documented from the transcripts this command "
            "has just discarded, and nothing revisits it afterwards. Wait until "
            f"`/queue session {summary.id}` reports `documented` and re-queue then — "
            "if it never gets there, that is a different fault, and `/queue status` "
            "counts the sessions stuck in it."
        )
    return (
        f"Refused: session {summary.id} is `{summary.status}`, and only a "
        "`documented` session can be re-queued — that is the one state in which "
        "nothing else in the pipeline is still working on it."
    )


def render_requeue_refusal(
    summary: SessionSummary, plan: RequeuePlan, names: dict[int, str], *, rechecked: bool = False
) -> str:
    """Why nothing was written. `rechecked` means the session moved under us.

    Split from the other two because a refusal is not a smaller success:
    `config_cog` names four honest outcomes rather than one cheerful
    confirmation, and this follows that.

    The branches are checked in `SessionView.is_refused`'s order and must
    stay in step with it, or the reply would explain a reason other than
    the one the write actually refused on.
    """
    prefix = (
        "The session changed while the confirmation was on screen, so nothing was written. "
        if rechecked
        else ""
    )
    return _capped(f"{prefix}{_refusal(summary, plan, names)}")


def _refusal(summary: SessionSummary, plan: RequeuePlan, names: dict[int, str]) -> str:
    """The reason itself, in `SessionView.is_refused`'s order of checks."""
    if plan.is_blocked:
        return (
            f"Refused: {_speakers(len(plan.active_user_ids))} in session "
            f"{summary.id} still have a job pending or running "
            f"({_named(plan.active_user_ids, names)}). Those recordings are already "
            "going to be transcribed, and resetting a job a worker is holding would be "
            "undone the moment that worker finishes. Try again once the queue is idle."
        )
    if summary.status != DOCUMENTED_STATUS:
        return _unsettled_refusal(summary)
    if plan.erased_user_ids:
        return (
            f"Nothing to do: every speaker in session {summary.id} has had "
            f"their audio erased ({_named(plan.erased_user_ids, names)}). Re-queueing "
            "would only hand a worker a key it cannot download. Their existing "
            "transcripts are untouched."
        )
    return (
        f"Nothing to do: session {summary.id} has no transcription jobs at all "
        "— nobody spoke, so there is nothing to transcribe again."
    )


# ---------------------------------------------------------------------------
# The confirmation view.
# ---------------------------------------------------------------------------


class RequeueConfirmView(discord.ui.View):
    """Confirm / Cancel for one session's re-queue.

    Author-only (`interaction_check`), because Discord components are
    clickable by anyone who can see the message; short-lived, and the
    buttons disable themselves on timeout so a stale prompt cannot
    re-queue a session tomorrow. The Discord rendering rules here are the
    ones `sturnus.infrastructure.discord.views.ConsentView` already
    established, which is why they look the same.

    The view holds ids, never a plan. Pressing Confirm re-derives the plan
    inside the row lock (`apply_requeue`), so the write can never be made
    from the snapshot the prompt was rendered from -- and a second press,
    which nothing stops Discord from delivering, finds the jobs already
    `pending` and is refused on that alone rather than producing a second
    document and a second public post.
    """

    def __init__(
        self,
        *,
        author_id: int,
        guild_id: int,
        session_id: int,
        session_factory: async_sessionmaker[AsyncSession],
        timeout: float = CONFIRM_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._author_id = author_id
        self._guild_id = guild_id
        self._session_id = session_id
        self._session_factory = session_factory
        #: Set by the cog right after sending, so the buttons can be
        #: disabled on the message that actually carries them.
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                f"Only <@{self._author_id}> can respond to this prompt.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        await self._disable()

    async def _disable(self) -> None:
        """Greys out both buttons on the message they are attached to.

        A failed edit is logged and swallowed: the answer the
        administrator is waiting for must not be lost because Discord
        would not repaint a message, and a live button on a stopped view
        is refused by `apply_requeue`'s own re-check anyway.
        """
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException as exc:
            log_exception(
                log,
                logging.WARNING,
                Event.QUEUE_VIEW_DISABLE_FAILED,
                "Could not grey out the /queue requeue buttons; a live button on a "
                "stopped view is refused by the re-check anyway",
                exc,
                guild_id=self._guild_id,
                session_id=self._session_id,
            )

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button[RequeueConfirmView]
    ) -> None:
        # The write takes a row lock and can wait on a worker that is
        # mid-`complete()`, which is easily more than the three seconds
        # Discord gives a component interaction to answer.
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.stop()
        await self._disable()
        view = await apply_requeue(self._session_factory, self._guild_id, self._session_id)
        if view is None:
            await interaction.followup.send(NO_SUCH_SESSION, ephemeral=True)
            return
        if view.is_refused:
            await interaction.followup.send(
                render_requeue_refusal(view.summary, view.plan, view.names, rechecked=True),
                ephemeral=True,
            )
            return
        log.info(
            "Re-queued %d job(s) of session %d in guild %d (requested by %d)",
            len(view.plan.resettable_job_ids),
            self._session_id,
            self._guild_id,
            interaction.user.id,
        )
        await interaction.followup.send(
            render_requeue_applied(view.summary, view.plan, view.names), ephemeral=True
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button[RequeueConfirmView]
    ) -> None:
        self.stop()
        await self._disable()
        await interaction.response.send_message(
            f"Cancelled. Session {self._session_id} is untouched.", ephemeral=True
        )


# ---------------------------------------------------------------------------
# The cog.
# ---------------------------------------------------------------------------


@app_commands.guild_only()
class QueueCog(
    commands.GroupCog, name="queue", description="Inspect the transcription queue (admin only)."
):
    """`/queue` command group: two read-only views and one re-queue."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        #: Only ever used to *describe* a running job as possibly abandoned.
        #: The lease that actually applies is `job_lease_seconds` in the
        #: worker's environment, which this process cannot read -- so this
        #: is the documented default, and `render_status` says so rather
        #: than presenting the count as a fact.
        self._lease_seconds = lease_seconds
        super().__init__()

    @app_commands.command(name="status", description="Counts of queued, running and failed jobs.")
    @require_admin()
    async def status(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        # Four aggregates over two joined tables; on a cold connection
        # pool that alone can miss Discord's three-second initial-response
        # window, and then the administrator sees "The application did not
        # respond" over a command that worked.
        await interaction.response.defer(ephemeral=True, thinking=True)
        now = self._clock.now()
        status = await load_status(self._session_factory, guild_id, now, self._lease_seconds)
        await interaction.followup.send(
            render_status(status, now, self._lease_seconds), ephemeral=True
        )

    @app_commands.command(name="session", description="One session's jobs, in detail.")
    @app_commands.describe(session_id="The session's numeric id, as shown by /queue status")
    @require_admin()
    async def session(self, interaction: discord.Interaction, session_id: int) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        loaded = await load_session(self._session_factory, guild_id, session_id)
        if loaded is None:
            await interaction.followup.send(NO_SUCH_SESSION, ephemeral=True)
            return
        summary, jobs, names = loaded
        await interaction.followup.send(render_session(summary, jobs, names), ephemeral=True)

    @app_commands.command(
        name="requeue", description="Transcribe a finished session again, from its stored audio."
    )
    @app_commands.describe(session_id="The session's numeric id, as shown by /queue status")
    @require_admin()
    async def requeue(self, interaction: discord.Interaction, session_id: int) -> None:
        """Offers a re-queue; the write happens only if Confirm is pressed.

        This half deliberately writes nothing. It reads the session, builds
        the plan and renders it, and every consequence that cannot be
        undone is named in that text before a button exists to press.
        """
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        view = await load_requeue_view(self._session_factory, guild_id, session_id)
        if view is None:
            await interaction.followup.send(NO_SUCH_SESSION, ephemeral=True)
            return
        if view.is_refused:
            # No buttons at all: there is nothing to confirm, and offering
            # a Confirm that would only be refused invites the
            # administrator to press it and learn nothing new.
            await interaction.followup.send(
                render_requeue_refusal(view.summary, view.plan, view.names), ephemeral=True
            )
            return
        confirm = RequeueConfirmView(
            author_id=interaction.user.id,
            guild_id=guild_id,
            session_id=session_id,
            session_factory=self._session_factory,
        )
        await interaction.followup.send(
            render_requeue_confirmation(view.summary, view.plan, view.names),
            view=confirm,
            ephemeral=True,
        )
        confirm.message = await interaction.original_response()
