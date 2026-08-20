"""Error reporting to Sentry, built as a privacy control first.

Sturnus records people talking in a voice channel, which Spec 3 treats as a
criminal-law question rather than a feature, and Spec 12.4 states flatly:
"Neither audio data nor transcript content appears in logs." Sentry is a
second system holding a copy of whatever it is sent, so the question is not
"is the SDK safe by default" but "what exactly are we prepared to copy into
it".

The answer here is: exception type, module, a source-context stack trace, the
`%s`-shaped log template, and the three tags that say which process and which
release produced it. Nothing else. A Sentry issue is therefore strictly less
than the corresponding pod log, and the operator reads the message with
`kubectl logs`.

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
from typing import TYPE_CHECKING, Any, cast

import sentry_sdk
from sentry_sdk.integrations.argv import ArgvIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.stdlib import StdlibIntegration

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
        "logentry",  # sub-filtered below: only the uninterpolated template
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
# reviewed, so it does not travel.
SAFE_CONTEXT_KEYS = frozenset({"runtime", "os", "trace"})

# Exception types whose `value` -- the message -- may be sent unredacted.
#
# `OSError` covers `ConnectionError`, `TimeoutError`, `ssl.SSLError` and
# `socket.gaierror`: the failures that actually matter for a process talking
# to Discord, S3, Postgres and Outline, with messages composed by the OS and
# the standard library rather than by us.
#
# `DiagnosticSafeError` is the explicit opt-in; its docstring carries the
# contract and the reasons two obvious candidates are not on it.
SAFE_VALUE_TYPES: tuple[type[BaseException], ...] = (OSError, DiagnosticSafeError)

log = logging.getLogger(__name__)


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
    `value` -- so the default is redaction and the exceptions are named in
    `SAFE_VALUE_TYPES`. Frames are rebuilt from `SAFE_FRAME_KEYS` at the same
    time.

    When the chain from `hint` cannot be paired one-for-one with the values
    the SDK produced, every message is redacted: a mismatch means the pairing
    is a guess, and a guess is not a basis for sending someone's words to a
    third system.
    """
    exception = event.get("exception")
    if not isinstance(exception, dict):
        return event

    values = exception.get("values")
    if not isinstance(values, list):
        return event

    chain = _exception_chain(hint)
    paired = chain if chain is not None and len(chain) == len(values) else None

    scrubbed_values = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        new_value = dict(value)
        exc = paired[index] if paired is not None else None
        if not isinstance(exc, SAFE_VALUE_TYPES):
            new_value["value"] = REDACTED
        stacktrace = new_value.get("stacktrace")
        if isinstance(stacktrace, dict) and isinstance(stacktrace.get("frames"), list):
            new_value["stacktrace"] = dict(stacktrace) | {
                "frames": [
                    {k: v for k, v in frame.items() if k in SAFE_FRAME_KEYS}
                    for frame in stacktrace["frames"]
                    if isinstance(frame, dict)
                ]
            }
        scrubbed_values.append(new_value)

    event["exception"] = dict(exception) | {"values": scrubbed_values}
    return event


def scrub_event(event: Event, hint: Hint) -> Event | None:
    """`before_send`: rebuilds the event from the allowlists above.

    This is the control, not a tidy-up pass. Read `SAFE_EVENT_KEYS` and the
    module docstring before changing anything here; in particular, adding a
    key because a Sentry issue looks sparse is a decision about what leaves
    the cluster, not a formatting preference.
    """
    out: dict[str, Any] = {k: v for k, v in event.items() if k in SAFE_EVENT_KEYS}

    tags = event.get("tags")
    if isinstance(tags, dict):
        out["tags"] = {k: v for k, v in tags.items() if k in SAFE_TAG_KEYS}

    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        out["contexts"] = {k: v for k, v in contexts.items() if k in SAFE_CONTEXT_KEYS}

    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        # `message` is `LogRecord.msg`, the format string as written in the
        # source. `formatted` is the rendered line and `params` is
        # `record.args` verbatim -- `log.error("job %s failed", transcript)`
        # puts the transcript in both. Only the template survives, and ruff's
        # `G` ruleset (see pyproject.toml) is what keeps the template a
        # literal rather than an f-string someone interpolated data into.
        out["logentry"] = {"message": logentry.get("message", "")}

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
    """
    settings = settings or SentrySettings()
    if settings.sentry_dsn is None:
        return False

    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.sentry_environment,
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
                # never retrieved" should become issues. Safe only because
                # `scrub_event` keeps `logentry.message` alone and ruff `G`
                # keeps that a literal.
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
    # Three deployments run from one image; this is what tells them apart in
    # the issue list, and it keeps grouping separate per process.
    sentry_sdk.set_tag("component", component)
    log.info("sentry error reporting enabled for component %s", component)
    return True
