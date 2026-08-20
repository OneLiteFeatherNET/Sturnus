"""What must never reach Sentry, asserted against the hook that stops it.

Every test here names a concrete leak route rather than a code path, because
the value of `sturnus.infrastructure.observability` is not that it runs, it
is that specific content does not leave the cluster. A test that only checked
`sentry_sdk.init` had been called would pass with the scrubbing deleted.

The sentinels below are the four things that must not travel -- a line of
transcript, a Discord display name, a master key, an OAuth authorization
code. Any of them appearing in the serialised event is the failure.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
from collections.abc import Iterator
from typing import Any, cast

import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from sturnus.config import SentrySettings
from sturnus.domain.errors import DiagnosticSafeError
from sturnus.infrastructure.observability import (
    REDACTED,
    SAFE_EVENT_KEYS,
    SAFE_EXCEPTION_VALUE_KEYS,
    drop_breadcrumb,
    drop_transaction,
    init_sentry,
    scrub_event,
)

# Deliberately unlike each other and unlike anything else in the payload, so
# a substring search over the serialised event cannot pass by accident.
TRANSCRIPT = "SENTINEL-so-then-I-told-the-whole-channel-about-my-divorce"
SPEAKER = "SENTINEL-display-name-Alice"
MASTER_KEY = "SENTINEL-master-key-material"
OAUTH_CODE = "SENTINEL-outline-authorization-code"
# Not a sentinel string but a real one: `OSError.__str__` renders whatever
# path the OS handed back, and `RecordingFileStore` names its files
# `<recording_dir>/session-<session_id>/<discord_user_id>.wav`. The Discord
# user id and the session id together say who was recorded and when, so the
# literal shape is what has to be searched for.
RECORDING_PATH = "/data/recordings/session-8f3c/198273645102938471.wav"
ALL_SENTINELS = (TRANSCRIPT, SPEAKER, MASTER_KEY, OAUTH_CODE, RECORDING_PATH)


def _rendered(event: object) -> str:
    """The event as it would go over the wire, for substring assertions.

    Asserting on the serialised form rather than on individual keys is the
    point: a leak that hides in a nested field nobody thought to check still
    ships, and still fails this.
    """
    return json.dumps(event, default=repr)


def _assert_clean(event: object) -> None:
    rendered = _rendered(event)
    for sentinel in ALL_SENTINELS:
        assert sentinel not in rendered, f"{sentinel} survived scrubbing: {rendered}"


def _capture(exc: BaseException) -> tuple[dict[str, Any], dict[str, Any]]:
    """An event/hint pair shaped like the SDK's, without a live client."""
    event = {
        "exception": {
            "values": [
                {
                    "type": type(exc).__name__,
                    "value": str(exc),
                    "stacktrace": {"frames": [{"filename": "x.py", "function": "f"}]},
                }
            ]
        }
    }
    return event, {"exc_info": (type(exc), exc, None)}


def test_unknown_event_fields_are_dropped() -> None:
    """The allowlist's whole reason for existing.

    `breadcrumbs`, `extra`, `request` and `user` are today's leak routes;
    `some_future_sdk_field` stands for the ones a later sentry-sdk invents,
    which a denylist would start forwarding the moment it appeared and no
    test would notice. Rebuilding the event means the new field is absent
    until someone puts it in `SAFE_EVENT_KEYS` on purpose.
    """
    event = {
        "event_id": "abc",
        "level": "error",
        "breadcrumbs": {"values": [{"message": f"transcribed {TRANSCRIPT}"}]},
        "extra": {"speaker": SPEAKER},
        "request": {"query_string": f"code={OAUTH_CODE}&state=x"},
        "user": {"id": SPEAKER},
        "message": TRANSCRIPT,
        "some_future_sdk_field": TRANSCRIPT,
    }

    out = scrub_event(cast(Any, event), {})

    assert out is not None
    assert set(out) <= SAFE_EVENT_KEYS
    assert out["event_id"] == "abc"
    _assert_clean(out)


def test_local_variables_are_never_attached() -> None:
    """Frame `vars` hold the master key and the transcript segments.

    `include_local_variables=False` already stops them being collected. This
    is the second, independent control: even if that option is ever flipped
    back on -- during an incident, "just for a bit" -- the frames are rebuilt
    without `vars` and the key does not travel.
    """
    event = {
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "value": "boom",
                    "stacktrace": {
                        "frames": [
                            {
                                "filename": "crypto.py",
                                "function": "unwrap",
                                "lineno": 12,
                                "context_line": "    return aead.decrypt(...)",
                                "vars": {"master_key": MASTER_KEY, "text": TRANSCRIPT},
                            }
                        ]
                    },
                }
            ]
        }
    }

    out = scrub_event(cast(Any, event), {})

    assert out is not None
    frame = out["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert "vars" not in frame
    assert frame["context_line"] == "    return aead.decrypt(...)"
    _assert_clean(out)


def test_exception_message_is_redacted_by_default() -> None:
    """Nothing structural separates a transcript from an errno string."""
    out = scrub_event(*cast(Any, _capture(RuntimeError(f"failed on {TRANSCRIPT}"))))

    assert out is not None
    assert out["exception"]["values"][0]["value"] == REDACTED
    assert out["exception"]["values"][0]["type"] == "RuntimeError"
    _assert_clean(out)


def test_os_error_errno_and_strerror_are_kept() -> None:
    """The messages that matter for a process talking to four networks, and
    which the C library composes from a fixed table rather than we do."""
    out = scrub_event(*cast(Any, _capture(ConnectionRefusedError(111, "Connection refused"))))

    assert out is not None
    assert out["exception"]["values"][0]["value"] == "[Errno 111] Connection refused"


def test_os_error_does_not_ship_the_filename() -> None:
    """`OSError` is vouched for because the OS writes its errno table, not
    because it writes the filename.

    `str(FileNotFoundError(2, "...", path))` appends `: '<path>'`, and the
    path an `OSError` here is most likely to name identifies a Discord user
    and a session. The errno survives -- it is the diagnostic value -- and
    the trailing `: <redacted>` keeps the operator informed that a path was
    involved, to be read with `kubectl logs`.
    """
    out = scrub_event(
        *cast(
            Any,
            _capture(FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), RECORDING_PATH)),
        )
    )

    assert out is not None
    assert out["exception"]["values"][0]["value"] == (
        f"[Errno {errno.ENOENT}] {os.strerror(errno.ENOENT)}: {REDACTED}"
    )
    _assert_clean(out)


def test_os_error_does_not_ship_either_filename_of_a_two_path_call() -> None:
    """`filename2` is the second path of an `os.rename`/`os.link` failure.

    Both halves name a recording, so redacting only `filename` would ship the
    other one.
    """
    exc = OSError(
        errno.EXDEV, os.strerror(errno.EXDEV), RECORDING_PATH, None, f"{RECORDING_PATH}.enc"
    )

    out = scrub_event(*cast(Any, _capture(exc)))

    assert out is not None
    assert out["exception"]["values"][0]["value"].endswith(f": {REDACTED} -> {REDACTED}")
    _assert_clean(out)


def test_a_free_form_os_error_message_is_redacted() -> None:
    """The single-argument form carries no errno table entry at all.

    `OSError("cannot open <path>")` is a string a library wrote, structurally
    indistinguishable from `RuntimeError("failed on <transcript>")`, so the
    `OSError` opt-out does not reach it. Type and stack trace still travel.
    """
    out = scrub_event(*cast(Any, _capture(OSError(f"cannot open {RECORDING_PATH}"))))

    assert out is not None
    assert out["exception"]["values"][0]["value"] == REDACTED
    assert out["exception"]["values"][0]["type"] == "OSError"
    _assert_clean(out)


def test_diagnostic_safe_error_message_is_kept() -> None:
    class SchemaMissing(DiagnosticSafeError):
        pass

    out = scrub_event(*cast(Any, _capture(SchemaMissing("table oauth_state is missing"))))

    assert out is not None
    assert out["exception"]["values"][0]["value"] == "table oauth_state is missing"


def test_chained_exception_values_are_each_judged() -> None:
    """A chain is scrubbed per exception, not all-or-nothing.

    Sentry orders `exception.values` oldest-first with the raised exception
    last, so the pairing has to be got the right way round; if it were
    reversed, this test would show the safe message redacted and the unsafe
    one sent.
    """

    class Inner(DiagnosticSafeError):
        pass

    try:
        try:
            raise Inner("job 41 has no participants")
        except Inner as inner:
            raise RuntimeError(f"assembling {TRANSCRIPT}") from inner
    except RuntimeError as raised:
        event = {
            "exception": {
                "values": [
                    {"type": "Inner", "value": str(raised.__cause__)},
                    {"type": "RuntimeError", "value": str(raised)},
                ]
            }
        }
        hint = {"exc_info": (type(raised), raised, raised.__traceback__)}

    out = scrub_event(cast(Any, event), cast(Any, hint))

    assert out is not None
    values = out["exception"]["values"]
    assert values[0]["value"] == "job 41 has no participants"
    assert values[1]["value"] == REDACTED
    _assert_clean(out)


def test_an_unpairable_chain_redacts_everything() -> None:
    """Fail closed: an event whose `exc_info` does not line up with the
    values the SDK produced gives no basis for vouching for any message."""
    event = {
        "exception": {
            "values": [
                {"type": "OSError", "value": f"a {TRANSCRIPT}"},
                {"type": "OSError", "value": "b"},
            ]
        }
    }

    out = scrub_event(cast(Any, event), {})

    assert out is not None
    assert [v["value"] for v in out["exception"]["values"]] == [REDACTED, REDACTED]
    _assert_clean(out)


def test_exception_values_are_rebuilt_from_an_allowlist() -> None:
    """The one nesting level that used to be copied wholesale.

    `exception.values` entries were `dict(value)` with `value` overwritten,
    so a field a later sentry-sdk starts attaching -- a `raw_stacktrace`, a
    `data` blob on the mechanism -- would have been forwarded, which is the
    exact failure mode `SAFE_EVENT_KEYS` exists to prevent one level up.
    """
    event = {
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "module": "sturnus.application.assembly",
                    "value": "boom",
                    "mechanism": {
                        "type": "logging",
                        "handled": True,
                        "data": {"speaker": SPEAKER},
                    },
                    "raw_stacktrace": {"frames": [{"vars": {"text": TRANSCRIPT}}]},
                    "some_future_sdk_field": OAUTH_CODE,
                }
            ]
        }
    }

    out = scrub_event(cast(Any, event), {})

    assert out is not None
    value = out["exception"]["values"][0]
    assert set(value) <= SAFE_EXCEPTION_VALUE_KEYS
    assert value["module"] == "sturnus.application.assembly"
    assert value["mechanism"] == {"type": "logging", "handled": True}
    _assert_clean(out)


def test_logentry_keeps_only_the_format_string() -> None:
    """`log.error("job %s failed", transcript)` renders the transcript into
    `formatted` and puts it verbatim into `params`; only the uninterpolated
    template, which ruff's `G` ruleset keeps a source literal, survives."""
    event = {
        "logger": "sturnus.application.worker",
        "logentry": {
            "message": "job %s failed",
            "formatted": f"job {TRANSCRIPT} failed",
            "params": [TRANSCRIPT],
        },
    }

    out = scrub_event(cast(Any, event), {})

    assert out is not None
    assert out["logentry"] == {"message": "job %s failed"}
    _assert_clean(out)


@pytest.mark.parametrize(
    "logger_name",
    ["asyncio", "discord.player", "aiohttp.server", "root", "sturnusx.thing", ""],
)
def test_logentry_from_a_foreign_logger_is_dropped(logger_name: str) -> None:
    """`logentry.message` is only a template because ruff says so, and ruff
    only says so about this repository.

    `record.msg` is a reviewable source literal for the log calls written
    here; for every other producer it is whatever that producer composed.
    `asyncio`'s default exception handler is the concrete one -- it builds
    its `msg` out of `repr(future)`, which embeds the raised exception's
    message -- and `sturnusx` is the reason the prefix test is dotted.
    """
    event = {
        "logger": logger_name,
        "logentry": {
            "message": f"Task exception was never retrieved\nfuture: <Task finished "
            f"coro=<transcribe()> exception=RuntimeError('whisper failed on "
            f"{TRANSCRIPT}')>",
            "params": (),
        },
    }

    out = scrub_event(cast(Any, event), {})

    assert out is not None
    assert "logentry" not in out
    _assert_clean(out)


def test_logentry_without_a_logger_is_dropped() -> None:
    """Fail closed: an event with no `logger` names no producer to vouch for."""
    event = {"logentry": {"message": f"failed on {TRANSCRIPT}"}}

    out = scrub_event(cast(Any, event), {})

    assert out is not None
    assert "logentry" not in out
    _assert_clean(out)


def test_tags_are_rebuilt_from_an_allowlist() -> None:
    """`sentry_sdk.set_tag` is global and searchable; one added later with a
    speaker name in it must not ride along on every event."""
    event = {"tags": {"component": "worker", "speaker": SPEAKER}}

    out = scrub_event(cast(Any, event), {})

    assert out is not None
    assert out["tags"] == {"component": "worker"}
    _assert_clean(out)


def test_contexts_are_rebuilt_from_an_allowlist() -> None:
    event = {"contexts": {"runtime": {"name": "CPython"}, "session": {"text": TRANSCRIPT}}}

    out = scrub_event(cast(Any, event), {})

    assert out is not None
    assert set(out["contexts"]) == {"runtime"}
    _assert_clean(out)


@pytest.mark.parametrize(
    "crumb",
    [
        {"category": "x", "message": f"transcribed {TRANSCRIPT}"},
        {"category": "httplib", "data": {"url": f"https://s3/x?X-Amz-Signature={MASTER_KEY}"}},
        {"category": "subprocess", "message": "ffmpeg"},
    ],
)
def test_breadcrumbs_are_always_dropped(crumb: dict[str, Any]) -> None:
    assert drop_breadcrumb(cast(Any, crumb), {}) is None


def test_transactions_are_dropped() -> None:
    """`before_send` is never called for transactions, so span data (SQL text,
    full URLs) would route around every allowlist above."""
    assert drop_transaction(cast(Any, {"type": "transaction"}), {}) is None


def test_init_sentry_does_nothing_without_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optionality, asserted where it actually lives.

    `sentry_sdk.init(dsn=None)` builds a live client that patches
    `logging.Logger.callHandlers`, `sys.excepthook`, `threading.Thread.run`
    and `atexit`, and reports `is_active() is True` while sending nothing --
    so `is_active()` cannot be used to check this, and the SDK must not be
    entered at all. An operator without Sentry runs the byte-for-byte
    behaviour they ran before this module existed.
    """
    calls: list[object] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))
    monkeypatch.delenv("STURNUS_SENTRY_DSN", raising=False)

    assert init_sentry("worker") is False
    assert calls == []


def test_init_sentry_treats_a_blank_dsn_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """What a chart default of `STURNUS_SENTRY_DSN: ""` produces on every
    cluster that has not opted in."""
    calls: list[object] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))
    monkeypatch.setenv("STURNUS_SENTRY_DSN", "   ")

    assert init_sentry("bot") is False
    assert calls == []


def test_init_sentry_initialises_with_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))
    tags: list[tuple[str, str]] = []
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda key, value: tags.append((key, value)))

    assert init_sentry("link", SentrySettings(sentry_dsn="https://k@sentry.invalid/1")) is True
    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["include_local_variables"] is False
    assert kwargs["send_default_pii"] is False
    assert kwargs["auto_enabling_integrations"] is False
    assert kwargs["traces_sample_rate"] == 0.0
    assert kwargs["enable_logs"] is False
    assert kwargs["enable_metrics"] is False
    assert kwargs["before_send"] is scrub_event
    assert kwargs["before_breadcrumb"] is drop_breadcrumb
    assert kwargs["before_send_transaction"] is drop_transaction
    # Three deployments, one image: without this the issue list cannot say
    # which process failed.
    assert tags == [("component", "link")]


def test_a_malformed_dsn_disables_sentry_instead_of_crashing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo in optional telemetry must not stop the bot recording.

    `sentry_sdk.init` raises `BadDsn` on an unparseable DSN, and `init_sentry`
    is the first statement of all three `main()`s -- so unhandled it takes
    down bot, worker and link before their event loops start, and Kubernetes
    turns that into a CrashLoopBackOff of the whole system because a value
    that only controls error reporting was mistyped.
    """
    caplog.set_level(logging.INFO, logger="sturnus.infrastructure.observability")

    assert init_sentry("worker", SentrySettings(sentry_dsn="not-a-dsn")) is False

    # Nothing half-built is left behind: no `callHandlers` patch, no
    # `excepthook`, no atexit flush -- the same state as having no DSN.
    assert sentry_sdk.is_initialized() is False
    assert "DISABLED" in caplog.text, "an operator must be able to see Sentry is off, and why"
    assert "STURNUS_SENTRY_DSN" in caplog.text
    assert "not-a-dsn" not in caplog.text


def test_a_rejected_dsn_does_not_put_its_key_in_the_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The "why" must not become its own leak.

    A DSN embeds a public key, but it is still a credential-shaped value that
    ends up in `kubectl logs` and in whatever ships those logs onward, so the
    failure is reported by the SDK's own message -- which names the scheme,
    the hostname or the project path -- and never by echoing the input.
    """
    caplog.set_level(logging.ERROR, logger="sturnus.infrastructure.observability")

    settings = SentrySettings(sentry_dsn=f"https://{MASTER_KEY}@sentry.invalid/not-a-project")
    assert init_sentry("bot", settings) is False

    assert sentry_sdk.is_initialized() is False
    _assert_clean(caplog.text)


class _CapturingTransport(Transport):
    """Collects envelopes instead of sending them.

    A `Transport` subclass rather than the callable-transport shorthand: that
    shorthand is deprecated in 2.68 and warns, and this test exists precisely
    to keep working across the SDK bumps Renovate will open.
    """

    def __init__(self, captured: list[dict[str, Any]]) -> None:
        super().__init__()
        self.captured = captured

    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.captured.append(cast(Any, event))

    def flush(self, timeout: float, callback: Any = None) -> None:
        del timeout, callback
        return None

    def kill(self) -> None:
        return None


@pytest.fixture
def sentry_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """A client built by `init_sentry` itself, whose events land in a list.

    The option set is not restated here. It used to be, and that made the
    end-to-end tests below assert against a copy of the configuration rather
    than against the configuration the three processes run: an option removed
    from `init_sentry` -- `before_send`, say -- would have left every test
    here green. The only thing injected is the transport, by wrapping
    `sentry_sdk.init` for the duration of the test; everything else, up to
    and including the `component` tag, comes from the shipped code path.

    `sentry_sdk.init` patches `logging.Logger.callHandlers` process-wide and
    the patch is not undone by a later `init`, so the teardown detaches the
    client from the global scope; the patched handler then finds a
    non-recording client and no other test starts emitting breadcrumbs.
    """
    captured: list[dict[str, Any]] = []
    real_init = sentry_sdk.init

    def init_with_capturing_transport(**kwargs: Any) -> Any:
        return real_init(transport=_CapturingTransport(captured), **kwargs)

    monkeypatch.setattr(sentry_sdk, "init", init_with_capturing_transport)
    settings = SentrySettings(sentry_dsn="https://sentinelkey@sentry.invalid/1")
    assert init_sentry("worker", settings) is True

    try:
        yield captured
    finally:
        sentry_sdk.flush()
        sentry_sdk.get_global_scope().set_client(None)


def test_a_real_capture_carries_no_content(sentry_transport: list[dict[str, Any]]) -> None:
    """End-to-end through `init_sentry` itself.

    The unit tests above feed hand-built events, which proves the hook but
    not that the hook is reached with these options; this drives a real
    `log.exception` through a client `init_sentry` built and asserts on what
    the transport receives. It is the test that would catch an option name
    that silently does nothing.
    """
    log = logging.getLogger("sturnus.test.observability")
    try:
        raise RuntimeError(f"assembling failed for {TRANSCRIPT}")
    except RuntimeError:
        log.exception("job %s failed", TRANSCRIPT, extra={"speaker": SPEAKER})

    sentry_sdk.flush()

    assert len(sentry_transport) == 1
    event = sentry_transport[0]
    assert event["tags"] == {"component": "worker"}
    assert event["logentry"] == {"message": "job %s failed"}
    assert event["exception"]["values"][0]["value"] == REDACTED
    assert set(event) <= SAFE_EVENT_KEYS
    _assert_clean(event)


def test_an_asyncio_handler_message_carries_no_content(
    sentry_transport: list[dict[str, Any]],
) -> None:
    """The leak route that went around `exception.values` entirely.

    `asyncio`'s default exception handler does not log a `%s` template. It
    composes `msg` itself, one line per context key, each rendered with
    `repr` -- and `repr` of a finished `Task` embeds the exception it holds.
    A coroutine that raised `RuntimeError(f"whisper failed on {segment}")`
    and was never awaited therefore produced an event whose
    `exception.values[0].value` was correctly `<redacted>` while
    `logentry.message` carried the segment verbatim.

    Asserted on what the transport receives, because the scrubber called
    directly was never the thing that was wrong.
    """

    async def transcribe() -> None:
        raise RuntimeError(f"whisper failed on {TRANSCRIPT}")

    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(transcribe())
        loop.run_until_complete(asyncio.wait([task]))
        # Exactly the call `Task.__del__` makes for a task nobody retrieved
        # the exception from, with the context asyncio itself builds.
        loop.call_exception_handler(
            {
                "message": "Task exception was never retrieved",
                "exception": task.exception(),
                "future": task,
            }
        )
    finally:
        loop.close()

    sentry_sdk.flush()

    assert len(sentry_transport) == 1
    event = sentry_transport[0]
    assert event["logger"] == "asyncio"
    assert "logentry" not in event, (
        "a message this repository did not compose cannot be vouched for by "
        "ruff's G ruleset, which only covers this repository's own log calls"
    )
    assert event["exception"]["values"][0]["value"] == REDACTED
    assert event["exception"]["values"][0]["type"] == "RuntimeError"
    assert set(event) <= SAFE_EVENT_KEYS
    _assert_clean(event)


def test_a_real_os_error_does_not_ship_the_recording_path(
    sentry_transport: list[dict[str, Any]],
) -> None:
    """`OSError` is on the safe side of the default, and its `__str__` is not.

    The path a `FileNotFoundError` names in this codebase is
    `<recording_dir>/session-<session_id>/<discord_user_id>.wav`: a Discord
    user id and a session id, i.e. who was recorded and when. What survives
    is the errno, which is what an operator actually diagnoses with.
    """
    log = logging.getLogger("sturnus.test.observability")
    try:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), RECORDING_PATH)
    except FileNotFoundError:
        log.exception("opening the recording for session %s failed", "8f3c")

    sentry_sdk.flush()

    assert len(sentry_transport) == 1
    event = sentry_transport[0]
    assert event["exception"]["values"][0]["type"] == "FileNotFoundError"
    assert event["exception"]["values"][0]["value"] == (
        f"[Errno {errno.ENOENT}] {os.strerror(errno.ENOENT)}: {REDACTED}"
    )
    _assert_clean(event)
