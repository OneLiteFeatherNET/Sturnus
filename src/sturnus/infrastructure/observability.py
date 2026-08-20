"""Error reporting to Sentry, built as a privacy control first.

Sturnus records people talking in a voice channel, which Spec 3 treats as a
criminal-law question rather than a feature, and Spec 12.4 states flatly:
"Neither audio data nor transcript content appears in logs." Sentry is a
second system holding a copy of whatever it is sent, so the question is not
"is the SDK safe by default" but "what exactly are we prepared to copy into
it".

The answer here is: exception type, module, a source-context stack trace, the
`%s`-shaped log template of *this repository's own* log calls, and the tags
that say which process and which release produced it. Nothing else. A Sentry
issue is therefore strictly less than the corresponding pod log, and the
operator reads the message with `kubectl logs`.

Two design choices carry that, and both are deliberate inversions of how the
SDK is normally configured.

**The event is rebuilt from an allowlist, not stripped with a denylist.**
`scrub_event` constructs a new event out of `SAFE_EVENT_KEYS` rather than
deleting `breadcrumbs`/`extra`/`request` from the one the SDK produced. A
denylist is correct against the SDK version it was written for and silently
wrong against the next one -- `sentry_sdk` gains event fields regularly, and
no test fails when a new one starts flowing. Rebuilding inverts the failure
mode: an unrecognised field is dropped, so the cost of an SDK upgrade is a
field missing from the UI, not a transcript in it. The same treatment applies
one level down to stack frames, tags and contexts.

**Every data-bearing integration is off.** `auto_enabling_integrations=False`
matters most for `link`: `AioHttpIntegration` attaches `request.query_string`
and the request body to *error* events, and `link`'s only route is
`/oauth/callback?code=...&state=...`, so an unhandled error there would ship
an Outline authorization code. `StdlibIntegration` records outbound
`http.client` URLs, which for S3 are presigned and carry `X-Amz-Signature`.
Neither is worth its risk, and neither comes back by accident when someone
adds a library.

**Nothing is vouched for because of where it usually comes from.** Two of
the allowlists below were first written around an assumption about the
*producer* of a string rather than about the string, and an assumption about
a producer is only as good as the set of producers you thought of:

- `logentry.message` is `LogRecord.msg`, which is a reviewable source
  literal only for log calls written here, because only they are covered by
  ruff's `G` ruleset. Records the SDK captures from code this repository did
  not write are not: `asyncio`'s default exception handler composes its
  `msg` from `repr(task)`, and a task that raised
  `RuntimeError(f"whisper failed on {segment}")` puts that segment straight
  into it -- past a redaction that correctly handled `exception.values`.
  `logentry` therefore survives only for the `sturnus` logger namespace and
  is dropped for every other producer.
- `OSError.__str__` appends whatever filename the OS reported, and the file
  an `OSError` here is most likely to name is
  `<recording_dir>/session-<session_id>/<discord_user_id>.wav` -- who was
  recorded, and in which session. `strerror` is no better: it is only
  *conventionally* the errno description -- nothing enforces that, it is
  simply `OSError`'s second constructor argument, and `aiohttp` raises
  `ClientOSError(errno, f"Can not write request body for {url}")`, which
  makes it a presigned S3 URL. The errno *number* is the diagnostic value
  and the only part of an `OSError` a fixed table constrains, so the message
  is composed from `os.strerror(exc.errno)` and no string the exception
  carries is read at all.

**Optional telemetry never stops the recording.** A malformed DSN makes
`sentry_sdk.init` raise `BadDsn`, and `init_sentry` is the first statement of
every `main()`: unhandled, one typo in an optional environment variable
CrashLoopBackOffs all three deployments and the bot stops recording. It is
caught, logged at `ERROR` as "disabled", and the process runs on without
reporting -- the state an operator without a DSN is already in.

Failure mode: `sentry_sdk` calls `before_send` inside
`capture_internal_exceptions()` with the result pre-initialised to `None`
(`sentry_sdk/client.py`), so a bug in `scrub_event` drops the event rather
than sending an unfiltered one. Telemetry is lost, never leaked. The visible
symptom is "Sentry went quiet", which is easy to misread as "nothing is
wrong" -- worth one deliberate smoke check after a deploy.

Verified against sentry-sdk 2.68.0.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, cast

import sentry_sdk
from sentry_sdk.integrations.argv import ArgvIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.stdlib import StdlibIntegration
from sentry_sdk.utils import BadDsn

from sturnus import __version__
from sturnus.config import SentrySettings
from sturnus.domain.errors import DiagnosticSafeError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sentry_sdk._types import Breadcrumb, BreadcrumbHint, Event, Hint

REDACTED = "<redacted>"

# The top-level event fields that may be sent. Everything absent from this set
# is dropped, including anything a future SDK version invents.
#
# Notably absent, each for its own reason:
#   breadcrumbs      -- LoggingIntegration crumbs carry `record.message`, the
#                       *interpolated* line, plus every `extra` attribute.
#   extra            -- `log.error("failed", extra={"speaker": name})` lands
#                       here, a route no format-string discipline catches.
#   request          -- query strings and bodies; `link`'s OAuth code.
#   user             -- Discord ids and Outline identities are the subjects
#                       Spec 3 and Spec 12 protect. Nothing in this codebase
#                       calls `set_user`; this turns that convention into a
#                       control rather than a habit.
#   message          -- the free-form capture_message payload.
#   threads          -- carries frame vars for every thread, not just the
#                       raising one.
SAFE_EVENT_KEYS = frozenset(
    {
        "event_id",
        "timestamp",
        "platform",
        "level",
        "logger",
        "release",
        "environment",
        "server_name",  # the pod name; see `init_sentry`
        "sdk",
        "modules",  # installed package versions, from ModulesIntegration
        "transaction",
        "exception",
        "logentry",  # sub-filtered below: our own loggers' template, or nothing
        "contexts",  # sub-filtered below
        "tags",  # sub-filtered below
    }
)

# Stack frame fields that may be sent: location and surrounding *source*.
# `vars` is absent, which is the point of this set -- in
# `sturnus.application.transcription`/`assembly`/`publishing` the locals are
# transcript segments, and in `sturnus.infrastructure.crypto` and
# `sturnus.entrypoints.worker._KeyWrapperDecryptor.__init__` they are the
# unwrapped data key and the master key. `include_local_variables=False`
# already prevents `vars` being collected; rebuilding the frame means the
# highest-value leak is stopped twice, independently.
SAFE_FRAME_KEYS = frozenset(
    {
        "filename",
        "abs_path",
        "function",
        "module",
        "lineno",
        "in_app",
        "context_line",
        "pre_context",
        "post_context",
    }
)

# Tags are searchable free text. `init_sentry` sets exactly one; rebuilding
# from this set means a stray `sentry_sdk.set_tag("speaker", name)` added
# later cannot ride along.
SAFE_TAG_KEYS = frozenset({"component"})

# `runtime` and `os` are interpreter and kernel versions; `trace` is ids.
# Everything else a context can hold (`threadpool`, custom contexts) is not
# reviewed, so it does not travel -- and neither does an unreviewed *field*
# of one that does, which is why this maps each allowed context to its own
# allowlist rather than naming the container alone.
#
# `trace` is the one that makes the difference concrete. `Span.get_trace_context`
# emits `description` -- the span description, i.e. SQL statement text for a
# database span and the full outbound URL for an HTTP one -- alongside the
# ids, plus a free-form `data` dict that any integration may write into. Both
# are exactly what `drop_transaction` exists to keep out of transactions, and
# they arrive on *error* events through this container whenever a span is
# active. `origin`, `status` and `op` are SDK-side enum-shaped strings.
#
# `os.raw_description` is absent for the same reason one level down: it is
# the unparsed `uname` line, which includes the node name.
SAFE_CONTEXT_FIELDS: dict[str, frozenset[str]] = {
    "runtime": frozenset({"type", "name", "version", "build"}),
    "os": frozenset({"type", "name", "version", "kernel_version"}),
    "trace": frozenset({"type", "trace_id", "span_id", "parent_span_id", "op", "origin", "status"}),
}

# The `exception` container itself. `values` is the only key Sentry defines
# for it, and it is rebuilt entry by entry below; naming the set is what
# stops a future SDK version's sibling key riding along one level *above*
# the nesting level that was already closed.
SAFE_EXCEPTION_KEYS = frozenset({"values"})

# The fields of one entry in `exception.values`. `values` was the one
# nesting level not rebuilt from an allowlist, which contradicted the
# principle the rest of this module is built on: a future SDK version that
# starts attaching, say, a rendered `raw_stacktrace` or a `data` blob would
# have been forwarded without a test noticing.
#
# `type` and `module` are identifiers from the source; `value` is judged
# below; `stacktrace` and `mechanism` are themselves sub-filtered.
SAFE_EXCEPTION_VALUE_KEYS = frozenset({"type", "module", "value", "stacktrace", "mechanism"})

# `mechanism.data` is free-form and integration-supplied (the logging
# integration and aiohttp both put request-shaped material there), so only
# the three scalars that say how the exception was caught survive.
SAFE_MECHANISM_KEYS = frozenset({"type", "handled", "synthetic"})

# The logger namespace whose `LogRecord.msg` is a literal written in this
# repository and therefore covered by ruff's `G` ruleset. Every module here
# uses `logging.getLogger(__name__)`, so this is exactly "our own log calls";
# `asyncio`, `discord`, `aiohttp`, `sqlalchemy` and `botocore` are not in it
# and their pre-composed messages are dropped rather than forwarded.
TRUSTED_LOGGER_ROOT = "sturnus"

log = logging.getLogger(__name__)


def _is_trusted_logger(name: object) -> bool:
    """Whether a record's logger is one whose format strings we review.

    See `TRUSTED_LOGGER_ROOT`. The dotted-prefix test is deliberate: a
    hypothetical third-party `sturnusx` logger must not match.
    """
    return isinstance(name, str) and (
        name == TRUSTED_LOGGER_ROOT or name.startswith(f"{TRUSTED_LOGGER_ROOT}.")
    )


def _os_error_value(exc: OSError) -> str:
    """An `OSError` message rebuilt from `errno` alone.

    `OSError` is on the safe side of the default because the failures that
    matter for a process talking to Discord, S3, Postgres and Outline --
    `ConnectionError`, `TimeoutError`, `ssl.SSLError`, `socket.gaierror` --
    carry an `errno`, which is a small integer indexing a fixed table. That
    reasoning covers the number and the table lookup, and *only* those. It
    does not cover any of the strings the exception happens to be holding:

    - `filename`/`filename2`, which for this codebase is a recording path
      naming a Discord user id and a session id;
    - the free-form single-argument form, `OSError("cannot open /data/...")`,
      which any library may raise and which no table constrains; and
    - `strerror` itself, which is only *conventionally* the errno
      description. Nothing enforces that: the attribute is just whatever the
      raiser passed as `OSError`'s second argument, and a library that
      composes it is not misusing the type. aiohttp does exactly that --
      `client_reqrep.py` raises
      `ClientOSError(errno, f"Can not write request body for {self.url!s}")`
      -- so `strerror` is a full request URL, and every URL this process
      writes a body to is either a presigned S3 URL carrying
      `X-Amz-Signature` or an Outline API call. That is the same hazard
      `filename` was, reached through a different attribute, so the fix has
      to be the one that does not depend on guessing which attribute the
      next library picks: read nothing free-form off the exception at all.

    So the description is looked up from the number with `os.strerror`
    rather than read off the exception, and an `OSError` without a usable
    integer `errno` yields `<redacted>` -- the type, module and stack trace
    still travel, which is what actually identifies the failure.
    """
    number = exc.errno
    if not isinstance(number, int) or isinstance(number, bool):
        return REDACTED
    try:
        # The fixed table, keyed by the integer -- not the string the
        # exception is carrying, whoever composed that.
        description = os.strerror(number)
    except (ValueError, OverflowError):
        # An errno outside the platform's table. The number is still a
        # number, but there is nothing to say about it.
        return REDACTED
    value = f"[Errno {number}] {description}"
    # Keep the shape of `str(OSError)` so an operator can see that a path was
    # involved -- and read it with `kubectl logs`, where it never left.
    if exc.filename is not None:
        value += f": {REDACTED}"
    if exc.filename2 is not None:
        value += f" -> {REDACTED}"
    return value


def _exception_value(exc: BaseException | None) -> str | None:
    """The message that may be sent for `exc`, or `None` to redact it.

    `DiagnosticSafeError` is checked first: it is an explicit, reviewed
    opt-in for the whole message, and it wins over the structural rebuild
    `OSError` gets even for a class that happens to be both.
    """
    if isinstance(exc, DiagnosticSafeError):
        return str(exc)
    if isinstance(exc, OSError):
        return _os_error_value(exc)
    return None


def _exception_chain(hint: Hint) -> list[BaseException] | None:
    """The exceptions of this event, ordered to match `exception.values`.

    Sentry serialises a chain oldest-first, with the exception that was
    actually raised last; `hint["exc_info"][1]` is that last one, and
    `__cause__`/`__context__` walk backwards from it. Reversing the walk
    therefore lines up index-for-index with `exception.values` -- which is
    what lets each message be judged against the class that produced it
    rather than against a name in a string.

    Returns `None` when there is no usable `exc_info`, which the caller
    treats as "redact everything".
    """
    exc_info = hint.get("exc_info")
    if not exc_info or len(exc_info) < 2:
        return None
    current = exc_info[1]
    if not isinstance(current, BaseException):
        return None

    chain: list[BaseException] = []
    seen: set[int] = set()
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    chain.reverse()
    return chain


def _scrub_exception(event: dict[str, Any], hint: Hint) -> dict[str, Any]:
    """Redacts every exception message not positively vouched for.

    No structural rule separates `ConnectionRefusedError: [Errno 111]` from
    `RuntimeError: failed on <transcript>` -- both are just a string in
    `value` -- so the default is redaction and the exceptions that opt out
    are named in `_exception_value`. Each entry is rebuilt from
    `SAFE_EXCEPTION_VALUE_KEYS`, its frames from `SAFE_FRAME_KEYS` and its
    mechanism from `SAFE_MECHANISM_KEYS`, at the same time.

    When the chain from `hint` cannot be paired one-for-one with the values
    the SDK produced, every message is redacted: a mismatch means the pairing
    is a guess, and a guess is not a basis for sending someone's words to a
    third system.

    A container that is not the shape this function knows how to rebuild is
    dropped, not forwarded. There is no version of "unrecognised, so send it
    unread" that is compatible with the rest of this module: an `exception`
    that is not a dict, or an `exception["values"]` that is not a list, is a
    payload no allowlist here has looked at, and the whole design is that
    such a payload does not leave the cluster. The cost of being wrong is a
    Sentry issue with no exception section; the cost of the other choice is
    the content of whatever that field turned out to hold.
    """
    exception = event.get("exception")
    if not isinstance(exception, dict):
        event.pop("exception", None)
        return event

    values = exception.get("values")
    if not isinstance(values, list):
        event.pop("exception", None)
        return event

    chain = _exception_chain(hint)
    paired = chain if chain is not None and len(chain) == len(values) else None

    scrubbed_values = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        new_value = {k: v for k, v in value.items() if k in SAFE_EXCEPTION_VALUE_KEYS}
        exc = paired[index] if paired is not None else None
        vouched = _exception_value(exc)
        new_value["value"] = vouched if vouched is not None else REDACTED

        # Same rule as the container above at both of these nesting levels:
        # a `mechanism` or a `stacktrace` that is not the shape rebuilt here
        # is dropped rather than passed through unread.
        mechanism = new_value.pop("mechanism", None)
        if isinstance(mechanism, dict):
            new_value["mechanism"] = {
                k: v for k, v in mechanism.items() if k in SAFE_MECHANISM_KEYS
            }

        stacktrace = new_value.pop("stacktrace", None)
        if isinstance(stacktrace, dict):
            frames = stacktrace.get("frames")
            if isinstance(frames, list):
                new_value["stacktrace"] = {
                    "frames": [
                        {k: v for k, v in frame.items() if k in SAFE_FRAME_KEYS}
                        for frame in frames
                        if isinstance(frame, dict)
                    ]
                }
        scrubbed_values.append(new_value)

    new_exception = {k: v for k, v in exception.items() if k in SAFE_EXCEPTION_KEYS}
    new_exception["values"] = scrubbed_values
    event["exception"] = new_exception
    return event


def scrub_event(event: Event, hint: Hint) -> Event | None:
    """`before_send`: rebuilds the event from the allowlists above.

    This is the control, not a tidy-up pass. Read `SAFE_EVENT_KEYS` and the
    module docstring before changing anything here; in particular, adding a
    key because a Sentry issue looks sparse is a decision about what leaves
    the cluster, not a formatting preference.
    """
    out: dict[str, Any] = {k: v for k, v in event.items() if k in SAFE_EVENT_KEYS}

    # Popped first, so that the one shape this rebuilds is the only one that
    # can survive: Sentry's protocol also accepts `tags` as a list of
    # `[key, value]` pairs, which the copy above would have forwarded intact.
    out.pop("tags", None)
    tags = event.get("tags")
    if isinstance(tags, dict):
        out["tags"] = {k: v for k, v in tags.items() if k in SAFE_TAG_KEYS}

    # Both levels, for the reason in `SAFE_CONTEXT_FIELDS`: naming `trace` as
    # an allowed context is a statement about ids, not about the span
    # `description` and `data` the SDK packs in beside them.
    out.pop("contexts", None)
    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        out["contexts"] = {
            name: {k: v for k, v in context.items() if k in SAFE_CONTEXT_FIELDS[name]}
            for name, context in contexts.items()
            if name in SAFE_CONTEXT_FIELDS and isinstance(context, dict)
        }

    # `logentry` is opt-in per producer, so it is removed first and only
    # added back for a record this repository wrote.
    #
    # `message` is `LogRecord.msg`. `formatted` is the rendered line and
    # `params` is `record.args` verbatim -- `log.error("job %s failed",
    # transcript)` puts the transcript in both, so those two never travel.
    # The template only survives when ruff's `G` ruleset (see pyproject.toml)
    # actually applies to the call site, i.e. when the record came from the
    # `sturnus` logger namespace. `asyncio`'s "Task exception was never
    # retrieved" is composed from `repr(task)` -- which embeds the raised
    # exception's message, transcript and all -- and is a `msg` no format
    # discipline of ours constrains, so it is dropped whole.
    out.pop("logentry", None)
    logentry = event.get("logentry")
    if isinstance(logentry, dict) and _is_trusted_logger(event.get("logger")):
        message = logentry.get("message")
        if isinstance(message, str):
            out["logentry"] = {"message": message}

    out = _scrub_exception(out, hint)
    return cast("Event", out)


def drop_breadcrumb(crumb: Breadcrumb, hint: BreadcrumbHint) -> Breadcrumb | None:
    """`before_breadcrumb`: there are no breadcrumbs.

    Breadcrumbs capture a superset of what a log line renders, so "our logs
    contain no content" does not transfer to them: `LoggingIntegration`
    records `record.message` (already interpolated) plus every non-standard
    `LogRecord` attribute, at `INFO` and above, for `discord.py`, `aiohttp`,
    `sqlalchemy` and `botocore` as well as for our own modules.
    `LoggingIntegration(level=None)` in `init_sentry` already stops the
    logging ones; this stops the rest, and stops any integration added later
    from quietly reintroducing them.
    """
    del crumb, hint
    return None


def drop_transaction(event: Event, hint: Hint) -> Event | None:
    """`before_send_transaction`: there are no transactions.

    `before_send` is never called for transactions -- `sentry_sdk/client.py`
    guards it with `event.get("type") != "transaction"` -- so span data (SQL
    statement text, full outbound URLs) would route around every allowlist
    above. Tracing is off via `traces_sample_rate=0.0`; this is the second
    lock, so that turning sampling on for an afternoon does not silently open
    that path.
    """
    del event, hint
    return None


def _init_client(dsn: str, environment: str) -> None:
    """The `sentry_sdk.init` call itself, split out so it can be guarded.

    Every option here is a decision about what leaves the cluster; see the
    module docstring before changing one.
    """
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        # `sturnus.__version__` reads the installed distribution's version,
        # which release-please keeps in lockstep with the chart's appVersion,
        # so a Sentry release matches a deployed tag without extra wiring.
        release=f"sturnus@{__version__}",
        send_default_pii=False,
        include_local_variables=False,
        # Source context is source code, not runtime values, and it is most
        # of what diagnostic value survives once the locals are gone.
        include_source_context=True,
        max_request_body_size="never",
        max_value_length=1024,
        # No stack traces on messages that did not come from an exception;
        # the frames would be of the logging call site, not of a failure.
        attach_stacktrace=False,
        # See the module docstring: aiohttp would attach `link`'s OAuth
        # callback query string to error events. Off means a library added
        # later is inert until someone opts it in deliberately.
        auto_enabling_integrations=False,
        integrations=[
            LoggingIntegration(
                # No breadcrumbs from log records at all.
                level=None,
                # `log.exception(...)` and asyncio's "Task exception was
                # never retrieved" should become issues. Both arrive with
                # `record.msg` attached, so `scrub_event` keeps that string
                # only for the `sturnus` logger namespace, where ruff `G`
                # guarantees it is a literal; asyncio's is composed from
                # `repr(task)` and is dropped.
                event_level=logging.ERROR,
                # Sentry Logs is a separate pipeline with its own
                # `before_send_log`; it would bypass every allowlist here.
                sentry_logs_level=None,
            )
        ],
        disabled_integrations=[StdlibIntegration(), ArgvIntegration()],
        before_send=scrub_event,
        before_breadcrumb=drop_breadcrumb,
        before_send_transaction=drop_transaction,
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
        enable_logs=False,
        # Defaults to True in 2.68; metrics are a third pipeline that
        # `before_send` never sees.
        enable_metrics=False,
        # Routine shutdown, not defects: both are how the three processes
        # are asked to stop.
        ignore_errors=[asyncio.CancelledError, KeyboardInterrupt],
        # Deliberately no `server_name`: the SDK defaults it to
        # `socket.gethostname()`, which in Kubernetes is the pod name --
        # more useful than anything we could pass, and not PII here.
    )


def init_sentry(component: str, settings: SentrySettings | None = None) -> bool:
    """Initialises Sentry for one process. Returns whether it did.

    Call this as the first statement of `main()`, before the process's own
    settings class is constructed: `SentrySettings` has no required fields,
    so it always builds, which is what makes a settings `ValidationError`
    itself reportable. (Pydantic's message can embed the offending input --
    safe here precisely because exception messages are redacted by default.)

    Sentry is optional. With no DSN -- unset, empty, or whitespace, see
    `SentrySettings` -- `sentry_sdk.init()` is *never reached*: no
    `logging.Logger.callHandlers` patch, no `sys.excepthook`, no `atexit`
    hook, no threading patch. The three processes then behave exactly as they
    do without this module. `sentry_sdk.init(dsn=None)` would not achieve
    that -- it installs all of it and merely sends nothing -- which is why
    the test here is the DSN and not `sentry_sdk.is_active()`.

    It is optional in the other direction too: a DSN that the SDK refuses is
    reported and then treated as no DSN. This function runs before the event
    loop starts in all three `main()`s, so letting `BadDsn` out of it turns a
    typo in an optional telemetry variable into a CrashLoopBackOff of the bot
    that is supposed to be recording -- an availability failure caused
    entirely by the thing that was meant to observe it.
    """
    settings = settings or SentrySettings()
    dsn = settings.sentry_dsn
    if dsn is None:
        return False

    try:
        _init_client(dsn.get_secret_value(), settings.sentry_environment)
    except Exception as exc:
        # `sentry_sdk.init` builds the `Client` and only then attaches it to
        # the global scope, and the DSN is parsed while the transport is
        # built -- so a raise here leaves nothing initialised and nothing
        # patched, exactly the no-DSN state.
        #
        # Only `BadDsn`'s own text is echoed. Its messages name the scheme,
        # the missing hostname, the missing public key or the project path,
        # none of which is key material; an unreviewed message from anywhere
        # else might quote the DSN, so those are reported by class alone.
        detail = str(exc) if isinstance(exc, BadDsn) else type(exc).__name__
        log.error(
            "sentry error reporting is DISABLED for component %s: the SDK rejected "
            "the configured DSN (%s). Check STURNUS_SENTRY_DSN; this process "
            "continues to run without error reporting.",
            component,
            detail,
        )
        return False

    # Three deployments run from one image; this is what tells them apart in
    # the issue list, and it keeps grouping separate per process.
    sentry_sdk.set_tag("component", component)
    log.info("sentry error reporting enabled for component %s", component)
    return True
