import os
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from sturnus.infrastructure.crypto import (
    CHUNK_SIZE,
    KeyWrapper,
    decrypt_file,
    encrypt_file,
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
