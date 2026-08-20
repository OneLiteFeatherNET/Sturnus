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
from datetime import datetime
from typing import Protocol, cast

from jinja2.sandbox import SandboxedEnvironment

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
DEFAULT_ANNOUNCEMENT_TEMPLATE = "The transcript for this session is ready: {{ document_url }}"


def _build_environment() -> SandboxedEnvironment:
    # Built directly from Jinja2 here rather than reusing
    # `sturnus.infrastructure.templates.engine.build_environment`: this
    # module lives in `sturnus.application`, which must never import
    # `sturnus.infrastructure` (see the module docstring) -- the same
    # reasoning `sturnus.application.documents._build_environment` follows
    # for the identical reason.
    return SandboxedEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True)


def render_announcement(
    document_url: str, template_source: str = DEFAULT_ANNOUNCEMENT_TEMPLATE
) -> str:
    """Renders the Discord announcement text for one finished session's link."""
    template = _build_environment().from_string(template_source)
    return template.render(document_url=document_url)


class SessionReader(Protocol):
    """Where announcement candidates are read from and `announced_at` is stamped."""

    async def candidates_for_announcement(self) -> list[dict[str, object]]:
        """Every `documented` session, shaped for `sessions_to_announce`.

        Deliberately unfiltered by `announced_at`/`document_url`: those
        checks are `sessions_to_announce`'s job alone, so there is exactly
        one definition of the selection rule anywhere in the codebase.
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
    """
    for session in sessions_to_announce(await sessions.candidates_for_announcement()):
        session_id = cast(int, session["id"])
        channel_id = cast(int, session["channel_id"])
        document_url = cast(str, session["document_url"])
        try:
            await announcer.post(channel_id, render_announcement(document_url, template_source))
            await sessions.mark_announced(session_id, now)
        except Exception as exc:
            log.warning("Failed to announce session %d; will retry: %s", session_id, exc)
