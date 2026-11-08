"""The redaction path, tested on values rather than on configuration.

Every test here offers something that must never travel -- a transcript, a
token, raw audio bytes -- and asserts it is absent from the output. That is
the shape the blocking gate in `docs/verification/end-to-end-checklist.md`
asks for, applied in CI instead of once by hand after a deploy.
"""

from __future__ import annotations

import pytest

from sturnus.domain.errors import DiagnosticSafeError
from sturnus.observability.fields import (
    ALLOWED_FIELDS,
    CREDENTIAL_NAMES,
    DENIED_NAMES,
    LOG_ONLY_FIELDS,
    METRIC_LABEL_FIELDS,
    SAFE_SPAN_ATTRIBUTES,
    span_attribute,
)
from sturnus.observability.redaction import (
    MAX_FIELD_CHARS,
    SAFE_MESSAGE_TYPES,
    safe_exception_message,
    scrub_fields,
    scrub_text,
    scrub_value,
)

#: A transcript-shaped string: the thing the whole design exists to keep out.
TRANSCRIPT = "so then I told my manager exactly what I thought of the reorg"

#: Discord bot token shape: 24 base64url chars, a 6-char segment, a 27+ tail.
#:
#: Assembled at import rather than written as one literal, and not because
#: the value is real -- the first segment is base64 for "123456789012345678"
#: and the tail is the alphabet. It is assembled because a literal of this
#: shape is what GitHub's push protection detects, and it detects it
#: correctly: a string that looks exactly like a bot token has no business
#: sitting in a repository, whether or not this particular one would open
#: anything. Splitting it keeps the runtime value identical -- which is the
#: whole point, since these tests exist to prove the redaction catches this
#: exact shape -- while leaving nothing in the file for a scanner, or a
#: reader, to mistake for a credential.
DISCORD_TOKEN = ".".join(
    ("MTIzNDU2Nzg5" + "MDEyMzQ1Njc4", "GaBcDe", "abcdefghijklmnopqrstuvwxyz" + "1234567")
)


def test_bytes_never_render_whatever_they_are() -> None:
    """The highest-value rule: audio is always `bytes`, so `bytes` never print."""
    pcm = b"\x00\x01" * 4096
    scrubbed = scrub_value(pcm)
    assert scrubbed == "<bytes len=8192>"
    assert "\x00" not in str(scrubbed)

    for kind in (bytearray(pcm), memoryview(pcm)):
        assert scrub_value(kind) == "<bytes len=8192>"


def test_a_wrapped_data_key_cannot_be_rendered_even_when_registered() -> None:
    """A key is bytes, so the bytes rule catches it with no name-matching."""
    wrapped = bytes(range(256)) * 2
    assert scrub_value(wrapped) == "<bytes len=512>"


@pytest.mark.parametrize(
    "secret,marker",
    [
        (DISCORD_TOKEN, "discord_token"),
        ("AKIA" + "IOSFODNN7EXAMPLE", "aws_access_key_id"),
        ("Authorization: Bearer " + "sk-abcdefghijklmnop", "bearer_token"),
        ("postgresql+asyncpg://sturnus:hunter2@db.internal/sturnus", "url_credentials"),
        ("X-Amz-Signature=deadbeefcafe1234", "aws_sigv4"),
    ],
)
def test_credentials_are_replaced_visibly(secret: str, marker: str) -> None:
    """A redaction that leaves no trace teaches nobody -- each names itself."""
    scrubbed = scrub_text(f"something failed: {secret}")
    assert f"«redacted:{marker}»" in scrubbed
    assert "hunter2" not in scrubbed
    if marker == "discord_token":
        assert DISCORD_TOKEN not in scrubbed


def test_the_pydantic_validation_error_leak_is_closed() -> None:
    """The exact string `StrictSettings` produces on a blank required value.

    Pydantic embeds the raw input dict in its message, so a blank
    `STURNUS_MASTER_KEY_ID` puts the first characters of the Discord token
    into the log store. This is that message, and the token must not survive
    it.
    """
    message = (
        "Value error, STURNUS_MASTER_KEY_ID is set but empty. "
        f"[type=value_error, input_value={{'discord_token': '{DISCORD_TOKEN}', "
        "'outline_redirect_uri': 'x'}, input_type=dict]"
    )
    scrubbed = scrub_text(message)
    assert DISCORD_TOKEN not in scrubbed
    assert "«redacted:discord_token»" in scrubbed
    # The diagnostic half survives: an operator still learns which variable.
    assert "STURNUS_MASTER_KEY_ID is set but empty" in scrubbed


def test_long_strings_are_capped() -> None:
    """Bounds the blast radius of anything the patterns miss."""
    scrubbed = scrub_text(TRANSCRIPT * 200)
    assert isinstance(scrubbed, str)
    assert len(scrubbed) < MAX_FIELD_CHARS + 64


def test_unregistered_fields_are_dropped_not_stripped() -> None:
    """Allowlist rebuild: a name nobody registered simply does not survive."""
    out = scrub_fields(
        {"job_id": 7, "transcript": TRANSCRIPT, "display_name": "Alice Example"},
        warn=False,
    )
    assert out == {"job_id": 7}
    assert TRANSCRIPT not in str(out)
    assert "Alice" not in str(out)


def test_an_object_renders_as_its_type_never_its_repr() -> None:
    """A repr is how an object holding a transcript prints itself into a line."""

    class Result:
        def __repr__(self) -> str:  # pragma: no cover - must never be called
            return f"Result({TRANSCRIPT!r})"

    assert scrub_value(Result()) == "<Result>"
    assert TRANSCRIPT not in str(scrub_value(Result()))


def test_exception_messages_are_withheld_unless_the_type_is_vouched_for() -> None:
    """The default is redaction; `SAFE_MESSAGE_TYPES` is the whole exemption."""
    unsafe = RuntimeError(f"failed rendering {TRANSCRIPT}")
    withheld = safe_exception_message(unsafe)
    assert TRANSCRIPT not in withheld
    assert "builtins.RuntimeError" in withheld

    # `OSError` messages are composed by the OS and the stdlib, not by us.
    assert "Errno 111" in safe_exception_message(ConnectionRefusedError(111, "Errno 111"))

    class Vouched(DiagnosticSafeError):
        pass

    assert "job 7 has no target" in safe_exception_message(Vouched("job 7 has no target"))


def test_the_exception_rule_is_the_one_sentry_uses() -> None:
    """One list, not two. Sentry aliases this tuple rather than restating it."""
    from sturnus.infrastructure.observability import SAFE_VALUE_TYPES

    assert SAFE_VALUE_TYPES is SAFE_MESSAGE_TYPES


def test_denied_names_and_registered_fields_never_overlap() -> None:
    """A name cannot be both registered and forbidden -- that would be ambiguous."""
    assert not (ALLOWED_FIELDS & DENIED_NAMES)


def test_log_only_fields_are_absent_from_spans_and_metrics() -> None:
    """The exclusion is computed, so it cannot be forgotten at a call site."""
    for field in LOG_ONLY_FIELDS:
        assert field in ALLOWED_FIELDS, "still loggable"
        assert span_attribute(field) not in SAFE_SPAN_ATTRIBUTES
        assert field not in METRIC_LABEL_FIELDS


def test_metric_labels_are_a_subset_of_the_registry() -> None:
    """Cardinality: only fixed literals and `guild_id` may become a label."""
    assert METRIC_LABEL_FIELDS <= ALLOWED_FIELDS
    assert "session_id" not in METRIC_LABEL_FIELDS
    assert "job_id" not in METRIC_LABEL_FIELDS


def test_a_pretty_printed_voice_secret_key_is_redacted() -> None:
    """The leaked value, in the shape it actually leaks in.

    `discord/ext/voice_recv/gateway.py:57` renders the op-4 payload with
    `pformat`, so the Discord voice secret key reaches a log record as
    thirty-two small integers in a list. It matches none of the
    shape-based patterns -- it is not base64, not `AKIA…`, not a bot
    token -- and it is inside `record.msg` rather than in any field the
    allowlist governs. The only handle on it is the name it is assigned
    to.
    """
    rendered = (
        "Received op 4: \n{'dave_protocol_version': 0,\n"
        " 'mode': 'aead_xchacha20_poly1305_rtpsize',\n"
        " 'secret_key': [31337, 31338, 31339, 31340],\n 'ssrc': 12345}"
    )
    scrubbed = scrub_text(rendered)

    assert "31337" not in scrubbed
    assert "«redacted:secret_value»" in scrubbed
    # The name survives, so an operator can tell a redaction from a field
    # that was simply never emitted -- and the rest of the payload, which
    # is what makes the line worth having, is untouched.
    assert "secret_key" in scrubbed
    assert "'ssrc': 12345" in scrubbed


def test_the_reader_spelling_of_the_same_key_is_redacted_too() -> None:
    """`reader.py`'s is `secret_key=%s` with the value running to the newline.

    A pattern that stopped at the first token boundary would leave most of
    a key on the line, which is not a smaller breach than all of it.
    """
    scrubbed = scrub_text("CryptoError details:\n  data=b'x'\n  secret_key=31337313373133731337")
    assert "31337" not in scrubbed
    assert scrubbed.endswith("secret_key=«redacted:secret_value»")


@pytest.mark.parametrize(
    "rendered",
    [
        "token: CANARYSECRET",
        "'token': 'CANARYSECRET'",
        'password="CANARYSECRET"',
        "data_key=CANARYSECRET",
        "client_secret: CANARYSECRET",
        "database_url=postgresql://u:CANARYSECRET@h/d",
    ],
)
def test_a_credential_name_assigned_anything_is_redacted(rendered: str) -> None:
    """Every spelling a third-party formatter is likely to produce."""
    assert "CANARYSECRET" not in scrub_text(rendered)


def test_the_credential_rule_leaves_ordinary_prose_alone() -> None:
    """Over-redaction is the right failure direction; noise is still a cost.

    The rule fires on `<credential name><separator><value>`, not on the
    word. A line that merely mentions a token stays readable, which is
    what keeps the control from being disabled by whoever gets tired of
    it.
    """
    prose = "the token was rejected by Outline and the password prompt never appeared"
    assert scrub_text(prose) == prose


def test_the_credential_names_are_the_denied_names_and_not_a_second_list() -> None:
    """One registry, split -- not two lists that can disagree.

    `fields.DENIED_NAMES` is the union of the payload half and the
    credential half, so a name added to either is a name the AST rule in
    `tests/test_logging_discipline.py` also refuses.
    """
    assert CREDENTIAL_NAMES < DENIED_NAMES
    assert "secret_key" in CREDENTIAL_NAMES
    # The payload half stays out of the text patterns on purpose: `text:`
    # and `body:` occur in English sentences, and a rule that redacts the
    # word after them is a rule someone will delete.
    assert not CREDENTIAL_NAMES & {"text", "body", "transcript", "display_name"}
