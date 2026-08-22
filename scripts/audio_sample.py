"""Decrypts a slice of one recording so a human can listen to it.

Every automated check this system has says the same thing about a track --
a level, a spectrum, an autocorrelation, a word-error rate of everything --
without ever answering the one question that decides what to do next: is
there speech on it, and is it speech a person could understand? Only ears
answer that, and until this script existed there was no way to put a
recording in front of any.

It exists because of one track that measured speech-like on every axis
(lag-1 autocorrelation 0.756, 26% of its energy in the 2-8 Hz syllable
band) and that neither `tiny` nor `large-v3` could transcribe a word of.
Nothing left to measure; somebody has to listen.

**This decrypts other people's voices.** The output is personal data under
the consent those speakers gave for a transcript, and nothing else. Write
it somewhere private, listen to what you need, delete it. Do not send it
anywhere -- not to a chat, not to a bug report, not to a colleague who was
not in the meeting. `--duration` defaults to 60 seconds rather than the
whole track for that reason: a sample is a sample.

Usage
-----
List what can be sampled::

    uv run python scripts/audio_sample.py list

Take a minute from the middle of job 4::

    uv run python scripts/audio_sample.py extract 4 --start 30:00 --out /tmp/s.wav

Reaching the cluster from a workstation needs both services forwarded and
the credentials in the environment; `docs/operations.md` has that recipe.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import struct
import sys
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import boto3  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sturnus.infrastructure.crypto import KeyWrapper, decrypt_file  # noqa: E402
from sturnus.infrastructure.db.models import (  # noqa: E402
    Session,
    SessionParticipant,
    TranscriptionJob,
)

# What `sturnus.infrastructure.recording_adapters.FileAudioWriterFactory`
# writes is a *complete RIFF/WAVE file* -- 16 kHz mono, converted from
# Discord's 48 kHz stereo on arrival because that is Whisper's own format.
#
# This script therefore states no format at all. It opens the decrypted
# object with `wave` and reads the rate and channel count out of it.
#
# The earlier draft of this file did state one -- "48 kHz stereo, no
# container" -- and so did `sturnus.console.audio`, and both were wrong by
# a factor of six (two channels times 48000/16000). Every slice this
# script wrote came out at six times speed with the stored header played
# as samples, and every length it printed was a sixth of the truth: that
# is where "a 52-minute session left an 8:41 track behind" came from. The
# track was 52 minutes. The reader was wrong. See #77.


def _seconds(value: str) -> float:
    """Accepts `90`, `1:30` or `1:30:00`, because a 100-minute track is not
    naturally thought about in seconds."""
    parts = value.split(":")
    if len(parts) > 3:
        raise argparse.ArgumentTypeError(f"not a time: {value}")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def _clock(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _engine() -> AsyncEngine:
    url = os.environ.get("STURNUS_DATABASE_URL")
    if not url:
        sys.exit("STURNUS_DATABASE_URL is not set; see docs/operations.md")
    return create_async_engine(url)


def _s3() -> Any:
    for name in ("STURNUS_S3_ENDPOINT", "STURNUS_S3_ACCESS_KEY", "STURNUS_S3_SECRET_KEY"):
        if not os.environ.get(name):
            sys.exit(f"{name} is not set; see docs/operations.md")
    return boto3.client(
        "s3",
        endpoint_url=os.environ["STURNUS_S3_ENDPOINT"],
        aws_access_key_id=os.environ["STURNUS_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["STURNUS_S3_SECRET_KEY"],
        region_name=os.environ.get("STURNUS_S3_REGION", "us-east-1"),
    )


async def _list() -> int:
    engine = _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    TranscriptionJob.id,
                    TranscriptionJob.session_id,
                    TranscriptionJob.discord_user_id,
                    TranscriptionJob.status,
                    TranscriptionJob.audio_deleted_at,
                    SessionParticipant.discord_display_name,
                    Session.started_at,
                )
                .join(Session, Session.id == TranscriptionJob.session_id)
                .outerjoin(
                    SessionParticipant,
                    (SessionParticipant.session_id == TranscriptionJob.session_id)
                    & (SessionParticipant.discord_user_id == TranscriptionJob.discord_user_id),
                )
                .order_by(TranscriptionJob.id)
            )
        ).all()
    await engine.dispose()

    if not rows:
        print("No jobs.")
        return 0
    print(f"{'job':>4}  {'session':>7}  {'status':<10}  {'audio':<8}  {'started':<16}  speaker")
    for row in rows:
        # A job whose audio the retention sweep already deleted cannot be
        # sampled, and saying so here beats a 404 from S3 later.
        audio = "deleted" if row.audio_deleted_at else "present"
        started = row.started_at.strftime("%Y-%m-%d %H:%M") if row.started_at else "-"
        speaker = row.discord_display_name or str(row.discord_user_id)
        print(
            f"{row.id:>4}  {row.session_id:>7}  {row.status:<10}  {audio:<8}  "
            f"{started:<16}  {speaker}"
        )
    return 0


async def _extract(job_id: int, start: float, duration: float, out: Path) -> int:
    engine = _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        job = await session.get(TranscriptionJob, job_id)
        if job is None:
            await engine.dispose()
            sys.exit(f"No job {job_id}.")
        if job.audio_deleted_at is not None:
            await engine.dispose()
            sys.exit(f"Job {job_id}'s audio was deleted on {job.audio_deleted_at:%Y-%m-%d}.")
        s3_key = job.s3_key
        wrapped = job.wrapped_data_key
        key_id = job.encryption_key_id
    await engine.dispose()

    master = os.environ.get("STURNUS_MASTER_KEY")
    if not master:
        sys.exit("STURNUS_MASTER_KEY is not set; see docs/operations.md")
    import base64

    data_key = KeyWrapper(base64.b64decode(master), key_id).unwrap(wrapped)

    bucket = os.environ.get("STURNUS_S3_BUCKET", "sturnus-audio")
    with TemporaryDirectory() as tmp:
        sealed = Path(tmp) / "sealed"
        plain = Path(tmp) / "plain.pcm"
        print(f"Downloading s3://{bucket}/{s3_key} ...", file=sys.stderr)
        _s3().download_file(bucket, s3_key, str(sealed))
        print(f"Decrypting {sealed.stat().st_size / 1e6:.1f} MB ...", file=sys.stderr)
        decrypt_file(sealed, plain, data_key)

        # The track describes itself, so the slice is taken in *frames*
        # through `wave` rather than in bytes through a seek. There is no
        # frame-alignment arithmetic to get wrong, because there is no
        # byte offset: `setpos` takes a frame index.
        with wave.open(str(plain)) as track:
            rate = track.getframerate()
            channels = track.getnchannels()
            width = track.getsampwidth()
            frames = track.getnframes()
            total = frames / rate if rate else 0.0
            if start >= total:
                sys.exit(f"Track is {_clock(total)} long; {_clock(start)} is past its end.")
            track.setpos(min(frames, int(start * rate)))
            pcm = track.readframes(int(duration * rate))

    bytes_per_second = rate * channels * width
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as wav:
        # Written back in the format it was read in. Anything else would be
        # a transcode this script has no reason to perform and every reason
        # not to: the point is to hear what is actually stored.
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(pcm)

    peak = 0
    for i in range(0, len(pcm) - 1, 2):
        peak = max(peak, abs(struct.unpack_from("<h", pcm, i)[0]))
    print(
        f"Wrote {out} -- {len(pcm) / bytes_per_second:.0f}s from {_clock(start)} "
        f"of a {_clock(total)} track ({rate} Hz, "
        f"{'mono' if channels == 1 else f'{channels} channels'}), peak {peak}/32767.",
        file=sys.stderr,
    )
    if peak == 0:
        print(
            "Peak is exactly zero: this slice is digital silence, not quiet speech.",
            file=sys.stderr,
        )
    print("This is personal data. Listen, then delete it.", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show every job whose audio still exists")

    extract = sub.add_parser("extract", help="write a slice of one job's audio as a WAV")
    extract.add_argument("job_id", type=int)
    extract.add_argument(
        "--start", type=_seconds, default=0.0, help="offset into the track (s, m:s, or h:m:s)"
    )
    extract.add_argument(
        "--duration",
        type=_seconds,
        default=60.0,
        help="how much to write (default 60s -- a sample is a sample)",
    )
    extract.add_argument("--out", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "list":
        return asyncio.run(_list())
    return asyncio.run(_extract(args.job_id, args.start, args.duration, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
