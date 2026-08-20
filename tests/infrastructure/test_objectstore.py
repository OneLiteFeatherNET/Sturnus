from collections.abc import Iterator
from pathlib import Path

import boto3  # type: ignore[import-untyped]
import pytest
from moto import mock_aws

from sturnus.infrastructure.objectstore import S3AudioStore, audio_key

BUCKET = "sturnus-audio"


@pytest.fixture
def store() -> Iterator[S3AudioStore]:
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield S3AudioStore(endpoint=None, bucket=BUCKET, access_key="ak", secret_key="sk")


def test_key_is_stable_and_scoped_to_session_and_speaker() -> None:
    assert audio_key(42, 1234) == "sessions/42/speakers/1234.enc"


async def test_put_then_delete(store: S3AudioStore, tmp_path: Path) -> None:
    source = tmp_path / "a.enc"
    source.write_bytes(b"encrypted-bytes")

    await store.put("sessions/1/speakers/2.enc", source)
    assert await store.exists("sessions/1/speakers/2.enc") is True

    await store.delete("sessions/1/speakers/2.enc")
    assert await store.exists("sessions/1/speakers/2.enc") is False


async def test_deleting_a_missing_object_is_not_an_error(store: S3AudioStore) -> None:
    await store.delete("sessions/9/speakers/9.enc")


async def test_put_transfers_the_bytes_unchanged(store: S3AudioStore, tmp_path: Path) -> None:
    payload = bytes(range(256)) * 40
    source = tmp_path / "b.enc"
    source.write_bytes(payload)
    await store.put("k", source)

    stored = boto3.client("s3", region_name="us-east-1").get_object(Bucket=BUCKET, Key="k")
    assert stored["Body"].read() == payload
