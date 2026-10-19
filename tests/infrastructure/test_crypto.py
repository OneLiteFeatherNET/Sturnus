import os
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from sturnus.infrastructure.crypto import (
    ARTEFACT_MAGIC,
    CHUNK_SIZE,
    MAGIC,
    KeyWrapper,
    decrypt_file,
    encrypt_file,
    is_sealed_artefact,
    open_artefact,
    seal_artefact,
    secret_context,
)

MASTER = b"0" * 32


def wrapper() -> KeyWrapper:
    return KeyWrapper(master_key=MASTER, key_id="k1")


def test_data_key_round_trips_through_the_master_key() -> None:
    w = wrapper()
    key = w.new_data_key()
    assert len(key.plaintext) == 32
    assert w.unwrap(key.wrapped) == key.plaintext


def test_each_data_key_is_distinct() -> None:
    w = wrapper()
    assert w.new_data_key().plaintext != w.new_data_key().plaintext


def test_a_wrong_master_key_cannot_unwrap() -> None:
    wrapped = wrapper().new_data_key().wrapped
    other = KeyWrapper(master_key=b"1" * 32, key_id="k1")
    with pytest.raises(InvalidTag):
        other.unwrap(wrapped)


def test_file_round_trips(tmp_path: Path) -> None:
    plain = tmp_path / "audio.wav"
    plain.write_bytes(os.urandom(CHUNK_SIZE * 2 + 1234))
    key = wrapper().new_data_key().plaintext

    encrypt_file(plain, tmp_path / "audio.enc", key)
    decrypt_file(tmp_path / "audio.enc", tmp_path / "audio.out", key)

    assert (tmp_path / "audio.out").read_bytes() == plain.read_bytes()


def test_ciphertext_does_not_contain_the_plaintext(tmp_path: Path) -> None:
    marker = b"SPOKEN-WORDS-MARKER" * 100
    plain = tmp_path / "a.wav"
    plain.write_bytes(marker)
    key = wrapper().new_data_key().plaintext

    encrypt_file(plain, tmp_path / "a.enc", key)
    assert marker not in (tmp_path / "a.enc").read_bytes()


def test_empty_file_round_trips(tmp_path: Path) -> None:
    """A participant who never speaks produces a zero-length recording."""
    plain = tmp_path / "empty.wav"
    plain.write_bytes(b"")
    key = wrapper().new_data_key().plaintext

    encrypt_file(plain, tmp_path / "empty.enc", key)
    decrypt_file(tmp_path / "empty.enc", tmp_path / "empty.out", key)
    assert (tmp_path / "empty.out").read_bytes() == b""


def test_tampering_is_detected(tmp_path: Path) -> None:
    """AES-GCM authenticates; a modified ciphertext must not decrypt silently."""
    plain = tmp_path / "b.wav"
    plain.write_bytes(os.urandom(4096))
    key = wrapper().new_data_key().plaintext
    encrypted = tmp_path / "b.enc"
    encrypt_file(plain, encrypted, key)

    data = bytearray(encrypted.read_bytes())
    data[-1] ^= 0xFF
    encrypted.write_bytes(bytes(data))

    with pytest.raises(InvalidTag):
        decrypt_file(encrypted, tmp_path / "b.out", key)


def test_a_wrong_data_key_cannot_decrypt(tmp_path: Path) -> None:
    plain = tmp_path / "c.wav"
    plain.write_bytes(os.urandom(4096))
    w = wrapper()
    encrypt_file(plain, tmp_path / "c.enc", w.new_data_key().plaintext)
    with pytest.raises(InvalidTag):
        decrypt_file(tmp_path / "c.enc", tmp_path / "c.out", w.new_data_key().plaintext)


def test_two_encryptions_of_the_same_file_differ(tmp_path: Path) -> None:
    """A fresh nonce prefix per file, so identical audio yields different bytes."""
    plain = tmp_path / "d.wav"
    plain.write_bytes(b"x" * 8192)
    key = wrapper().new_data_key().plaintext
    encrypt_file(plain, tmp_path / "d1.enc", key)
    encrypt_file(plain, tmp_path / "d2.enc", key)
    assert (tmp_path / "d1.enc").read_bytes() != (tmp_path / "d2.enc").read_bytes()


def test_a_wrap_with_no_context_still_unwraps_with_no_context() -> None:
    """The existing callers pass nothing, and nothing changes for them.

    Every audio data key ever written was wrapped without associated
    data. Adding the parameter had to leave that spelling byte-compatible
    or the master key would stop opening the recordings it wrapped.
    """
    w = wrapper()
    wrapped = w.wrap(b"a secret")
    assert w.unwrap(wrapped) == b"a secret"


def test_a_secret_wrapped_under_a_context_needs_that_context_back() -> None:
    w = wrapper()
    wrapped = w.wrap(b"a secret", secret_context("export", 1))
    assert w.unwrap(wrapped, secret_context("export", 1)) == b"a secret"


def test_a_secret_wrapped_under_a_context_does_not_open_under_another() -> None:
    """What binds a wrapped blob to the row it sits in.

    AES-GCM's associated data is authenticated but not encrypted: it does
    not hide anything, it makes the tag depend on it. So a blob wrapped
    for guild 1 and pasted into guild 2's row fails to authenticate
    rather than decrypting into a credential guild 2 was never given.
    """
    w = wrapper()
    wrapped = w.wrap(b"a secret", secret_context("export", 1))
    with pytest.raises(InvalidTag):
        w.unwrap(wrapped, secret_context("export", 2))


def test_a_secret_wrapped_for_one_purpose_does_not_open_under_another() -> None:
    """One guild holds several kinds of secret, and they are not each other.

    Binding to the guild alone would leave a Confluence token and an
    OAuth client secret interchangeable within the guild that owns both.
    """
    w = wrapper()
    wrapped = w.wrap(b"a secret", secret_context("export", 1))
    with pytest.raises(InvalidTag):
        w.unwrap(wrapped, secret_context("oauth", 1))


def test_a_context_bound_secret_does_not_open_without_a_context() -> None:
    """Dropping the argument must fail, not fall back to the unbound form."""
    w = wrapper()
    wrapped = w.wrap(b"a secret", secret_context("export", 1))
    with pytest.raises(InvalidTag):
        w.unwrap(wrapped)


def test_an_unbound_secret_does_not_open_under_a_context() -> None:
    """And the other direction, so neither side can be added by accident."""
    w = wrapper()
    wrapped = w.wrap(b"a secret")
    with pytest.raises(InvalidTag):
        w.unwrap(wrapped, secret_context("export", 1))


def test_a_data_key_can_be_bound_too() -> None:
    """The generator takes the same parameter, so nothing has two shapes."""
    w = wrapper()
    key = w.new_data_key(secret_context("export", 1))
    assert w.unwrap(key.wrapped, secret_context("export", 1)) == key.plaintext


# ---------------------------------------------------------------------------
# The sealed artefact envelope
# ---------------------------------------------------------------------------
#
# A rendered protocol, sealed in one piece and carrying the wrapped key
# that opens it. The properties worth pinning are the two that make this a
# different shape from `encrypt_file`: the object is self-contained, and
# the key inside it is bound to a context the reader has to supply from
# somewhere other than the object.


def test_a_sealed_artefact_round_trips() -> None:
    w = wrapper()
    aad = secret_context("export-artefact", 7)
    sealed = seal_artefact(b"# Minutes\n", w, aad)
    assert open_artefact(sealed, w, aad) == b"# Minutes\n"


def test_a_sealed_artefact_does_not_contain_its_plaintext() -> None:
    w = wrapper()
    sealed = seal_artefact(b"Anna said something", w, secret_context("export-artefact", 7))
    assert b"Anna said something" not in sealed


def test_a_sealed_artefact_names_itself() -> None:
    """The envelope is self-describing so a reader can tell it apart from
    the plaintext artefacts written before it existed, and from the chunked
    recording format, without being told which it is holding."""
    sealed = seal_artefact(b"body", wrapper(), secret_context("export-artefact", 7))
    assert sealed.startswith(ARTEFACT_MAGIC)
    assert is_sealed_artefact(sealed)
    assert not is_sealed_artefact(b"# Minutes\n")
    assert not is_sealed_artefact(b"")


def test_two_seals_of_one_body_differ() -> None:
    """A fresh data key per artefact, so two objects never share a key and
    a nonce is never reused under one."""
    w = wrapper()
    aad = secret_context("export-artefact", 7)
    assert seal_artefact(b"body", w, aad) != seal_artefact(b"body", w, aad)


def test_an_artefact_sealed_for_one_guild_does_not_open_for_another() -> None:
    """The whole point of binding the key to a context the object does not
    carry: an object copied onto another guild's key fails to authenticate
    instead of handing that guild somebody else's meeting."""
    w = wrapper()
    sealed = seal_artefact(b"body", w, secret_context("export-artefact", 7))
    with pytest.raises(InvalidTag):
        open_artefact(sealed, w, secret_context("export-artefact", 8))


def test_an_artefact_does_not_open_under_another_purpose() -> None:
    w = wrapper()
    sealed = seal_artefact(b"body", w, secret_context("export-artefact", 7))
    with pytest.raises(InvalidTag):
        open_artefact(sealed, w, secret_context("export-target", 7))


def test_an_artefact_does_not_open_under_another_master_key() -> None:
    sealed = seal_artefact(b"body", wrapper(), secret_context("export-artefact", 7))
    other = KeyWrapper(master_key=b"1" * 32, key_id="k1")
    with pytest.raises(InvalidTag):
        open_artefact(sealed, other, secret_context("export-artefact", 7))


def test_a_tampered_body_does_not_open() -> None:
    w = wrapper()
    aad = secret_context("export-artefact", 7)
    sealed = bytearray(seal_artefact(b"body", w, aad))
    sealed[-1] ^= 0xFF
    with pytest.raises(InvalidTag):
        open_artefact(bytes(sealed), w, aad)


def test_something_that_is_not_an_artefact_is_refused_before_any_key_is_touched() -> None:
    w = wrapper()
    with pytest.raises(ValueError):
        open_artefact(b"# Minutes\n", w, secret_context("export-artefact", 7))


def test_a_truncated_artefact_is_refused() -> None:
    w = wrapper()
    aad = secret_context("export-artefact", 7)
    sealed = seal_artefact(b"body", w, aad)
    with pytest.raises(ValueError):
        open_artefact(sealed[: len(ARTEFACT_MAGIC) + 1], w, aad)


def test_an_artefact_claiming_a_longer_key_than_it_holds_is_refused() -> None:
    """A length prefix read out of an object is a length an attacker
    chooses, so it is checked against what is actually there."""
    w = wrapper()
    aad = secret_context("export-artefact", 7)
    sealed = bytearray(seal_artefact(b"body", w, aad))
    sealed[len(ARTEFACT_MAGIC) : len(ARTEFACT_MAGIC) + 2] = b"\xff\xff"
    with pytest.raises(ValueError):
        open_artefact(bytes(sealed), w, aad)


def test_an_artefact_is_not_the_chunked_recording_format() -> None:
    """Two formats, one family, and neither reader accepts the other's
    bytes: `decrypt_file` refuses this on its magic."""
    sealed = seal_artefact(b"body", wrapper(), secret_context("export-artefact", 7))
    assert not sealed.startswith(MAGIC)
