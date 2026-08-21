"""Installs the one handler every log record leaves through.

`configure_logging` replaces `root.handlers` wholesale -- it never appends --
with a single `StreamHandler(sys.stdout)` carrying `SturnusFilter` and one
of the two formatters. That is the design: `alloy-logs` runs as a DaemonSet
scraping container stdout into Loki, so Sturnus ships no logs itself and
"optimising for Loki" means changing what it prints, not adding a shipper.
One handler means one place records can leave the process, and the filter
sits on it rather than on any logger so that `botocore`'s records get the
same treatment as Sturnus's own.

**The level knob is the most dangerous thing in this file.** Raising the
root logger to DEBUG in production would dump credentials into Loki, and
this is verified in the installed packages rather than assumed:

- `discord/ext/voice_recv/reader.py` logs `secret_key=%s` -- the Discord
  voice secret key -- and raw packet payload bytes, both at DEBUG.
- `discord/ext/voice_recv/gateway.py` reaches the *same* key by another
  route: its `hook()` pretty-prints the whole voice-gateway payload at
  DEBUG for every op except 3 and 6, and op 4 is `SESSION_DESCRIPTION`,
  which is where `discord.gateway.load_secret_key` reads the key from.
  One logger clamped and the other not would have closed the reported
  path and left this one open.
- `discord/ext/voice_recv/voice_client.py` pretty-prints the voice state
  update at DEBUG, which carries the voice `token` and `session_id`.
- `botocore/auth.py` logs `CanonicalRequest`, `StringToSign` and the SigV4
  signature at DEBUG; `botocore/endpoint.py` logs the prepared request
  including the `Authorization` header.

Two library modules Sturnus now leans on much harder were re-read for the
same class of defect and are clean: `voice_recv/rtp.py`'s `RTPPacket.
__repr__` reports `size=` rather than the bytes, so `voice_recv/buffer.py`
logging a dropped packet at DEBUG is a length and three integers; and
`discord/voice_state.py` -- the logger the leak report named -- formats no
secret into any record. Both are recorded here rather than left implicit,
because "not on the list" and "checked and clean" are indistinguishable
from the outside.

So `STURNUS_LOG_LEVEL` applies to `logging.getLogger("sturnus")` **only**,
and two independent mechanisms hold the rest down where no environment
variable can reach them:

- `THIRD_PARTY_FLOOR` is the level below which nothing outside `sturnus.*`
  may go, including the root logger everything unnamed inherits from;
- `NEVER_BELOW` clamps specific loggers tighter still.

That sentence used to be written here as fact while the code contradicted
it. `configure_logging` set the root logger to
`min(resolved_level, resolved_third_party)`, so `STURNUS_LOG_LEVEL=DEBUG`
raised *root* to DEBUG, and every third-party logger absent from
`NEVER_BELOW` -- 24 of them in a running worker -- inherited DEBUG from
there. `discord.ext.voice_recv.gateway` was on the list and stayed quiet;
`discord.http`, which logs whole REST response bodies, was not. The list
was never the problem. Enumerating was.

The floor is what makes the claim structural rather than aspirational, and
`redaction.PATTERNS`' `secret_value` rule is the second lock behind it, for
the case where a future library release moves one of these lines to a level
the floor permits. Turning Sturnus's own logging up is safe; there is no
configuration that turns theirs up.

The same clamp fixes the flood that made a real incident's log unreadable:
`voice_recv.reader` logs `"Received packet for unknown ssrc %s"` with the
full RTP packet repr **at INFO**, and `basicConfig(level=INFO)` on the root
logger -- which is what all three entrypoints did before this module -- let
every one of them through. Sturnus counts those packets itself and emits one
rate-limited line carrying the count instead (see `events.RateLimiter`).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import traceback
from types import TracebackType
from typing import Any, Final

from sturnus import __version__
from sturnus.observability.events import (
    Event,
    current_trace_context,
    log_event,
    log_exception,
)
from sturnus.observability.redaction import (
    SturnusFilter,
    format_exception_safely,
    scrub_fields,
    scrub_text,
)

#: The most verbose any logger outside `sturnus.*` may be, whatever the
#: environment says. **The structural half of the fix; `NEVER_BELOW` below
#: is the per-logger half.**
#:
#: `NEVER_BELOW` is an enumeration of names, and an enumeration answers only
#: for the names on it. Every third-party logger *not* on it carries
#: `level == NOTSET` and takes its effective level from its nearest
#: configured ancestor -- in practice the root logger. That is how the
#: reported leak worked: the clamp named `discord.ext.voice_recv.gateway`
#: and the root logger was set to `min(level, third_party)`, so
#: `STURNUS_LOG_LEVEL=DEBUG` put root at DEBUG and every unnamed logger --
#: `discord.http`, which prints whole REST response bodies, among two dozen
#: others -- inherited it. One list cannot be complete about libraries it
#: does not import.
#:
#: So the level an operator asks for is raised to this floor before
#: anything is configured, and `configure_logging` then sweeps any logger
#: that already carries an explicit level below it. The property that
#: results -- *no logger outside `sturnus.*` sits below `THIRD_PARTY_FLOOR`*
#: -- holds for names nobody enumerated, and
#: `tests/observability/test_third_party_log_floor.py` asserts it over
#: `logging.Logger.manager.loggerDict` rather than over a list.
#:
#: **INFO rather than WARNING, and the difference is a real cost either
#: way.** `discord.voice_state` logs the connect narrative at INFO --
#: "Starting voice handshake... (connection attempt %d)", "Voice handshake
#: complete", "Timed out connecting to voice", "Disconnected from voice by
#: discord, close code %d" -- and that is the entire evidence base for
#: telling apart the three ways capture fails, which is what
#: `voice.join_failed` / `voice.reader_stopped` / `voice.decode_failed`
#: exist to distinguish. WARNING would delete it. What INFO does cost is
#: real and is named in `docs/operations.md` section 7.2: gateway
#: IDENTIFY/RESUME tracing and `discord.http` rate-limit bucket diagnosis
#: are no longer reachable from a Helm value. They need a deliberate,
#: non-production change to this constant -- which is the point. An
#: environment variable a production `values.yaml` can hold must not be
#: able to publish a session key.
#:
#: (`discord.voice_state` is pinned at exactly INFO -- `NEVER_BELOW` keeps
#: its DEBUG payload out, `NEVER_ABOVE` keeps its INFO narrative in even at
#: the deployed default third-party level of WARNING. Both entries are
#: about the same logger and neither is redundant; see their comments.)
THIRD_PARTY_FLOOR: Final = logging.INFO

#: Loggers that may never be turned below their listed level, whatever the
#: environment says. Applied *after* the environment's third-party level, so
#: it is a floor and not a default. Tighter than `THIRD_PARTY_FLOOR` by
#: construction -- `max()` of the two is what gets installed -- so this list
#: is now a set of *exceptions* to a floor rather than the floor itself.
#:
#: `discord.ext.voice_recv.router` is on the list at WARNING deliberately:
#: its own fatal line -- `log.exception("Error in %s loop", self)` when the
#: packet-router thread dies -- is at ERROR and therefore survives. That is
#: the one library line that mattered during the incident this design was
#: written against, and silencing it would remove the only evidence.
NEVER_BELOW: Final[dict[str, int]] = {
    "botocore": logging.WARNING,
    "botocore.auth": logging.WARNING,
    "botocore.endpoint": logging.WARNING,
    "boto3": logging.WARNING,
    "s3transfer": logging.WARNING,
    "discord.ext.voice_recv.reader": logging.WARNING,
    "discord.ext.voice_recv.router": logging.WARNING,
    "discord.ext.voice_recv.opus": logging.WARNING,
    # **The second route to the same secret key, and the one the original
    # report did not name.** `voice_recv/gateway.py`'s `hook()` logs
    # `pformat(data)` at DEBUG for *every* voice-gateway op except 3 and 6
    # -- and op 4 is `SESSION_DESCRIPTION`, whose `d` is where
    # `discord.gateway.load_secret_key` reads `data['secret_key']` from.
    # Clamping `.reader` alone would have left the key one op code away
    # from Loki. Nothing above DEBUG is lost: this module logs one INFO
    # line about unexpected WS keys and nothing at WARNING or above.
    "discord.ext.voice_recv.gateway": logging.WARNING,
    # Same shape, different payload: `voice_client.on_voice_state_update`
    # logs `pformat(data)` at DEBUG, and a voice state update carries the
    # voice `token` and `session_id`. Its one `log.exception` survives the
    # floor, which is the line that would actually matter.
    "discord.ext.voice_recv.voice_client": logging.WARNING,
    "discord.gateway": logging.WARNING,
    "discord.client": logging.WARNING,
    # **INFO, not WARNING, and the level is the whole point of the entry.**
    # Which level carries what, read at the installed version rather than
    # assumed:
    #
    #   DEBUG -- connection-state transitions, DAVE upgrade/downgrade
    #     notices, socket read errors, "Voice server update, closing old
    #     voice websocket". No secret is formatted into any of them
    #     (`secret_key` appears in `voice_state.py` only as an attribute
    #     that is assigned and awaited), but DEBUG is the level the leak
    #     lives at everywhere else in this list, and this is the logger the
    #     leak report named. It stays closed, and it stays listed, because
    #     "absent from the list" and "checked and clean" are
    #     indistinguishable afterwards.
    #   INFO -- the connect narrative: "Starting voice handshake...
    #     (connection attempt %d)", "Voice handshake complete. Endpoint
    #     found: %s", "Connecting to voice...", "Voice connection complete",
    #     "Timed out connecting to voice", "Disconnected from voice by
    #     discord, close code %d", "Successfully resumed voice connection".
    #     Close codes, attempt numbers and a voice server hostname; no
    #     credential, no content.
    #
    # That INFO narrative is the evidence base for `client.py`'s
    # capture-failure cooldown and for telling `voice.join_failed` from
    # `voice.reader_stopped` from `voice.decode_failed` -- it says *which*
    # stage of the handshake failed, which none of Sturnus's own events
    # can. All three entrypoints emitted it before this package existed
    # (`basicConfig(level=INFO)`), and clamping it to WARNING deleted it in
    # exchange for nothing: the leak is one level lower.
    #
    # See `NEVER_ABOVE`, which is what makes the INFO half reachable: an
    # entry here can only ever make a logger *quieter* than the third-party
    # level, so with `THIRD_PARTY_FLOOR` already at INFO this entry changes
    # nothing on its own today. It is kept anyway, for two reasons that are
    # not redundant with each other: it is the record that this logger's
    # DEBUG output was read and judged, and it is what `NEVER_ABOVE`'s pin
    # is checked against -- the two must name the same level, and
    # `test_debug_for_sturnus_does_not_turn_up_the_credential_loggers`
    # fails if they drift apart. Lowering `THIRD_PARTY_FLOOR` in future
    # would make this entry load-bearing again without anyone editing it.
    "discord.voice_state": logging.INFO,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "sqlalchemy.engine": logging.WARNING,
    "asyncio": logging.WARNING,
    # **A real leak, not a precaution.** aiohttp's access logger formats
    # `%r` as `request.path_qs` -- the path *with its query string* -- at
    # INFO, and `link`'s only route is
    # `/oauth/callback?code=<authorization code>&state=<csrf token>`. With
    # the root logger at INFO, which is what all three entrypoints used to
    # set, every successful account link wrote an Outline authorization
    # code into the pod log and therefore into Loki. The blocking gate in
    # `docs/verification/end-to-end-checklist.md` forbids exactly that.
    #
    # Nothing is lost by silencing it: an access line per request
    # duplicates what the ingress already records, and `link`'s own
    # `link.callback_rejected` / `link.established` events carry the
    # diagnostic content without the credential.
    "aiohttp.access": logging.WARNING,
    # ERROR, not WARNING, and measured rather than guessed: the OTLP HTTP
    # exporter logs each retry of a failed batch at WARNING and only the
    # final give-up at ERROR. With Alloy unreachable that is four lines per
    # batch per pod, forever -- and the three retry lines say nothing the
    # give-up line does not, since retrying is the exporter doing its job.
    # Clamping to ERROR keeps one honest line per lost batch.
    #
    # This deliberately leaves export failure *visible in Loki*. The
    # matching `ignore_logger("opentelemetry")` in
    # `sturnus.infrastructure.telemetry` only keeps it out of Sentry, where
    # it would be an issue per retry per batch and would drown the issue
    # list rather than inform it.
    "opentelemetry": logging.ERROR,
}

#: Loggers whose level is *pinned*, so the environment can neither turn them
#: up nor turn them off. The mirror image of `NEVER_BELOW`, and it exists
#: because that dict alone cannot keep a third-party line alive.
#:
#: `NEVER_BELOW` is applied as `max(resolved_third_party, floor)`, so every
#: entry in it can only ever make a logger *quieter* than the third-party
#: level. With `STURNUS_LOG_THIRD_PARTY_LEVEL` at its deployed default of
#: `WARNING` -- see `docs/operations.md` section 7.2 -- an entry of `INFO`
#: there is therefore a no-op: the logger still ends up at `WARNING` and the
#: line the entry was written to keep is still gone. Deciding that a
#: library's INFO output is evidence worth keeping means installing `INFO`
#: outright, which is what this does.
#:
#: Applied **last**, after the sweep below, because the sweep raises any
#: explicit level below `resolved_third_party` back up to it and would
#: otherwise undo this on the very next line.
#:
#: The safety argument is that the pin is a *level*, not an exemption: a
#: pinned logger is held at exactly the level named here, so
#: `STURNUS_LOG_THIRD_PARTY_LEVEL=DEBUG` cannot make it any louder either.
#: Nothing goes in here without the same reading `NEVER_BELOW`'s entries
#: got -- what does this logger print at this level, in the installed
#: version -- and `tests/observability/test_third_party_log_floor.py`
#: asserts both halves for the one entry there is, on the rendered stream.
NEVER_ABOVE: Final[dict[str, int]] = {
    "discord.voice_state": logging.INFO,
}

#: `LogRecord` attributes the JSON formatter renders itself or deliberately
#: drops. Anything else a call site attached lands in `sturnus_fields` and
#: goes through the registry; nothing reaches the line by accident.
_STANDARD_RECORD_ATTRS: Final = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
        "sturnus_event",
        "sturnus_fields",
    }
)

#: Set once `configure_logging` has run. Read by `migrations/env.py`, which
#: must not call `logging.config.fileConfig` over a configuration this
#: process already owns -- see the comment there.
_configured = False


def logging_is_configured() -> bool:
    """Whether `configure_logging` has run in this process."""
    return _configured


log = logging.getLogger(__name__)


class _SafeFormatterBase(logging.Formatter):
    """Shared exception rendering. Never calls `traceback.format_exception`.

    That function renders `str(exc)` for every exception in the chain, which
    is the one part of an exception that can carry payload. This renders the
    frames -- file, line, function, and the *source* line, all static program
    text -- and defers each message to
    `redaction.safe_exception_message`.
    """

    component: str = "unknown"

    def formatException(  # noqa: N802 - overriding logging.Formatter
        self,
        ei: tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None],
    ) -> str:
        exc = ei[1]
        if exc is None:
            return ""
        frames = [
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
            f"    {(frame.line or '').strip()}"
            for frame in traceback.extract_tb(exc.__traceback__)
        ]
        return "\n".join(
            ["Traceback (most recent call last):", *frames, *format_exception_safely(exc)]
        )

    def _payload(self, record: logging.LogRecord) -> dict[str, Any]:
        fields = dict(getattr(record, "sturnus_fields", {}) or {})
        # `SturnusFilter` already scrubbed these; scrubbing again is cheap
        # and makes the formatter safe on its own, so a handler someone adds
        # without the filter still cannot render a raw value.
        fields = scrub_fields(fields)
        # Anything attached through `extra=` outside `log_event` -- including
        # by a third-party library -- is routed through the same registry
        # rather than trusted.
        stray = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_")
        }
        if stray:
            # `warn=False`: third-party records attach attributes that were
            # never meant for this registry, and warning about each would
            # be a flood. They are dropped just as firmly either way.
            fields.update(scrub_fields(stray, warn=False))
        fields.update(current_trace_context())

        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%03dZ"),
            "level": record.levelname,
            "event": getattr(record, "sturnus_event", record.name),
            "component": self.component,
            "logger": record.name,
            "msg": scrub_text(record.getMessage()),
            "version": __version__,
        }
        payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return payload


class JsonFormatter(_SafeFormatterBase):
    """One JSON object per line, on stdout, for `alloy-logs` to hand to Loki.

    `default=str` is a backstop, not a strategy: every value reaching here
    has already been through `scrub_value`, which returns only JSON-native
    types. If something slips past, `str()` of it is far less dangerous than
    the alternative -- an exception inside the formatter, which `logging`
    swallows, turning a leak into silence.
    """

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self._payload(record), default=str, ensure_ascii=False)


class ConsoleFormatter(_SafeFormatterBase):
    """The same content, laid out for a human at a terminal.

    JSON is unreadable in `kubectl logs` without `jq`, and that is the one
    workflow people reach for under pressure. This format is selected
    automatically when stdout is a TTY, so local development never sees
    JSON, and `STURNUS_LOG_FORMAT` overrides the guess in both directions.
    Both formats share `SturnusFilter` and `_payload`, so redaction is
    identical -- the choice is presentation only.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = self._payload(record)
        head = f"{payload['ts']} {payload['level']:<7} {payload['event']}"
        rest = " ".join(
            f"{k}={v}"
            for k, v in payload.items()
            if k not in {"ts", "level", "event", "component", "logger", "msg", "version", "exc"}
        )
        line = f"{head} {payload['msg']}" + (f" | {rest}" if rest else "")
        if "exc" in payload:
            line += "\n" + str(payload["exc"])
        return line


def _resolve_level(value: str, *, name: str) -> int:
    level = logging.getLevelNamesMapping().get(value.strip().upper())
    if level is None:
        raise ValueError(f"{name} must be a logging level name, got {value!r}")
    return level


def configure_logging(
    component: str,
    *,
    level: str | None = None,
    third_party_level: str | None = None,
    log_format: str | None = None,
    stream: Any = None,
) -> logging.Handler:
    """Installs the single handler for this process. Returns it, for tests.

    Reads `STURNUS_LOG_LEVEL`, `STURNUS_LOG_THIRD_PARTY_LEVEL` and
    `STURNUS_LOG_FORMAT` directly from the environment rather than through a
    settings class, because it runs *before* the settings are constructed --
    a `ValidationError` from `WorkerSettings` has to be formatted by
    something, and that something cannot be configured by `WorkerSettings`.

    Sturnus's own tree is set to `level`. Everything else is set to
    `third_party_level` **raised to `THIRD_PARTY_FLOOR`** and then clamped
    further by `NEVER_BELOW`, and the root logger carries that same
    third-party level so a logger nobody enumerated inherits the floor
    rather than inheriting Sturnus's verbosity. There is no configuration
    that lowers any of it, and an unparseable level raises rather than
    being accepted silently.
    """
    level_name = level or os.environ.get("STURNUS_LOG_LEVEL", "INFO")
    third_party_name = third_party_level or os.environ.get(
        "STURNUS_LOG_THIRD_PARTY_LEVEL", "WARNING"
    )
    resolved_level = _resolve_level(level_name, name="STURNUS_LOG_LEVEL")
    requested_third_party = _resolve_level(third_party_name, name="STURNUS_LOG_THIRD_PARTY_LEVEL")
    # Raised, not rejected. Refusing to start on
    # `STURNUS_LOG_THIRD_PARTY_LEVEL=DEBUG` would turn a request for more
    # diagnostics into an outage, which is a worse trade than ignoring it
    # -- but ignoring it silently sends the operator looking for a broken
    # variable, so the clamp announces itself below.
    resolved_third_party = max(requested_third_party, THIRD_PARTY_FLOOR)

    target = stream if stream is not None else sys.stdout
    fmt = log_format or os.environ.get("STURNUS_LOG_FORMAT")
    if fmt is None:
        fmt = "console" if getattr(target, "isatty", lambda: False)() else "json"
    if fmt not in {"json", "console"}:
        raise ValueError(f"STURNUS_LOG_FORMAT must be 'json' or 'console', got {fmt!r}")

    formatter: _SafeFormatterBase = JsonFormatter() if fmt == "json" else ConsoleFormatter()
    formatter.component = component

    handler = logging.StreamHandler(target)
    handler.setFormatter(formatter)
    handler.addFilter(SturnusFilter())

    root = logging.getLogger()
    # Replaced, never appended: a second handler would be a second exit from
    # the process, and only this one carries the filter.
    root.handlers = [handler]
    # **The third-party level, not `min(level, third_party)`.** That `min()`
    # was the leak: it made the root logger as verbose as Sturnus's own
    # tree, and every third-party logger with no explicit level of its own
    # inherits from root. `STURNUS_LOG_LEVEL=DEBUG` therefore turned on
    # DEBUG for two dozen libraries nobody had enumerated, including the
    # one that pretty-prints the voice `secret_key`.
    #
    # It bought nothing in exchange, which is what makes this a pure
    # deletion rather than a trade: `logging` tests a record against the
    # level of the logger it was created on (`Logger.isEnabledFor` ->
    # `getEffectiveLevel`), and never against root's level while
    # propagating. Root's own level gates only records logged on root
    # itself, of which this process emits none. Sturnus's DEBUG output is
    # unaffected -- `test_sturnus_debug_survives_the_third_party_floor`
    # is the assertion that says so rather than the reasoning above.
    root.setLevel(resolved_third_party)

    sturnus_logger = logging.getLogger("sturnus")
    sturnus_logger.setLevel(resolved_level)
    # `logging.config.fileConfig` -- which Alembic's `env.py` calls -- sets
    # `disabled = True` on every logger its ini does not name. A disabled
    # logger emits nothing at all, so inheriting that flag would make this
    # function appear to succeed while silencing the process. This function
    # claims to own logging configuration, so it asserts that rather than
    # inheriting whoever ran last.
    sturnus_logger.disabled = False
    for existing in logging.Logger.manager.loggerDict.values():
        if isinstance(existing, logging.Logger) and existing.name.startswith("sturnus"):
            existing.disabled = False

    for name, floor in NEVER_BELOW.items():
        logging.getLogger(name).setLevel(max(resolved_third_party, floor))

    # Inheriting from root covers every third-party logger that has no
    # level of its own -- which is nearly all of them. The exception is a
    # library that turns *itself* up at import time: an explicit level
    # short-circuits `getEffectiveLevel`, so neither the root level above
    # nor `NEVER_BELOW` (whose list cannot name a logger written after it)
    # would reach it. Raising it here is what turns "the loggers we thought
    # of" into "every logger that exists".
    #
    # Only ever raises. A library that set itself to ERROR keeps ERROR,
    # because quieter than the floor was never the problem.
    for existing in list(logging.Logger.manager.loggerDict.values()):
        if not isinstance(existing, logging.Logger) or existing.name.startswith("sturnus"):
            continue
        if existing.level != logging.NOTSET and existing.level < resolved_third_party:
            existing.setLevel(resolved_third_party)

    # After the sweep, which would otherwise raise these straight back to
    # `resolved_third_party`. `setLevel` outright rather than `min`/`max`:
    # the pin is the decision, and both directions of the environment knob
    # are what it is pinned against.
    for name, pinned in NEVER_ABOVE.items():
        logging.getLogger(name).setLevel(pinned)

    global _configured
    _configured = True

    if requested_third_party < resolved_third_party:
        # Emitted through the handler that was just installed, so it lands
        # in Loki next to the lines the operator is about to go looking
        # for. Without it, `STURNUS_LOG_THIRD_PARTY_LEVEL=DEBUG` produces
        # no new output and no explanation, and the next hour goes into
        # debugging the deployment rather than reading section 7.2.
        log_event(
            log,
            logging.WARNING,
            Event.LOG_LEVEL_CLAMPED,
            "Third-party log level raised to the floor; see docs/operations.md section 7.2",
            # No `component=`: the formatter puts it on every line already.
            reason="third_party_floor",
        )

    return handler


def install_excepthooks() -> None:
    """Routes every uncaught exception through the configured handler.

    Without this, an exception escaping `asyncio.run` reaches the default
    excepthook, which writes an unformatted, unredacted traceback straight
    to stderr -- and Alloy scrapes stderr too. That is not hypothetical:
    `sturnus.config.StrictSettings._reject_blank_required_values` raises
    through pydantic, which embeds the raw input dict in its message, so a
    blank required variable -- the single most likely operator mistake --
    puts the first characters of `STURNUS_DISCORD_TOKEN` into the log store.
    Routing it here formats it through `_SafeFormatterBase`, whose exception
    rendering withholds the message, and `PATTERNS` redacts what pydantic's
    own truncation left.

    Covers all three routes an exception can take out of these processes:
    the main thread, a `threading.Thread` (the OTLP exporters and the voice
    router both run on one), and an asyncio task whose exception is never
    retrieved.
    """

    def hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, tb
        log_exception(log, logging.ERROR, Event.UNHANDLED_EXCEPTION, "Unhandled exception", exc)

    sys.excepthook = hook

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            log_exception(
                log,
                logging.ERROR,
                Event.UNHANDLED_EXCEPTION,
                "Unhandled exception in a thread",
                args.exc_value,
            )

    threading.excepthook = thread_hook


def asyncio_exception_handler(loop: Any, context: dict[str, Any]) -> None:
    """`loop.set_exception_handler` target: the asyncio half of the above.

    asyncio's default handler renders `context["message"]` plus the
    exception through the root logger with no filtering of the message. This
    keeps the same signal -- something failed on the loop and nobody caught
    it -- while putting it through the registry.
    """
    del loop
    exc = context.get("exception")
    if isinstance(exc, BaseException):
        log_exception(
            log,
            logging.ERROR,
            Event.UNHANDLED_EXCEPTION,
            "Unhandled exception on the event loop",
            exc,
        )
    else:
        log.error("Unhandled event-loop error")
