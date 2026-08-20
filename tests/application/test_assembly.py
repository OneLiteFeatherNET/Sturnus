from datetime import UTC, datetime, timedelta

from sturnus.application.assembly import assemble
from sturnus.application.transcription import TranscribedSegment, TranscriptionResult

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
ANNA, BEN = 100, 200


class FakeSessions:
    def __init__(self) -> None:
        self.names = {ANNA: "anna", BEN: "ben"}
        self.epochs = {ANNA: T0, BEN: T0 + timedelta(seconds=10)}
        self.bounds = (T0, T0 + timedelta(hours=1))

    async def participant_names(self, _session_id: int) -> dict[int, str]:
        return self.names

    async def audio_epoch(self, _session_id: int, user_id: int) -> datetime | None:
        return self.epochs.get(user_id)

    async def session_bounds(self, _session_id: int) -> tuple[datetime, datetime]:
        return self.bounds


class FakeJobs:
    def __init__(self, per_speaker: dict[int, TranscriptionResult]) -> None:
        self._per_speaker = per_speaker

    async def transcripts_for(self, _session_id: int) -> dict[int, TranscriptionResult]:
        return self._per_speaker


class FakeLinks:
    def __init__(self, linked: dict[int, tuple[str, str]] | None = None) -> None:
        self._linked = linked or {}

    async def external_identity(self, discord_user_id: int) -> tuple[str, str] | None:
        return self._linked.get(discord_user_id)


def result(*pairs: tuple[float, float, str]) -> TranscriptionResult:
    return TranscriptionResult(
        segments=tuple(TranscribedSegment(s, e, t) for s, e, t in pairs), language="de"
    )


async def test_segments_from_both_speakers_are_interleaved_by_time() -> None:
    """Each speaker's offsets are relative to their own epoch, ten seconds apart."""
    transcript = await assemble(
        1,
        FakeSessions(),
        FakeJobs({ANNA: result((0.0, 2.0, "anna first")), BEN: result((0.0, 2.0, "ben second"))}),
        FakeLinks(),
        tz=UTC,
    )
    assert [b.text for b in transcript.blocks] == ["anna first", "ben second"]


async def test_a_linked_account_reaches_the_transcript() -> None:
    transcript = await assemble(
        1,
        FakeSessions(),
        FakeJobs({ANNA: result((0.0, 1.0, "hello"))}),
        FakeLinks({ANNA: ("out-1", "Anna Example")}),
        tz=UTC,
    )
    speaker = transcript.blocks[0].speaker
    assert speaker.external_user_id == "out-1"
    assert speaker.external_display_name == "Anna Example"


async def test_an_unlinked_speaker_keeps_only_their_discord_identity() -> None:
    transcript = await assemble(
        1, FakeSessions(), FakeJobs({BEN: result((0.0, 1.0, "hi"))}), FakeLinks(), tz=UTC
    )
    assert transcript.blocks[0].speaker.external_user_id is None


async def test_a_speaker_without_an_epoch_is_skipped() -> None:
    """No epoch means no audio was ever recorded for them."""
    sessions = FakeSessions()
    sessions.epochs.pop(BEN)
    transcript = await assemble(
        1,
        sessions,
        FakeJobs({ANNA: result((0.0, 1.0, "a")), BEN: result((0.0, 1.0, "b"))}),
        FakeLinks(),
        tz=UTC,
    )
    assert [b.text for b in transcript.blocks] == ["a"]


async def test_a_session_with_no_transcripts_yields_an_empty_transcript() -> None:
    transcript = await assemble(1, FakeSessions(), FakeJobs({}), FakeLinks(), tz=UTC)
    assert transcript.blocks == ()


async def test_merge_gap_is_read_from_the_caller_not_the_domain_default() -> None:
    """`assemble` must forward a caller-supplied `merge_gap` to
    `build_transcript` rather than always falling back to
    `sturnus.domain.transcript.DEFAULT_MERGE_GAP` (15s, Spec 11's
    `merge_gap_seconds` default): a test that only exercises the domain
    default would pass even if this parameter were silently dropped.
    """
    jobs = FakeJobs({ANNA: result((0.0, 1.0, "first"), (5.0, 6.0, "second"))})

    # A 4-second gap between "first" and "second" is well inside the
    # 15-second domain default -- they merge into one block there.
    default_gap = await assemble(1, FakeSessions(), jobs, FakeLinks(), tz=UTC)
    assert len(default_gap.blocks) == 1

    # The same 4-second gap, against a caller-configured 1-second limit,
    # must split into two blocks -- proving the configured value, not the
    # default, actually governed the merge.
    configured_gap = await assemble(
        1, FakeSessions(), jobs, FakeLinks(), tz=UTC, merge_gap=timedelta(seconds=1)
    )
    assert len(configured_gap.blocks) == 2


async def test_display_names_come_from_the_session_not_from_now() -> None:
    """Names are frozen at recording time (Spec 8.3)."""
    transcript = await assemble(
        1, FakeSessions(), FakeJobs({ANNA: result((0.0, 1.0, "x"))}), FakeLinks(), tz=UTC
    )
    assert transcript.blocks[0].speaker.discord_display_name == "anna"
