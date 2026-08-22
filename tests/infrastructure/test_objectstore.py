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


# ---------------------------------------------------------------------------
# The read side, which the console streams a recording through
# ---------------------------------------------------------------------------


async def test_size_reports_the_stored_length_without_fetching_it(
    store: S3AudioStore, tmp_path: Path
) -> None:
    """The console declares a track's length in the WAV header before it
    sends a byte of audio, and derives it from exactly this number."""
    source = tmp_path / "c.enc"
    source.write_bytes(b"x" * 4_097)
    await store.put("k", source)

    assert await store.size("k") == 4_097


async def test_size_of_a_missing_object_is_a_key_error(store: S3AudioStore) -> None:
    """A row that outlived its object is the retention sweep mid-stride, so
    the caller answers 404. `KeyError` rather than a botocore
    `ClientError` because the caller should not have to know what a
    `ClientError` is to tell "gone" from "broken".
    """
    with pytest.raises(KeyError):
        await store.size("sessions/1/speakers/1.enc")


async def test_read_returns_exactly_the_bytes_asked_for(
    store: S3AudioStore, tmp_path: Path
) -> None:
    payload = bytes(range(256))
    source = tmp_path / "d.enc"
    source.write_bytes(payload)
    await store.put("k", source)

    assert await store.read("k", 10, 5) == payload[10:15]


async def test_streaming_from_an_offset_skips_everything_before_it(
    store: S3AudioStore, tmp_path: Path
) -> None:
    """The property `Range` exists for: a listener who wants the end of a
    recording must not pay for the beginning of it."""
    payload = bytes(range(256)) * 1_000
    source = tmp_path / "e.enc"
    source.write_bytes(payload)
    await store.put("k", source)

    streamed = b"".join([piece async for piece in store.stream("k", 200_000)])

    assert streamed == payload[200_000:]


async def test_a_stream_that_is_closed_early_stops_transferring(
    store: S3AudioStore, tmp_path: Path
) -> None:
    """A player that stops halfway must stop the transfer out of S3 with
    it, rather than leaving a body open until something finalises it."""
    payload = bytes(range(256)) * 4_000
    source = tmp_path / "f.enc"
    source.write_bytes(payload)
    await store.put("k", source)

    pieces = store.stream("k", 0)
    first = await anext(aiter(pieces))
    await pieces.aclose()

    assert first == payload[: len(first)]
    assert len(first) < len(payload)
