"""Envelope encryption for recorded audio (Spec 12.1).

A fresh data key is generated per session and encrypted with the master key
from the environment; only the wrapped form is stored, alongside the id of
the master key that wrapped it. Rotating the master key therefore does not
require re-encrypting existing recordings.

Files are encrypted in fixed-size chunks rather than in one piece: a
recording can run to hundreds of megabytes, and AES-GCM in a single call
would require holding all of it in memory at once.

**The framing constants below are public, and that is a decision.** The
chunked layout -- `MAGIC`, an 8-byte per-file prefix, then repeated
`[4-byte big-endian length][sealed chunk]` -- was for years read by exactly
one function in this module, so naming it was nobody's business. The
console streams a recording out of S3 straight to a browser
(`sturnus.console.audio`), which means seeking to a chunk boundary without
a file to seek in and deriving the plaintext length without decrypting
anything. Both need the arithmetic, and the alternative to exporting it is
a second copy of the format written down somewhere else -- which is how a
format ends up with two definitions that disagree.
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
_COUNTER_BYTES = 4

#: The framing, as read by anything that is not `decrypt_file`.
MAGIC = b"STRN\x01"
FILE_PREFIX_BYTES = 8
LENGTH_BYTES = 4
#: AES-GCM appends a 16-byte authentication tag, so a sealed chunk is
#: always exactly this much larger than the plaintext it holds. That
#: constant is what makes a track's length derivable from the size of the
#: object without reading any of it.
TAG_BYTES = 16
#: What every file starts with, before the first chunk's length prefix.
HEADER_BYTES = len(MAGIC) + FILE_PREFIX_BYTES
#: The on-disk size of one *full* chunk, prefix and tag included. Every
#: chunk but the last has exactly this size, which is what makes the
#: ciphertext offset of chunk `n` a multiplication rather than a scan.
FRAME_BYTES = LENGTH_BYTES + CHUNK_SIZE + TAG_BYTES


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
        # Named `wrap_nonce` rather than `nonce`: the module-level `nonce`
        # is the *chunk* nonce and has nothing to do with this one, and a
        # local that shadows it here reads as though it did.
        wrap_nonce = os.urandom(_WRAP_NONCE_BYTES)
        return DataKey(plaintext, wrap_nonce + self._aead.encrypt(wrap_nonce, plaintext, None))

    def unwrap(self, wrapped: bytes) -> bytes:
        wrap_nonce, ciphertext = wrapped[:_WRAP_NONCE_BYTES], wrapped[_WRAP_NONCE_BYTES:]
        return self._aead.decrypt(wrap_nonce, ciphertext, None)


def nonce(prefix: bytes, counter: int) -> bytes:
    """The AES-GCM nonce for one chunk of one file.

    Public for the same reason the framing constants are: a reader that
    decrypts chunk `n` without walking to it from chunk zero has to be able
    to construct that chunk's nonce directly.
    """
    # 8 random bytes per file plus a 4-byte counter fills AES-GCM's 12-byte
    # nonce. The prefix makes two encryptions of the same file differ; the
    # counter keeps chunks within one file distinct. Since every file also
    # gets a fresh data key per session, nonce reuse under one key is
    # impossible.
    return prefix + struct.pack(">I", counter)


def encrypt_file(source: Path, target: Path, data_key: bytes) -> None:
    aead = AESGCM(data_key)
    prefix = os.urandom(FILE_PREFIX_BYTES)
    with source.open("rb") as src, target.open("wb") as dst:
        dst.write(MAGIC)
        dst.write(prefix)
        counter = 0
        while chunk := src.read(CHUNK_SIZE):
            sealed = aead.encrypt(nonce(prefix, counter), chunk, None)
            dst.write(struct.pack(">I", len(sealed)))
            dst.write(sealed)
            counter += 1


def decrypt_file(source: Path, target: Path, data_key: bytes) -> None:
    aead = AESGCM(data_key)
    with source.open("rb") as src, target.open("wb") as dst:
        if src.read(len(MAGIC)) != MAGIC:
            raise ValueError("not a sturnus encrypted file")
        prefix = src.read(FILE_PREFIX_BYTES)
        if len(prefix) != FILE_PREFIX_BYTES:
            raise ValueError("truncated header")
        counter = 0
        while header := src.read(LENGTH_BYTES):
            if len(header) != LENGTH_BYTES:
                raise ValueError("truncated chunk header")
            (size,) = struct.unpack(">I", header)
            sealed = src.read(size)
            if len(sealed) != size:
                raise ValueError("truncated chunk")
            dst.write(aead.decrypt(nonce(prefix, counter), sealed, None))
            counter += 1
