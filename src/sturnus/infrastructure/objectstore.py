"""S3 storage for encrypted recordings, and for rendered protocols.

Two classes, deliberately apart. `S3AudioStore` streams large encrypted
objects and never holds one; `S3DocumentStore` reads and writes whole small
text objects. Keeping them separate is what stops the second one's
`get`-the-whole-thing from being within reach of the first one's callers --
the same argument the note below makes about `get`-into-a-file.



`boto3` is synchronous, so every call runs in a worker thread — the bot's
event loop must never block on a network transfer while it is receiving
voice packets.

The read side (`size`, `read`, `stream`) exists for the console, which
serves a recording to a browser without ever holding it: it asks how large
the object is to declare the track's length, reads the fixed-size file
header for its nonce prefix, and streams the body from a chosen chunk
boundary onwards. `get`-into-a-file, which the worker needs, is deliberately
not here — that one downloads a whole recording, and this class is shared
with a process that must never be tempted to.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from sturnus.application.recording import audio_key

__all__ = ["S3AudioStore", "S3DocumentStore", "audio_key"]

#: How much of a streamed body is pulled across per thread hop. Large
#: enough that a 4 MiB chunk costs a handful of hops rather than hundreds,
#: small enough that a listener who stops playing stops the transfer within
#: a fraction of a chunk rather than at the end of one.
_STREAM_PIECE_BYTES = 256 * 1024

#: What S3 and its compatible implementations call "no such object". Two
#: spellings because `head_object` answers with the HTTP status and
#: `get_object` with the API error name.
_MISSING = frozenset({"404", "NoSuchKey"})


class S3AudioStore:
    def __init__(
        self,
        endpoint: str | None,
        bucket: str,
        access_key: str,
        secret_key: str,
    ) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
        )

    async def put(self, key: str, source: Path) -> None:
        await asyncio.to_thread(self._client.upload_file, str(source), self._bucket, key)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)

    async def exists(self, key: str) -> bool:
        def _head() -> bool:
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in _MISSING:
                    return False
                raise
            return True

        return await asyncio.to_thread(_head)

    async def size(self, key: str) -> int:
        """How many bytes the stored object has.

        Raises `KeyError` for an object that is not there, rather than
        letting a `ClientError` out: the ordinary cause is a job row that
        outlived its object because the retention sweep erased the audio,
        and the caller answering that with a 404 should not have to know
        what a `ClientError` is to tell "gone" from "broken".
        """

        def _head() -> int:
            try:
                head = self._client.head_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in _MISSING:
                    raise KeyError(key) from exc
                raise
            length: int = head["ContentLength"]
            return length

        return await asyncio.to_thread(_head)

    async def read(self, key: str, start: int, length: int) -> bytes:
        """Exactly `length` bytes from `start`.

        For the fixed-size file header, and nothing larger: a caller that
        wants a recording wants `stream`.
        """

        def _get() -> bytes:
            try:
                response = self._client.get_object(
                    Bucket=self._bucket, Key=key, Range=f"bytes={start}-{start + length - 1}"
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] in _MISSING:
                    raise KeyError(key) from exc
                raise
            body: bytes = response["Body"].read()
            return body

        return await asyncio.to_thread(_get)

    async def stream(self, key: str, start: int) -> AsyncGenerator[bytes, None]:
        """Yields the object from `start` to its end, a piece at a time.

        Never assembles the whole body: a recording runs to hundreds of
        megabytes, and the console's whole reason for streaming it is that
        neither this process nor the disk under it ever holds one.

        The `finally` closes the body even when the consumer stops early,
        which is the normal case rather than the exceptional one — a
        listener who pauses is a caller who abandons this generator.
        """
        try:
            response = await asyncio.to_thread(
                self._client.get_object, Bucket=self._bucket, Key=key, Range=f"bytes={start}-"
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] in _MISSING:
                raise KeyError(key) from exc
            raise
        body = response["Body"]
        try:
            while piece := await asyncio.to_thread(body.read, _STREAM_PIECE_BYTES):
                yield piece
        finally:
            await asyncio.to_thread(body.close)


class S3DocumentStore:
    """Whole small objects: a rendered protocol, not a recording.

    The worker writes one per session per object-store destination and the
    console API reads it back; both construct one of these against the same
    bucket. `get` returns the whole body, which is right here and is exactly
    what `S3AudioStore` refuses to offer -- a protocol is tens of kilobytes
    of text and a recording is hundreds of megabytes of somebody's voice.

    **This class moves bytes and does not decide what they are.** The
    objects it carries today are sealed envelopes
    (`sturnus.infrastructure.documents.artefacts.SealedArtefacts`, which
    is what both processes actually hold): a protocol is every word every
    participant said, and it was for one release the only thing in this
    bucket that was not ciphertext. The sealing is deliberately *not*
    here, because "put whole small object" and "seal a protocol" are
    different jobs and a store that did both would be a store somebody
    reaches for when they want the first.

    Access control is the console route in front of the artefact, which is
    why the URL a sink hands back points there and never at a presigned S3
    URL -- a presigned URL outlives the access rules that issued it, and
    the participant rule this content sits behind is checked per request.
    """

    def __init__(
        self,
        endpoint: str | None,
        bucket: str,
        access_key: str,
        secret_key: str,
    ) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
        )

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        """Writes one rendered protocol, replacing whatever was there.

        Replacing rather than versioning: a re-export of a session is a
        correction of the same artefact at the same address, and the
        console route resolves that address from `session_document`, which
        `SessionDocumentStore.record` upserts on the same key.
        """
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )

    async def get(self, key: str) -> bytes:
        """The whole object, or `KeyError` if it is not there.

        `KeyError` rather than a `ClientError` for the reason
        `S3AudioStore.size` gives: the caller answering "gone" with a 404
        should not have to know what a `ClientError` is to tell it apart
        from "broken".
        """

        def _get() -> bytes:
            try:
                response = self._client.get_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in _MISSING:
                    raise KeyError(key) from exc
                raise
            body: bytes = response["Body"].read()
            return body

        return await asyncio.to_thread(_get)
