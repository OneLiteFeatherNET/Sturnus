"""The voice `secret_key` must not reach Loki at any level an operator can set.

The reported leak was: with `STURNUS_LOG_LEVEL=DEBUG`, the Discord voice
`secret_key` appears in the pod log and therefore in Loki. The mechanism is
one this package's redaction cannot touch. `redaction.scrub_fields` governs
*Sturnus's* structured fields; the key travels inside a third-party
logger's own `%s`-interpolated message string, produced by
`discord/ext/voice_recv/gateway.py`'s `hook()` on every voice connect. No
allowlist over our field names sees it.

So every test here drives the **real** `configure_logging` and asserts on
the **rendered stream** -- the bytes `alloy-logs` would scrape -- rather
than on a level, a filter object or an entry in a dict. A configuration
assertion is exactly what the previous round of this fix consisted of, and
it passed while the leak was open: `NEVER_BELOW` named the right loggers,
and `root.setLevel(min(level, third_party))` set the root logger to DEBUG
underneath them, so every third-party logger the enumeration did *not* name
inherited DEBUG from root.

Two properties are pinned, and they are different claims:

1. **Suppression.** No logger outside `sturnus.*` may sit below
   `THIRD_PARTY_FLOOR`, whatever the environment says -- asserted as a
   property over `logging.Logger.manager.loggerDict`, not as another list
   of names. A list is what failed.
2. **Redaction.** A rendered `secret_key: ...` in a message string is
   scrubbed even at a level that *is* allowed, so a future library release
   that moves the line to WARNING does not reopen the same hole.

Where a level is asserted numerically it is written as a `logging.*`
literal, never as the constant under test: `assert
logger.getEffectiveLevel() >= THIRD_PARTY_FLOOR` would pass for every
possible value of `THIRD_PARTY_FLOOR`, including `DEBUG`, and would
therefore pin nothing at all.
"""

from __future__ import annotations

import io
import json
import logging
import os
from collections.abc import Iterator
from pprint import pformat

# Importing the submodules is what puts their loggers into `loggerDict`;
# without this the sweep below would run over a dictionary that does not
# yet contain the loggers that matter, and pass by measuring nothing.
import discord  # noqa: F401
import discord.ext.voice_recv.gateway  # noqa: F401
import discord.ext.voice_recv.reader  # noqa: F401
import discord.ext.voice_recv.voice_client  # noqa: F401
import discord.http  # noqa: F401
import discord.state  # noqa: F401
import discord.voice_state  # noqa: F401
import pytest

from sturnus.observability.events import Event
from sturnus.observability.setup import THIRD_PARTY_FLOOR, configure_logging

#: Distinctive on purpose. The real key is 32 small integers, and `1`, `2`,
#: `12` are substrings of timestamps, byte counts and version numbers -- a
#: canary made of those would be indistinguishable from a false positive.
#: These are the same *shape* (a list of ints, pretty-printed) with values
#: no other part of a log line produces.
SECRET_KEY_CANARY = list(range(31337, 31337 + 32))
SECRET_KEY_DIGITS = "31337"

#: The voice `token` from a VOICE_STATE_UPDATE, which
#: `voice_recv/voice_client.py:53` pretty-prints at DEBUG next to the
#: `session_id`.
VOICE_TOKEN_CANARY = "CANARYVOICETOKEN-do-not-put-me-in-loki"

#: `discord/http.py:707` logs the whole REST *response body* at DEBUG. Not
#: a key, but message content and display names -- the same class of
#: payload, through a logger no list in this repository names.
REST_BODY_CANARY = "CANARYRESTBODY-and-then-she-said-she-would-resign"


def session_description_payload() -> dict[str, object]:
    """The `d` of voice op 4, in the shape `hook()` receives it.

    Op 4 is `SESSION_DESCRIPTION`, and its payload is where
    `discord.gateway.load_secret_key` reads `data['secret_key']` from --
    so this is the literal dictionary that carries the key on every voice
    connect, not a stand-in for it.
    """
    return {
        "dave_protocol_version": 0,
        "mode": "aead_xchacha20_poly1305_rtpsize",
        "secret_key": SECRET_KEY_CANARY,
        "ssrc": 12345,
    }


def drive_the_voice_handshake() -> None:
    """The verbatim call shapes from the installed packages.

    Copied from site-packages rather than paraphrased, because the thing
    under test is what those exact calls do to the rendered stream:

    - `discord/ext/voice_recv/gateway.py:57`
    - `discord/ext/voice_recv/voice_client.py:53`
    - `discord/voice_state.py` (connection-state transitions)
    - `discord/http.py:707`
    """
    logging.getLogger("discord.ext.voice_recv.gateway").debug(
        "Received op %s: \n%s", 4, pformat(session_description_payload(), compact=True)
    )
    logging.getLogger("discord.ext.voice_recv.voice_client").debug(
        "Got voice_client VSU: \n%s",
        pformat({"token": VOICE_TOKEN_CANARY, "session_id": "abc"}, compact=True),
    )
    logging.getLogger("discord.voice_state").debug(
        "Connection state changed to %s", session_description_payload()
    )
    logging.getLogger("discord.gateway").debug(
        "Voice websocket frame received: %s", session_description_payload()
    )
    logging.getLogger("discord.http").debug(
        "%s %s has received %s",
        "GET",
        "/channels/1/messages",
        {"content": REST_BODY_CANARY},
    )
    # A logger no list in this repository names, and that does not exist
    # until this line runs -- the case an enumeration is structurally
    # unable to cover.
    logging.getLogger("some_future_library.transport").debug(
        "frame: %s", session_description_payload()
    )


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """Snapshot and restore every level `configure_logging` may move.

    It sets levels across the whole tree, so restoring the root handler
    alone would leak DEBUG-suppressing levels into whatever test runs next.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_root_level = root.level
    saved_levels = {
        name: existing.level
        for name, existing in logging.Logger.manager.loggerDict.items()
        if isinstance(existing, logging.Logger)
    }
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_root_level)
    for name, level in saved_levels.items():
        existing = logging.Logger.manager.loggerDict.get(name)
        if isinstance(existing, logging.Logger):
            existing.setLevel(level)


def rendered(stream: io.StringIO) -> str:
    return stream.getvalue()


def lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def assert_no_credential_reached_the_stream(stream: io.StringIO) -> None:
    output = rendered(stream)
    # Without this the whole assertion set passes vacuously on a stream
    # nothing was ever written to.
    assert output, "nothing was logged; the test would pass by writing nothing"
    for what, canary in (
        ("the voice secret key", SECRET_KEY_DIGITS),
        ("the voice token", VOICE_TOKEN_CANARY),
        ("a REST response body", REST_BODY_CANARY),
    ):
        assert canary not in output, f"{what} reached the log stream"
    for line in output.splitlines():
        if line.strip():
            json.loads(line)


def test_the_canaries_really_are_in_the_records_this_file_drives() -> None:
    """The control on every "canary not in output" assertion below.

    Those assertions are absence checks, and an absence check passes just
    as happily when it is looking for a string the test never planted. If
    someone renames a canary, edits the payload, or a library changes the
    shape of what it logs, this is what goes red instead of the whole file
    going quietly green.
    """
    payload = pformat(session_description_payload(), compact=True)
    assert SECRET_KEY_DIGITS in payload, "the secret-key canary is not in the payload"

    vsu = pformat({"token": VOICE_TOKEN_CANARY, "session_id": "abc"}, compact=True)
    assert VOICE_TOKEN_CANARY in vsu

    assert REST_BODY_CANARY in str({"content": REST_BODY_CANARY})


@pytest.mark.usefixtures("restored_logging")
def test_sturnus_debug_puts_no_voice_secret_key_in_the_stream() -> None:
    """The reported case: `STURNUS_LOG_LEVEL=DEBUG`, everything else default."""
    buffer = io.StringIO()
    configure_logging("worker", level="DEBUG", log_format="json", stream=buffer)

    logging.getLogger("sturnus.test").debug("sturnus own debug line")
    drive_the_voice_handshake()

    assert_no_credential_reached_the_stream(buffer)


@pytest.mark.usefixtures("restored_logging")
def test_sturnus_debug_survives_the_third_party_floor() -> None:
    """The cost check. A fix that also silences Sturnus is not a fix.

    Raising the root logger to DEBUG was never what made Sturnus's own
    DEBUG output visible -- `logging` checks a record against the
    *originating* logger's effective level, never against root's during
    propagation -- so leaving root at the third-party level costs nothing
    here. This test is what says so.
    """
    buffer = io.StringIO()
    configure_logging("worker", level="DEBUG", log_format="json", stream=buffer)

    logging.getLogger("sturnus.application.worker").debug("a Sturnus debug line")

    assert any(line["msg"] == "a Sturnus debug line" for line in lines(buffer)), (
        "the third-party floor swallowed Sturnus's own DEBUG output"
    )


@pytest.mark.usefixtures("restored_logging")
def test_turning_the_third_party_knob_up_cannot_reopen_it() -> None:
    """`STURNUS_LOG_THIRD_PARTY_LEVEL=DEBUG` is the obvious way to try.

    It is the variable whose *name* says it turns third-party logging up,
    it is settable from a Helm value, and before the floor it set the root
    logger to DEBUG and published the key.
    """
    buffer = io.StringIO()
    configure_logging(
        "worker",
        level="DEBUG",
        third_party_level="DEBUG",
        log_format="json",
        stream=buffer,
    )

    logging.getLogger("sturnus.test").debug("sturnus own debug line")
    drive_the_voice_handshake()

    assert_no_credential_reached_the_stream(buffer)


@pytest.mark.usefixtures("restored_logging")
def test_the_environment_cannot_reopen_it_either(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the environment, which is the only route an operator has.

    The arguments to `configure_logging` are a test convenience; in the
    three entrypoints it is called with none of them and reads
    `os.environ` itself. A fix proven only through the keyword arguments
    would not be proven on the path a Helm value takes.
    """
    monkeypatch.setitem(os.environ, "STURNUS_LOG_LEVEL", "DEBUG")
    monkeypatch.setitem(os.environ, "STURNUS_LOG_THIRD_PARTY_LEVEL", "DEBUG")

    buffer = io.StringIO()
    configure_logging("worker", log_format="json", stream=buffer)

    logging.getLogger("sturnus.test").debug("sturnus own debug line")
    drive_the_voice_handshake()

    assert_no_credential_reached_the_stream(buffer)


@pytest.mark.usefixtures("restored_logging")
def test_a_logger_already_set_to_debug_is_raised_back_to_the_floor() -> None:
    """A library that turns its own logger up at import time.

    Neither the root level nor `NEVER_BELOW` reaches such a logger: it
    carries an explicit level, so it inherits nothing, and its name is by
    definition not on a list written before it existed.
    """
    logging.getLogger("some_future_library.transport").setLevel(logging.DEBUG)

    buffer = io.StringIO()
    configure_logging("worker", level="DEBUG", log_format="json", stream=buffer)

    logging.getLogger("sturnus.test").debug("sturnus own debug line")
    drive_the_voice_handshake()

    assert_no_credential_reached_the_stream(buffer)


@pytest.mark.usefixtures("restored_logging")
def test_no_logger_outside_sturnus_sits_below_info() -> None:
    """The property, over every logger that exists -- not over a list.

    `logging.INFO` is written as a literal rather than as
    `THIRD_PARTY_FLOOR`, deliberately. Comparing the levels this function
    installed against the constant that installed them would hold for
    every possible value of that constant, `DEBUG` included, and would
    assert nothing.
    """
    configure_logging(
        "worker",
        level="DEBUG",
        third_party_level="DEBUG",
        log_format="json",
        stream=io.StringIO(),
    )

    offenders = sorted(
        name
        for name, existing in logging.Logger.manager.loggerDict.items()
        if isinstance(existing, logging.Logger)
        and not name.startswith("sturnus")
        and existing.getEffectiveLevel() < logging.INFO
    )
    assert not offenders, f"third-party loggers left below INFO: {offenders}"
    # The sweep must have had something to sweep. Without this the
    # assertion above passes on an empty dictionary.
    assert "discord.ext.voice_recv.gateway" in logging.Logger.manager.loggerDict


@pytest.mark.usefixtures("restored_logging")
def test_the_voice_connect_narrative_survives_the_deployed_default() -> None:
    """The half of `discord.voice_state` that is evidence, not leak.

    `voice_state.py` puts the whole connect narrative at **INFO** --
    "Starting voice handshake... (connection attempt %d)", "Voice handshake
    complete. Endpoint found: %s", "Timed out connecting to voice",
    "Disconnected from voice by discord, close code %d", "Successfully
    resumed voice connection" -- and formats no secret into any of them
    (read at the installed version: `secret_key` appears there only as an
    attribute that is assigned and awaited). All three entrypoints emitted
    those lines before this package existed, because they called
    `basicConfig(level=INFO)`, and they are the evidence base for
    `client.py`'s capture-failure cooldown and for telling apart the three
    ways capture fails.

    The leak sits one level below, at DEBUG, and this asserts both halves
    at once on the **rendered stream**: the INFO narrative is there and the
    DEBUG payload is not.

    Driven at the levels a production `values.yaml` actually carries --
    `STURNUS_LOG_THIRD_PARTY_LEVEL` defaults to `WARNING` (see
    `docs/operations.md` section 7.2), which is what makes a plain floor
    entry of `INFO` insufficient on its own and `NEVER_ABOVE` load-bearing.
    """
    buffer = io.StringIO()
    configure_logging(
        "bot", level="INFO", third_party_level="WARNING", log_format="json", stream=buffer
    )

    voice_state = logging.getLogger("discord.voice_state")
    voice_state.info("Starting voice handshake... (connection attempt %d)", 2)
    voice_state.info("Disconnected from voice by discord, close code %d.", 4014)
    voice_state.debug("Connection state changed to %s", session_description_payload())

    output = rendered(buffer)
    assert "Starting voice handshake" in output, (
        "the connect narrative was clamped away; capture failures are undiagnosable"
    )
    assert "close code 4014" in output
    assert SECRET_KEY_DIGITS not in output, "the DEBUG payload is the leak and must not appear"


@pytest.mark.usefixtures("restored_logging")
def test_a_logger_pinned_open_is_still_closed_at_debug() -> None:
    """`NEVER_ABOVE` opens a logger *to a level*, never to whatever is asked.

    The failure this guards against is an entry in `NEVER_ABOVE` being
    read as "exempt from the floor". It is not: the level it names is
    installed outright, so turning the third-party knob to DEBUG cannot
    make a pinned logger any louder than its pin.
    """
    buffer = io.StringIO()
    configure_logging(
        "bot", level="DEBUG", third_party_level="DEBUG", log_format="json", stream=buffer
    )

    assert logging.getLogger("discord.voice_state").getEffectiveLevel() == logging.INFO
    drive_the_voice_handshake()
    assert_no_credential_reached_the_stream(buffer)


def test_the_floor_is_info_and_the_reason_is_recorded() -> None:
    """Pinned against a literal so a later edit is a deliberate one.

    WARNING would be quieter and would also delete `discord.voice_state`'s
    INFO connect narrative -- "Starting voice handshake", "Voice handshake
    complete", "Timed out connecting to voice" -- which is the entire
    evidence base for diagnosing a capture failure. DEBUG is the leak.
    INFO is the only level that is both.
    """
    assert THIRD_PARTY_FLOOR == logging.INFO


@pytest.mark.usefixtures("restored_logging")
def test_an_operator_who_asked_for_more_is_told_they_did_not_get_it() -> None:
    """Silently ignoring the knob would send them looking in the wrong place.

    Someone who sets `STURNUS_LOG_THIRD_PARTY_LEVEL=DEBUG` during an
    incident and sees no new lines will conclude the variable is not
    wired up, and go and edit the deployment instead of reading section
    7.2. One line in the log is what stops that hour.
    """
    buffer = io.StringIO()
    configure_logging("worker", third_party_level="DEBUG", log_format="json", stream=buffer)

    clamped = [line for line in lines(buffer) if line["event"] == str(Event.LOG_LEVEL_CLAMPED)]
    assert len(clamped) == 1, f"expected exactly one clamp line, got {len(clamped)}"
    assert clamped[0]["level"] == "WARNING"
    assert clamped[0]["reason"] == "third_party_floor"


@pytest.mark.usefixtures("restored_logging")
def test_a_level_at_or_above_the_floor_is_not_reported_as_clamped() -> None:
    """The line must mean something. A warning on every start means nothing."""
    buffer = io.StringIO()
    configure_logging("worker", third_party_level="WARNING", log_format="json", stream=buffer)

    assert not [line for line in lines(buffer) if line["event"] == str(Event.LOG_LEVEL_CLAMPED)]


@pytest.mark.usefixtures("restored_logging")
def test_the_key_is_redacted_even_at_a_level_the_floor_permits() -> None:
    """The second lock, which does not depend on a level at all.

    The floor stops the record being emitted. It cannot help if a future
    `discord-ext-voice-recv` moves that same `pformat` call to WARNING or
    logs it from an exception handler -- and the installed version already
    has `log.info("WS payload has extra keys: %s", m)` a few lines below
    it. So the rendered value is scrubbed as well as suppressed, and this
    asserts the scrubbing on a record the floor deliberately lets through.
    """
    buffer = io.StringIO()
    configure_logging("worker", log_format="json", stream=buffer)

    logging.getLogger("discord.ext.voice_recv.gateway").warning(
        "Received op %s: \n%s", 4, pformat(session_description_payload(), compact=True)
    )

    output = rendered(buffer)
    assert output, "the record was suppressed, so this test measured nothing"
    assert SECRET_KEY_DIGITS not in output
    assert "«redacted:secret_value»" in output
    # The rest of the payload is what makes the line worth keeping.
    assert "aead_xchacha20_poly1305_rtpsize" in output


@pytest.mark.usefixtures("restored_logging")
def test_the_sweep_only_ever_raises_a_level() -> None:
    """A library that quieted itself down keeps its own choice.

    The sweep exists to close a hole, not to normalise levels. `boto3`
    calls `set_stream_logger` in some setups and libraries do turn
    themselves *down* on import; overwriting that with the floor would be
    this function making a noise decision it was never asked to make, and
    quieter than the floor was never the problem.
    """
    logging.getLogger("some_quiet_library").setLevel(logging.ERROR)

    configure_logging(
        "worker",
        level="DEBUG",
        third_party_level="DEBUG",
        log_format="json",
        stream=io.StringIO(),
    )

    assert logging.getLogger("some_quiet_library").level == logging.ERROR
