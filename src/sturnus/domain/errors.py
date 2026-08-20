"""The one marker that decides whether an exception message may leave the pod.

Sturnus records people talking. Spec 12.4 -- "Neither audio data nor
transcript content appears in logs" -- is the standard the pod logs are held
to, and `sturnus.infrastructure.observability` holds Sentry to at least the
same one by redacting every exception message it cannot positively vouch
for.

`DiagnosticSafeError` is how a message opts back in. Subclassing it is a
claim about the message, not about the exception's importance, and the claim
is checked by a human at review time -- there is no way to verify it
mechanically, which is exactly why it lives in a marker class rather than in
a regex.

Stdlib-only by construction so that every layer, including `domain`, can
raise it (see `tests/test_architecture.py`).
"""

from __future__ import annotations


class DiagnosticSafeError(Exception):
    """An exception whose message may be sent to Sentry verbatim.

    The contract a subclass accepts, which the person adding the subclass is
    responsible for and a reviewer is expected to check: **every** message
    this exception can carry is built only from

    - string literals written in this repository,
    - enum members, type names and other identifiers from the source,
    - opaque ids (session id, job id, guild id, document id),
    - counts, durations, sizes and other numbers.

    And carries none of

    - transcript text or any other audio-derived content,
    - Discord display names, usernames, or user-authored text,
    - tokens, keys, DSNs, connection strings, presigned URLs,
    - response bodies, request bodies, or query strings from any external
      service.

    Anything not marked is redacted to `<redacted>` before it leaves the
    process, which loses the message but keeps the exception type, the module
    and the stack trace -- and the unredacted message is still in the pod log
    (`kubectl logs`), which never leaves the cluster.

    Deliberately *not* subclasses today:
    `sturnus.infrastructure.documents.outline.PermanentDocumentError` and
    `sturnus.infrastructure.documents.outline_oauth.LinkExchangeError`. Both
    may embed an Outline API response body, which is precisely the class of
    content that needs reading before it is waved through. Marking them is a
    follow-up for someone who has actually read what those bodies contain,
    not a tidy-up.
    """
