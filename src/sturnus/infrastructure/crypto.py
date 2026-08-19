"""Envelope encryption for recorded audio (Spec 12.1).

A fresh data key is generated per session and encrypted with the master key
from the environment; only the wrapped form is stored, alongside the id of
the master key that wrapped it. Rotating the master key therefore does not
require re-encrypting existing recordings.

Files are encrypted in fixed-size chunks rather than in one piece: a
recording can run to hundreds of megabytes, and AES-GCM in a single call
would require holding all of it in memory at once.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHUNK_SIZE = 4 * 1024 * 1024
_KEY_BYTES = 32
_WRAP_NONCE_BYTES = 12
_FILE_PREFIX_BYTES = 8
_COUNTER_BYTES = 4
_LENGTH_BYTES = 4
_MAGIC = b"STRN\x01"


@dataclass(frozen=True)
class DataKey:
    plaintext: bytes
    wrapped: bytes


class KeyWrapper:
    """Wraps and unwraps per-session data keys with the master key."""

    def __init__(self, master_key: bytes, key_id: str) -> None:
        if len(master_key) != _KEY_BYTES:
            raise ValueError(f"master key must be {_KEY_BYTES} bytes")
        self._aead = AESGCM(master_key)
        self.key_id = key_id

    def new_data_key(self) -> DataKey:
        plaintext = os.urandom(_KEY_BYTES)
        nonce = os.urandom(_WRAP_NONCE_BYTES)
        return DataKey(plaintext, nonce + self._aead.encrypt(nonce, plaintext, None))

    def unwrap(self, wrapped: bytes) -> bytes:
        nonce, ciphertext = wrapped[:_WRAP_NONCE_BYTES], wrapped[_WRAP_NONCE_BYTES:]
        return self._aead.decrypt(nonce, ciphertext, None)


def _nonce(prefix: bytes, counter: int) -> bytes:
    # 8 random bytes per file plus a 4-byte counter fills AES-GCM's 12-byte
    # nonce. The prefix makes two encryptions of the same file differ; the
    # counter keeps chunks within one file distinct. Since every file also
    # gets a fresh data key per session, nonce reuse under one key is
    # impossible.
    return prefix + struct.pack(">I", counter)


def encrypt_file(source: Path, target: Path, data_key: bytes) -> None:
    aead = AESGCM(data_key)
    prefix = os.urandom(_FILE_PREFIX_BYTES)
    with source.open("rb") as src, target.open("wb") as dst:
        dst.write(_MAGIC)
        dst.write(prefix)
        counter = 0
        while chunk := src.read(CHUNK_SIZE):
            sealed = aead.encrypt(_nonce(prefix, counter), chunk, None)
            dst.write(struct.pack(">I", len(sealed)))
            dst.write(sealed)
            counter += 1


def decrypt_file(source: Path, target: Path, data_key: bytes) -> None:
    aead = AESGCM(data_key)
    with source.open("rb") as src, target.open("wb") as dst:
        if src.read(len(_MAGIC)) != _MAGIC:
            raise ValueError("not a sturnus encrypted file")
        prefix = src.read(_FILE_PREFIX_BYTES)
        if len(prefix) != _FILE_PREFIX_BYTES:
            raise ValueError("truncated header")
        counter = 0
        while header := src.read(_LENGTH_BYTES):
            if len(header) != _LENGTH_BYTES:
                raise ValueError("truncated chunk header")
            (size,) = struct.unpack(">I", header)
            sealed = src.read(size)
            if len(sealed) != size:
                raise ValueError("truncated chunk")
            dst.write(aead.decrypt(_nonce(prefix, counter), sealed, None))
            counter += 1
