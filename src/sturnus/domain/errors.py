"""The exception types no single layer gets to own.

`DiagnosticSafeError` is the marker that decides whether an exception
message may leave the pod. `CorruptRecording` is the refusal two readers
of the stored audio format both raise. Neither belongs to one layer:
the first is checked by `sturnus.infrastructure.observability` and raised
anywhere, the second is raised by `sturnus.application.spectrogram` and by
`sturnus.console.audio`, and `application` may not import the console
(tests/test_architecture.py). `domain` is the only place below both, and
both are stdlib-only by construction so that every layer, `domain`
included, can raise them.

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


class CorruptRecording(Exception):
    """The stored object is not a recording this reader understands.

    Raised before any plaintext is produced -- a wrong magic, a truncated
    upload, a track whose own header says nothing usable, a stored
    spectrogram of a shape this build does not draw. The alternative to
    refusing is plausible-looking noise on somebody's speakers, or a
    picture of a meeting that never happened.

    **Here because two layers name it.** It began in
    `sturnus.console.audio`, where the only reader of the on-disk format
    lived. There are two now: that one, and
    `sturnus.application.spectrogram`, which the worker also draws with --
    and `application` may not import the console (tests/test_architecture.py),
    so an exception both of them raise has to live below both. `domain` is
    the only place that is, and this class needs nothing but the standard
    library to be what it is.

    Deliberately *not* a `DiagnosticSafeError` yet, even though every
    message it can carry today is a literal from this repository. Marking
    it is a claim about every future message too, and the class is raised
    from two modules now; see that class's docstring for what the claim
    costs to make honestly.
    """
