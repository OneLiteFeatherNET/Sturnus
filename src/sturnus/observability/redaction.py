"""The one scrubbing implementation. Spans, metrics, logs and Sentry share it.

`scrub_fields` is the function the whole design rests on: it rebuilds a
mapping from `fields.ALLOWED_FIELDS` and passes every surviving value
through `scrub_value`. `events.log_event` calls it, `SturnusFilter` calls it
again on the way to the formatter, and
`sturnus.infrastructure.telemetry.span` calls it before turning fields into
span attributes. There is no second copy to disagree with it.

Four mechanisms, ordered from "prevents the value existing" to "prevents it
leaving the process". Each is structural; none relies on a reviewer
remembering.

1. **Unregistered keys are dropped.** Allowlist rebuild -- see
   `fields.ALLOWED_FIELDS`.
2. **`bytes` are never rendered.** Any `bytes`/`bytearray`/`memoryview`,
   anywhere, becomes `<bytes len=N>`. Unconditional and name-blind, which
   is what makes it the highest-value rule here: the most consequential
   payload in Sturnus -- raw PCM, Opus frames, encrypted blobs, wrapped
   data keys -- is always `bytes`, and this closes all of it as a class.
3. **Strings are pattern-scrubbed and capped.** Discord token shape,
   `AKIA…`, `Bearer …`, `scheme://user:pass@host`, long base64 runs, and
   -- for the one secret in this system that has no recognisable shape --
   anything assigned to a name in `fields.CREDENTIAL_NAMES`. The cap bounds
   the blast radius of anything the patterns miss: half a sentence is still
   a breach, a 40-minute conversation is a categorically larger one.

   That last pattern is the only rule here aimed at text this codebase did
   not write. Rules 1 and 4 govern *our* fields and *our* exceptions, and a
   third-party logger's `%s`-interpolated message is neither: the Discord
   voice `secret_key` arrives as thirty-two small integers inside
   `record.msg`, where an allowlist over field names cannot see it and no
   shape-based pattern would recognise it. It is the second lock behind
   `setup.THIRD_PARTY_FLOOR`, which is what stops that record being emitted
   at all -- deliberately two independent mechanisms, because the first is
   a level and levels are what operators change.
4. **Exception messages are allowlisted by type, not scrubbed by
   guesswork.** `safe_exception_message` -- see `SAFE_MESSAGE_TYPES`.

Replacements are visible (`«redacted:discord_token»`) rather than silent. A
redaction that leaves no trace teaches nobody and hides its own false
positives; this one shows up in the line as a confusing value rather than
as missing data, which is the right failure direction.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from sturnus.domain.errors import DiagnosticSafeError
from sturnus.observability.fields import (
    ALLOWED_FIELDS,
    CREDENTIAL_NAMES,
    DENIED_NAMES,
    OMITTED_WHEN_NONE,
)

REDACTED: Final = "<redacted>"

#: Every string value is truncated to this many characters after pattern
#: scrubbing. Generous enough for a stack frame's source line, far too
#: short for a transcript.
MAX_FIELD_CHARS: Final = 512

#: Exception types whose `str()` may travel verbatim.
#:
#: The single source of truth for this rule.
#: `sturnus.infrastructure.observability.SAFE_VALUE_TYPES` -- Sentry's
#: `before_send` -- is an alias of this tuple rather than a second list, so
#: "which exception messages may leave the pod" is answered in one place for
#: Sentry, Tempo and Loki alike.
#:
#: `OSError` covers `ConnectionError`, `TimeoutError`, `ssl.SSLError` and
#: `socket.gaierror`: failures of a process talking to Discord, S3, Postgres
#: and Outline, with messages composed by the OS and the standard library
#: rather than by us. `DiagnosticSafeError` is the explicit opt-in and
#: carries the contract in its own docstring.
SAFE_MESSAGE_TYPES: Final[tuple[type[BaseException], ...]] = (OSError, DiagnosticSafeError)

#: The credential half of `fields.DENIED_NAMES`, spelled as a regex
#: alternation. Longest first, because Python's `|` is first-match and
#: `secret` would otherwise win against `secret_key` and leave `_key: ...`
#: dangling in front of the marker.
_CREDENTIAL_NAME_ALTERNATION: Final = "|".join(
    re.escape(name) for name in sorted(CREDENTIAL_NAMES, key=lambda n: (-len(n), n))
)

#: Patterns applied to every string value. Each replacement names itself, so
#: an operator seeing `«redacted:aws_access_key_id»` knows a control fired
#: rather than wondering where a field went.
#:
#: This layer is also what catches a leak that predates this package.
#: `sturnus.config.StrictSettings._reject_blank_required_values` raises
#: through pydantic, which embeds the raw input dict in the `ValidationError`
#: message -- `input_value={'discord_token': 'TOKEN_...'}` -- and that
#: exception escapes `asyncio.run` to the default excepthook, to stderr, to
#: Alloy, to Loki. `setup.install_excepthooks` routes it here instead, and
#: the Discord-token pattern redacts what pydantic's own 50-character
#: truncation left of it.
PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    # Discord bot token: three dot-separated base64url segments, the first
    # of which is a base64-encoded snowflake.
    (
        "discord_token",
        re.compile(r"\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}"),
    ),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("aws_sigv4", re.compile(r"(?i)X-Amz-Signature=[A-Za-z0-9%]+")),
    ("url_credentials", re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")),
    # A long unbroken base64-ish run is a key, a wrapped key, or a blob. No
    # legitimate field in `ALLOWED_FIELDS` looks like this.
    ("base64_blob", re.compile(r"\b[A-Za-z0-9+/]{64,}={0,2}")),
    # **The name-shaped rule, and the only one aimed at a message string
    # somebody else composed.** Last in the tuple deliberately: it is the
    # broadest, and running it after the shape-based patterns above lets
    # the more specific control name itself in the line
    # (`Authorization: «redacted:bearer_token»` rather than
    # `Authorization: «redacted:secret_value»`), which is the difference
    # between an operator knowing what fired and guessing.
    #
    # It exists because the Discord voice secret key does not *look* like a
    # secret. `discord/ext/voice_recv/gateway.py` pretty-prints the op-4
    # payload, so the key reaches a record as
    # `'secret_key': [1, 2, 3, ...]` -- thirty-two small integers, matching
    # none of the shapes above, and inside `record.msg` rather than in any
    # field this package's allowlist governs. The level floor in
    # `setup.THIRD_PARTY_FLOOR` is what stops that record existing; this is
    # what stops the value travelling if a future release moves the same
    # line to a level the floor permits.
    #
    # The value alternation runs to the end of the line rather than to the
    # end of the token, because `reader.py`'s
    # `"CryptoError details:\n  data=%s\n  secret_key=%s"` puts the whole
    # key there with no delimiter after it. Over-redaction is the intended
    # failure direction: a confusing `«redacted:secret_value»` in a line is
    # recoverable, and the alternative is not.
    (
        "secret_value",
        re.compile(
            r"(?P<keep>['\"]?\b(?:" + _CREDENTIAL_NAME_ALTERNATION + r")\b['\"]?\s*+[:=]\s*+)"
            # Not a value an earlier, more specific pattern already
            # replaced -- overwriting `\u00abredacted:bearer_token\u00bb` with
            # `\u00abredacted:secret_value\u00bb` would lose which control fired.
            #
            # The `\s*+` above are possessive for this lookahead's sake. A
            # greedy `\s*` gives its whitespace back when the lookahead
            # fails, so `Authorization: \u00abredacted:bearer_token\u00bb` would
            # re-match with `keep` one space shorter and the lookahead
            # satisfied by that space -- passing the guard by stepping
            # around it.
            r"(?!\u00abredacted:)"
            r"(?:\[[^\]]*\]|'[^']*'|\"[^\"]*\"|[^\n,;)\]}]+)",
            re.IGNORECASE,
        ),
    ),
)

log = logging.getLogger(__name__)


def scrub_text(value: str) -> str:
    """Pattern-scrubs and truncates one string."""
    for name, pattern in PATTERNS:
        replacement = f"«redacted:{name}»"
        if name == "url_credentials":
            value = pattern.sub(rf"\g<1>{replacement}@", value)
        elif "keep" in pattern.groupindex:
            # A pattern that recognises a secret by the *name* it is
            # assigned to keeps that name: `secret_key=«redacted:…»` tells
            # an operator which value went, where a bare marker would leave
            # them unable to tell a redaction from a missing field.
            value = pattern.sub(rf"\g<keep>{replacement}", value)
        else:
            value = pattern.sub(replacement, value)
    if len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + f"…«truncated to {MAX_FIELD_CHARS} chars»"
    return value


def scrub_value(value: object) -> object:
    """Renders one value safe, whatever it is.

    `bool` is checked before `int` because `bool` is a subclass of it and
    JSON should keep `true` rather than `1`. Anything that is not a scalar,
    a string, bytes, a `Path` or a short sequence of those is replaced by
    its *type name* -- never its `repr`, which is how an object holding a
    transcript would otherwise render itself into the line.
    """
    if isinstance(value, bytes | bytearray | memoryview):
        # Unconditional, name-blind, and the single most valuable rule in
        # this module: audio is always bytes.
        return f"<bytes len={len(value)}>"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Path):
        return scrub_text(str(value))
    if isinstance(value, Mapping):
        return {str(k): scrub_value(v) for k, v in list(value.items())[:32]}
    if isinstance(value, Sequence):
        return [scrub_value(item) for item in list(value)[:32]]
    return f"<{type(value).__name__}>"


def scrub_fields(fields: Mapping[str, object], *, warn: bool = True) -> dict[str, object]:
    """Rebuilds a field mapping from `ALLOWED_FIELDS`, scrubbing what survives.

    The shared chokepoint: `events.log_event`, the log formatters and
    `sturnus.infrastructure.telemetry.span_attributes` all pass through
    here, so a name is judged once and identically for Loki, Tempo and the
    metric store.

    `warn=True` -- the default, used for fields a Sturnus call site passed
    deliberately -- logs the *name* of anything unregistered. Dropping it
    silently would make a typo indistinguishable from a field that is
    simply always empty, which is the exact failure mode this package is
    arranged to avoid.

    `warn=False` is for sweeping third-party `LogRecord` attributes, where
    unregistered is the normal case and not a mistake: `aiohttp.access`
    alone attaches six per request, and a warning for each would be a flood
    of this module's own making.

    Only the key is ever logged, never the value, and only when it is a
    plain identifier -- the value is precisely what must not travel, and a
    non-identifier key is itself suspicious enough not to echo.
    """
    out: dict[str, object] = {}
    for key, value in fields.items():
        # A `None` in `OMITTED_WHEN_NONE` means "this field does not apply
        # to this process", not "we looked and there was none" -- see the
        # registry entry, which draws the contrast with `close_code`
        # explicitly. It is what lets a call site emit a field
        # *conditionally* without reaching for `**kwargs`, which rule R3 in
        # `tests/test_logging_discipline.py` forbids outright so that every
        # emitted field name stays statically visible at its call site.
        if value is None and key in OMITTED_WHEN_NONE:
            continue
        if key not in ALLOWED_FIELDS:
            if warn:
                log.warning(
                    "Dropping unregistered telemetry field %s",
                    key if key.isidentifier() else "<invalid>",
                )
            continue
        out[key] = scrub_value(value)
    return out


def is_message_safe(exc: BaseException) -> bool:
    """Whether this exception's message may travel verbatim."""
    return isinstance(exc, SAFE_MESSAGE_TYPES)


def safe_exception_message(exc: BaseException) -> str:
    """This exception's message, or a placeholder naming the type that was withheld.

    Nothing structural separates `ConnectionRefusedError: [Errno 111]` from
    `RuntimeError: failed on <transcript>` -- both are a string in `args` --
    so the default is redaction and the exceptions are named in
    `SAFE_MESSAGE_TYPES`.

    This is a real loss and it is worth stating plainly: there will be a
    first incident where the withheld message was the answer. The
    alternative is regex-scrubbing arbitrary third-party exception text,
    which is guesswork dressed as a control. Where a specific field of a
    message matters -- Outline's status code, the failing stage -- the call
    site captures it as a registered field instead, which is both safer and
    more queryable than the sentence it came from.
    """
    if is_message_safe(exc):
        return scrub_text(str(exc))
    return f"<message withheld: {type(exc).__module__}.{type(exc).__qualname__}>"


def error_type(exc: BaseException) -> str:
    """The value of the `error_type` field: a class name, never a message."""
    return type(exc).__qualname__


class SturnusFilter(logging.Filter):
    """Scrubs every record on its way to the formatter.

    Installed on the **handler**, not on a logger, which is the whole point:
    `botocore`'s records and `discord.ext.voice_recv`'s records pass through
    it exactly as Sturnus's own do. A call site cannot route around it
    without adding a second handler, and `setup.configure_logging` replaces
    `root.handlers` wholesale so there is exactly one --
    `tests/observability/test_setup.py` asserts that.

    What it does to a record:

    - drops any `extra` attribute whose name is in `DENIED_NAMES`, whatever
      the call site passed;
    - rebuilds `sturnus_fields` through `scrub_fields`;
    - scrubs `record.args` positionally, so `%s`-interpolated third-party
      values get the bytes rule and the pattern rule too;
    - leaves `record.msg` alone for Sturnus's own loggers, where
      `tests/test_logging_discipline.py` and ruff's `G` ruleset guarantee it
      is a literal, and scrubs it for everyone else.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for denied in DENIED_NAMES:
            if hasattr(record, denied):
                delattr(record, denied)

        fields = getattr(record, "sturnus_fields", None)
        if isinstance(fields, Mapping):
            record.sturnus_fields = scrub_fields(fields)

        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(scrub_value(arg) for arg in record.args)
            elif isinstance(record.args, Mapping):
                record.args = {k: scrub_value(v) for k, v in record.args.items()}

        if isinstance(record.msg, str) and not record.name.startswith("sturnus"):
            record.msg = scrub_text(record.msg)

        return True


def format_exception_safely(exc: BaseException) -> list[str]:
    """The exception chain as type names plus vouched-for messages.

    Deliberately not `traceback.format_exception`: that renders `str(exc)`
    for every exception in the chain, which is precisely the string
    `safe_exception_message` exists to withhold. The frames themselves are
    rendered separately by the formatter -- file, line, function and source
    text are static program text and carry nothing.
    """
    lines: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        lines.append(
            f"{type(current).__module__}.{type(current).__qualname__}: "
            f"{safe_exception_message(current)}"
        )
        current = current.__cause__ or current.__context__
    return lines


__all__ = [
    "ALLOWED_FIELDS",
    "MAX_FIELD_CHARS",
    "PATTERNS",
    "REDACTED",
    "SAFE_MESSAGE_TYPES",
    "SturnusFilter",
    "error_type",
    "format_exception_safely",
    "is_message_safe",
    "safe_exception_message",
    "scrub_fields",
    "scrub_text",
    "scrub_value",
]
