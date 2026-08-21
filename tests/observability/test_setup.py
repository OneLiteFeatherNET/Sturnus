"""Logging configuration, asserted on the bytes that actually get written.

Every test here parses the line the handler emitted rather than inspecting
the handler's configuration. A formatter that is configured correctly and
renders wrongly is exactly the failure a configuration assertion cannot
see, and the whole point of the JSON format is that something downstream
has to be able to parse it.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from sturnus.domain.errors import DiagnosticSafeError
from sturnus.observability.events import Event, log_event, log_exception
from sturnus.observability.redaction import SturnusFilter
from sturnus.observability.setup import (
    NEVER_ABOVE,
    NEVER_BELOW,
    configure_logging,
    install_excepthooks,
)

TRANSCRIPT = "and then the client said they would not be renewing"


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    """A configured handler writing into a buffer, torn down afterwards."""
    buffer = io.StringIO()
    saved_handlers = logging.getLogger().handlers[:]
    saved_level = logging.getLogger().level
    configure_logging("worker", log_format="json", stream=buffer)
    yield buffer
    logging.getLogger().handlers = saved_handlers
    logging.getLogger().setLevel(saved_level)


def _lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _event(stream: io.StringIO, event: Event) -> dict[str, object]:
    """The one line carrying this event.

    Picked by name rather than by position because `scrub_fields` emits its
    own "dropping unregistered field" warning through the same handler --
    which is the behaviour under test two tests down, not noise to suppress.
    """
    matching = [line for line in _lines(stream) if line["event"] == str(event)]
    assert len(matching) == 1, f"expected one {event} line, got {len(matching)}"
    return matching[0]


#: AWS's own documentation example for an access key id, assembled rather
#: than written whole. Not because it opens anything -- it is the string AWS
#: prints in its own docs -- but because secret scanners flag the shape, and
#: they are right to: a reader cannot tell this from a real one either. The
#: runtime value is identical, which is the point, since these tests exist to
#: prove the scrubber catches exactly this shape. Same reasoning as
#: `DISCORD_TOKEN` in tests/observability/test_redaction.py.
AWS_ACCESS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"


def test_an_emitted_line_is_one_parseable_json_object(stream: io.StringIO) -> None:
    log = logging.getLogger("sturnus.test")
    log_event(
        log,
        logging.INFO,
        Event.JOB_TRANSCRIBED,
        "Transcribed a recording",
        job_id=7,
        session_id=4711,
        segments=12,
        realtime_factor=1.04,
    )

    line = _event(stream, Event.JOB_TRANSCRIBED)
    assert line["event"] == "job.transcribed"
    assert line["level"] == "INFO"
    assert line["component"] == "worker"
    assert line["logger"] == "sturnus.test"
    assert line["msg"] == "Transcribed a recording"
    assert line["job_id"] == 7
    assert line["session_id"] == 4711
    assert line["realtime_factor"] == 1.04
    assert "version" in line and "ts" in line


def test_an_unregistered_field_never_reaches_the_line(stream: io.StringIO) -> None:
    """The registry is enforced at emit time, not documented and hoped for."""
    log = logging.getLogger("sturnus.test")
    log_event(
        log,
        logging.INFO,
        Event.JOB_TRANSCRIBED,
        "Transcribed a recording",
        job_id=7,
        transcript=TRANSCRIPT,
        display_name="Alice Example",
    )

    line = _event(stream, Event.JOB_TRANSCRIBED)
    assert line["job_id"] == 7
    assert "transcript" not in line
    assert "display_name" not in line
    assert TRANSCRIPT not in stream.getvalue()
    assert "Alice" not in stream.getvalue()


def test_audio_bytes_offered_as_a_field_render_as_a_length(stream: io.StringIO) -> None:
    log = logging.getLogger("sturnus.test")
    log_event(
        log,
        logging.INFO,
        Event.SESSION_SPEAKER_FINALIZED,
        "Finalized one speaker",
        session_id=1,
        bytes=b"\xde\xad\xbe\xef" * 512,
    )

    line = _event(stream, Event.SESSION_SPEAKER_FINALIZED)
    assert line["bytes"] == "<bytes len=2048>"
    assert "\\xde" not in stream.getvalue()


def test_a_third_party_record_is_scrubbed_too(stream: io.StringIO) -> None:
    """The filter is on the handler, so `botocore` gets the same treatment."""
    logging.getLogger("botocore").setLevel(logging.DEBUG)
    logging.getLogger("botocore").warning("signing request with %s", AWS_ACCESS_KEY_ID)

    (line,) = _lines(stream)
    assert AWS_ACCESS_KEY_ID not in stream.getvalue()
    assert "«redacted:aws_access_key_id»" in str(line["msg"])


def test_an_exception_message_is_withheld_but_the_traceback_survives(
    stream: io.StringIO,
) -> None:
    """Type and frames answer "where did this break"; the message is the payload."""
    log = logging.getLogger("sturnus.test")
    try:
        raise RuntimeError(f"jinja failed rendering {TRANSCRIPT}")
    except RuntimeError as exc:
        log_exception(
            log,
            logging.WARNING,
            Event.SESSION_DOCUMENT_RETRY_FAILED,
            "Document creation failed",
            exc,
            session_id=4711,
        )

    line = _event(stream, Event.SESSION_DOCUMENT_RETRY_FAILED)
    assert line["error_type"] == "RuntimeError"
    assert TRANSCRIPT not in stream.getvalue()
    rendered = str(line["exc"])
    assert "<message withheld: builtins.RuntimeError>" in rendered
    # The frames are static program text and are what locate the failure.
    assert "test_setup.py" in rendered
    assert "Traceback" in rendered


def test_a_vouched_for_exception_keeps_its_message(stream: io.StringIO) -> None:
    class Vouched(DiagnosticSafeError):
        pass

    log = logging.getLogger("sturnus.test")
    try:
        raise Vouched("guild 42 has no document_target configured")
    except Vouched as exc:
        log_exception(log, logging.WARNING, Event.SWEEP_FAILED, "Sweep failed", exc)

    line = _event(stream, Event.SWEEP_FAILED)
    assert "guild 42 has no document_target configured" in str(line["exc"])


def test_configure_logging_replaces_rather_than_appends() -> None:
    """One handler means one exit, and only that one carries the filter.

    A second handler would be a second way out of the process, bypassing
    `SturnusFilter` entirely, so `configure_logging` assigns `root.handlers`
    rather than appending to it. Asserted immediately after the call --
    pytest's own `caplog` plugin re-attaches its capture handler afterwards,
    which is a harness artifact rather than a property of the process.
    """
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        root.handlers = [
            logging.StreamHandler(io.StringIO()),
            logging.StreamHandler(io.StringIO()),
        ]
        handler = configure_logging("bot", log_format="json", stream=io.StringIO())
        assert root.handlers == [handler]
        assert any(isinstance(f, SturnusFilter) for f in handler.filters)
    finally:
        root.handlers = saved


def test_debug_for_sturnus_does_not_turn_up_the_credential_loggers() -> None:
    """The clamp is a floor no environment variable can undercut.

    `STURNUS_LOG_LEVEL=DEBUG` is a knob an operator will reach for during an
    incident. On `botocore.auth` it prints the SigV4 signature, and on
    `discord.ext.voice_recv.reader` it prints the Discord voice secret key
    and raw packet bytes.
    """
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        configure_logging(
            "bot",
            level="DEBUG",
            third_party_level="DEBUG",
            log_format="json",
            stream=io.StringIO(),
        )
        assert logging.getLogger("sturnus").level == logging.DEBUG
        for name, floor in NEVER_BELOW.items():
            assert logging.getLogger(name).level >= floor, name
    finally:
        root.handlers = saved


def test_the_aiohttp_access_log_is_clamped() -> None:
    """`link`'s only route is `/oauth/callback?code=...`, and `%r` is `path_qs`."""
    assert NEVER_BELOW["aiohttp.access"] >= logging.WARNING


@pytest.mark.parametrize(
    "logger_name",
    [
        # `log.debug("CryptoError details:\n  data=%s\n  secret_key=%s", ...)`
        "discord.ext.voice_recv.reader",
        # `hook()` pretty-prints every voice-gateway payload except ops 3
        # and 6 at DEBUG, and op 4 (SESSION_DESCRIPTION) is the one that
        # carries `secret_key`. A different logger, the same secret.
        "discord.ext.voice_recv.gateway",
        # The voice state update, which carries the voice `token`.
        "discord.ext.voice_recv.voice_client",
    ],
)
def test_no_logger_that_can_see_the_voice_secret_key_may_emit_debug(logger_name: str) -> None:
    """The reported leak, closed at every route rather than at the one named.

    The report was `STURNUS_LOG_LEVEL=DEBUG` puts the Discord voice
    `secret_key` into Loki. The mechanism it proposed -- redaction covers
    Sturnus's own structured fields and can do nothing about a third-party
    logger's message string -- is right, and is exactly why the fix is a
    level floor rather than a scrubber: `NEVER_BELOW` is applied *after*
    the environment's level, so no value of any variable reaches these
    loggers' DEBUG.

    Parametrised over every logger in the installed packages that can see
    the key or the voice token, not just the one the report named, because
    closing one route and leaving the others open is indistinguishable from
    closing none if the next release moves a log line.
    """
    assert NEVER_BELOW[logger_name] >= logging.WARNING


def test_the_logger_the_report_named_is_shut_at_debug_and_open_at_info() -> None:
    """`discord.voice_state`, which is a *level* decision rather than a list one.

    It is the logger the leak report named, and it is the one on these
    lists whose two levels say different things:

    - **DEBUG** is connection-state transitions and DAVE upgrade notices.
      No secret is formatted into any of them in the installed version, but
      DEBUG is where the leak lives on every other logger here and a name
      absent from `NEVER_BELOW` reads as "considered and cleared" when it
      was never considered. It stays shut.
    - **INFO** is the connect narrative -- handshake attempts, endpoint
      found, timed out, close codes, resumed. That is the evidence base for
      the capture-failure cooldown, all three entrypoints emitted it before
      this package existed, and it carries no credential.

    Pinned by equality against two literals rather than by `>=` against
    one, because both bounds are the point: `>= WARNING` would pass while
    deleting the narrative, and `>= DEBUG` would pass while publishing the
    payload. `tests/observability/test_third_party_log_floor.py` asserts
    the same two claims on the rendered stream; this one pins the
    declaration a reader edits.
    """
    assert NEVER_BELOW["discord.voice_state"] == logging.INFO
    assert NEVER_ABOVE["discord.voice_state"] == logging.INFO


def test_a_pin_survives_the_environment_in_both_directions() -> None:
    """`NEVER_ABOVE` is a level, not an exemption from the floor.

    Both directions matter and they fail differently: without the pin the
    default `WARNING` swallows the narrative, and without it being a fixed
    level `STURNUS_LOG_THIRD_PARTY_LEVEL=DEBUG` would reopen the payload.
    """
    root = logging.getLogger()
    saved = root.handlers[:]
    saved_level = logging.getLogger("discord.voice_state").level
    try:
        for third_party in ("ERROR", "WARNING", "INFO", "DEBUG"):
            configure_logging(
                "bot",
                level="DEBUG",
                third_party_level=third_party,
                log_format="json",
                stream=io.StringIO(),
            )
            assert logging.getLogger("discord.voice_state").level == logging.INFO, third_party
    finally:
        root.handlers = saved
        logging.getLogger("discord.voice_state").setLevel(saved_level)


def test_an_unparseable_level_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="STURNUS_LOG_LEVEL"):
        configure_logging("bot", level="verbose", stream=io.StringIO())


@pytest.mark.usefixtures("stream")
def test_the_console_format_carries_the_same_redaction() -> None:
    """Presentation differs; the filter does not.

    Takes the `stream` fixture only for its teardown, which restores the
    root handlers this test replaces.
    """
    root = logging.getLogger()
    saved = root.handlers[:]
    buffer = io.StringIO()
    try:
        configure_logging("bot", log_format="console", stream=buffer)
        log_event(
            logging.getLogger("sturnus.test"),
            logging.INFO,
            Event.SESSION_CLOSED,
            "Session closed",
            session_id=1,
            transcript=TRANSCRIPT,
        )
        rendered = buffer.getvalue()
        assert "session.closed" in rendered
        assert "session_id=1" in rendered
        assert TRANSCRIPT not in rendered
    finally:
        root.handlers = saved


def test_an_uncaught_exception_is_routed_through_the_handler(
    stream: io.StringIO,
) -> None:
    """Otherwise it reaches stderr unformatted -- and Alloy scrapes stderr too."""
    import sys

    saved = sys.excepthook
    try:
        install_excepthooks()
        try:
            raise ValueError(f"boom {TRANSCRIPT}")
        except ValueError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
    finally:
        sys.excepthook = saved

    line = _event(stream, Event.UNHANDLED_EXCEPTION)
    assert line["error_type"] == "ValueError"
    assert TRANSCRIPT not in stream.getvalue()
