"""Link publishing selection and orchestration (Spec 8.5).

The worker marks a session `documented` once its protocol exists in the
document system and records the document's URL on the session row. The bot
then polls, on `publish_poll_seconds`, for sessions still waiting to have
their link posted into the channel; `sessions_to_announce` is the pure
selection behind that poll, given whatever a caller already read from the
database.

`announce_ready_sessions` is the periodic poll that actually calls it: it
reads candidates through `SessionReader`, filters them with
`sessions_to_announce`, then -- for each -- renders the announcement text
and posts it through `Announcer`, stamping `announced_at` only once the
post has actually gone out. `SessionReader`/`Announcer` are narrow local
`Protocol`s rather than concrete types, the same pattern
`sturnus.application.worker` uses for its own collaborators -- this module
lives in `sturnus.application`, which must never import
`sturnus.infrastructure` (tests/test_architecture.py); the concrete
adapters (`sturnus.infrastructure.db.repositories.SessionRepository`, the
Discord gateway) are wired in by `sturnus.entrypoints.bot`.

`announced_at` is what protects against a restart re-posting every link the
bot ever published: once it is set, a session is never selected again,
regardless of how many times the poll runs afterwards.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, cast

from jinja2.sandbox import SandboxedEnvironment

from sturnus.application.sharding import process_serves_guild
from sturnus.observability.events import Event, log_event, log_exception

log = logging.getLogger(__name__)

#: The session `status` value the worker writes once a session's protocol
#: has been created in the document system (Spec 8.5). A plain string, like
#: the "open"/"closed" values `SessionRepository` already writes for this
#: same column -- there is no domain enum for it to reuse.
DOCUMENTED_STATUS = "documented"


def sessions_to_announce(sessions: list[dict[str, object]]) -> list[dict[str, object]]:
    """Selects documented sessions whose link has not been posted yet.

    A session qualifies only once it is `documented`, still has
    `announced_at` unset, and actually carries a `document_url` to post --
    the last check is defensive: a `documented` session with no URL yet has
    nothing to post regardless of what its status claims.
    """
    selected: list[dict[str, object]] = []
    for candidate in sessions:
        status = cast(str, candidate["status"])
        document_url = cast("str | None", candidate["document_url"])
        announced_at = cast("datetime | None", candidate["announced_at"])
        if status == DOCUMENTED_STATUS and announced_at is None and document_url is not None:
            selected.append(candidate)
    return selected


#: Default wording for the announcement (Spec 8.5). Rendered through the
#: same sandboxed-Jinja2 pattern `sturnus.application.documents.
#: render_transcript` already uses for documents -- Spec 8.2 calls for one
#: engine rendering every Discord message this system posts, the recording
#: announcement and the completion message with the document link alike --
#: rather than a hardcoded f-string, so the wording can be adjusted later
#: without a code change once a per-guild template exists.
#:
#: The mentions come first, on their own line: a protocol is written for
#: the people who were in the room, and a link posted into a busy channel
#: without addressing anyone scrolls past unread -- the same reasoning
#: `SILENT_AUDIO_WARNING_TEMPLATE` below already applies to naming one
#: speaker. `{% if %}` rather than an unconditional `{{ mentions }}`, so a
#: session whose participants are somehow unknown posts the link by itself
#: instead of a stray blank first line.
DEFAULT_ANNOUNCEMENT_TEMPLATE = (
    "{% if mentions %}{{ mentions }}\n{% endif %}"
    "The transcript for this session is ready: {{ document_url }}"
)


def _build_environment() -> SandboxedEnvironment:
    # Built directly from Jinja2 here rather than reusing
    # `sturnus.infrastructure.templates.engine.build_environment`: this
    # module lives in `sturnus.application`, which must never import
    # `sturnus.infrastructure` (see the module docstring) -- the same
    # reasoning `sturnus.application.documents._build_environment` follows
    # for the identical reason.
    return SandboxedEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True)


def format_mentions(discord_user_ids: Sequence[int]) -> str:
    """Renders Discord's `<@id>` mention syntax for a session's participants.

    Each id is passed through `int()` first. The ids come from a
    `BigInteger` column and are already integers, so this converts
    nothing in practice -- it exists because this environment does not
    autoescape and the result is posted verbatim into a channel: an id
    that somehow arrived as a string would otherwise be able to carry
    `@everyone` into the message. `int()` makes that unrepresentable
    rather than merely unlikely.
    """
    return " ".join(f"<@{int(discord_user_id)}>" for discord_user_id in discord_user_ids)


def render_announcement(
    document_url: str,
    participant_ids: Sequence[int] = (),
    template_source: str = DEFAULT_ANNOUNCEMENT_TEMPLATE,
) -> str:
    """Renders the Discord announcement text for one finished session's link.

    `participant_ids` defaults to empty so a caller that has no
    participant list still renders a valid announcement -- the link is
    the message; the mentions only decide who is told about it.
    """
    template = _build_environment().from_string(template_source)
    return template.render(
        document_url=document_url,
        participant_ids=tuple(participant_ids),
        mentions=format_mentions(participant_ids),
    )


#: Wording for the in-meeting warning that a speaker's audio is arriving
#: with nothing audible in it (`sturnus.domain.silence`). Posted publicly
#: into the recording channel and naming the person, which is a deliberate
#: choice over a direct message: whoever is muted at system level is
#: usually the last to notice, and somebody else in the room can help.
#:
#: Every word of it is load-bearing. It states what was observed rather
#: than what somebody did wrong, it names the one cause that explains
#: audio arriving at zero level, and it says the recording continues --
#: without that last sentence the message reads as "you are not being
#: recorded", which is false and would send people out of the meeting to
#: fix something that is not broken.
#:
#: Rendered through the same sandboxed engine as the announcement above,
#: for the same reason (Spec 8.2: one engine for every Discord message
#: this system posts), so the wording can move to a per-guild template
#: later without a code change.
SILENT_AUDIO_WARNING_TEMPLATE = (
    "Audio is arriving from <@{{ discord_user_id }}> but at no audible level. "
    "The microphone is most likely muted at system level. Recording continues."
)


def render_silent_audio_warning(
    discord_user_id: int, template_source: str = SILENT_AUDIO_WARNING_TEMPLATE
) -> str:
    """Renders the public warning for one speaker whose audio carries no level.

    `<@id>` is Discord's mention syntax; the id is an integer taken from
    the voice packet itself, so nothing user-controlled reaches the
    template even though this environment does not autoescape.
    """
    template = _build_environment().from_string(template_source)
    return template.render(discord_user_id=discord_user_id)


class SessionReader(Protocol):
    """Where announcement candidates are read from and `announced_at` is stamped."""

    async def candidates_for_announcement(self) -> list[dict[str, object]]:
        """Every `documented` session, shaped for `sessions_to_announce`.

        Deliberately unfiltered by `announced_at`/`document_url`: those
        checks are `sessions_to_announce`'s job alone, so there is exactly
        one definition of the selection rule anywhere in the codebase.

        Each row carries a `participant_ids` key -- the Discord ids of
        everyone recorded in that session, so the announcement can
        mention them. An implementation that omits the key still works:
        `announce_ready_sessions` falls back to no mentions rather than
        failing to post the link.

        Each row also carries `guild_id`, which is not used to render
        anything: it is what lets `announce_ready_sessions` ask whether
        this process serves that guild at all. This is the one sweep in
        the bot whose input is the database rather than a gateway cache,
        so it is the one that has to ask -- see
        `sturnus.application.sharding`.
        """
        ...

    async def mark_announced(self, session_id: int, now: datetime) -> None: ...


class Announcer(Protocol):
    """Where the rendered announcement text is actually posted."""

    async def post(self, channel_id: int, text: str) -> None: ...


async def announce_ready_sessions(
    sessions: SessionReader,
    announcer: Announcer,
    now: datetime,
    template_source: str = DEFAULT_ANNOUNCEMENT_TEMPLATE,
    *,
    shard_count: int | None = None,
) -> None:
    """Posts each documented session's link once and stamps `announced_at`.

    Survives its own errors per session: a failure posting or stamping one
    session's link is logged and does not stop the sweep from handling the
    rest -- one unreachable channel must not block every other session's
    announcement.

    `announced_at` is stamped only once `announcer.post` has actually
    returned, so a failed post is retried on the next sweep instead of
    being silently lost -- losing an announcement entirely is worse than
    an occasional duplicate. The accepted cost of that choice is a
    possible duplicate post if the post itself succeeds but the following
    stamp fails (a narrow window between two calls in the same process);
    the alternative -- stamping first -- would instead risk never posting
    at all whenever the post itself is what fails, which is the more
    common failure mode (a flaky Discord API call) and the one this
    function exists to survive.

    **The one sweep in the bot that asks whether it serves a guild.**
    Everything else the bot sweeps periodically is driven off its own
    gateway cache and is therefore scoped to this process's shards for
    free; this reads `sessions` rows for every guild there is. Today
    `process_serves_guild` answers `True` for all of them -- one process
    holds every shard -- and the call is there so that the day it does
    not, four pods do not each post the same document link four times.
    `shard_count` is `None` for a caller with no gateway (every test of
    this function), which changes nothing today and is the honest value.
    """
    for session in sessions_to_announce(await sessions.candidates_for_announcement()):
        session_id = cast(int, session["id"])
        channel_id = cast(int, session["channel_id"])
        if not process_serves_guild(cast(int, session["guild_id"]), shard_count):
            continue
        document_url = cast(str, session["document_url"])
        # `.get`, not `[...]`: a reader that does not supply participants
        # loses the mentions, not the announcement itself.
        participant_ids = cast("Sequence[int]", session.get("participant_ids", ()))
        try:
            await announcer.post(
                channel_id,
                render_announcement(document_url, participant_ids, template_source),
            )
            await sessions.mark_announced(session_id, now)
            log_event(
                log,
                logging.INFO,
                Event.ANNOUNCE_POSTED,
                "Posted the session document link to the channel",
                session_id=session_id,
                channel_id=channel_id,
            )
        except Exception as exc:
            log_exception(
                log,
                logging.WARNING,
                Event.ANNOUNCE_FAILED,
                "Failed to announce the session document; will retry next sweep",
                exc,
                session_id=session_id,
                channel_id=channel_id,
            )
