"""S3 storage for encrypted recordings.

`boto3` is synchronous, so every call runs in a worker thread — the bot's
event loop must never block on a network transfer while it is receiving
voice packets.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from sturnus.application.recording import audio_key

__all__ = ["S3AudioStore", "audio_key"]


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
                if exc.response["Error"]["Code"] in {"404", "NoSuchKey"}:
                    return False
                raise
            return True

        return await asyncio.to_thread(_head)
