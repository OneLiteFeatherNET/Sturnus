"""Link publishing selection (Spec 8.5).

The worker marks a session `documented` once its protocol exists in the
document system and records the document's URL on the session row. The bot
then polls, on `publish_poll_seconds`, for sessions still waiting to have
their link posted into the channel; `sessions_to_announce` is the pure
selection behind that poll, given whatever a caller already read from the
database. Posting the link and stamping `announced_at` are I/O and belong
to the infrastructure adapter that calls this function.

`announced_at` is what protects against a restart re-posting every link the
bot ever published: once it is set, a session is never selected again,
regardless of how many times the poll runs afterwards.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

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
