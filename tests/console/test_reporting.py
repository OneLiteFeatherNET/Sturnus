"""What a guild's recording adds up to, computed without a database.

The shaping is pure, so these tests are about the decisions rather than
about SQL: null is not zero, an unfinished session has no length, an
average over nothing is not zero, and months are cut in the guild's own
calendar rather than in UTC.

The last of those is the one worth stating plainly. A meeting that opened
at half past midnight in Berlin belongs to the month the people in it
think it does. Bucketing by UTC would file it under the previous one --
and disagree with the timestamps printed in the protocol of that very
meeting, which the worker writes in the guild's timezone (Spec 11).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sturnus.console.reporting import RecordedSession, guild_report, months

BERLIN = ZoneInfo("Europe/Berlin")
GUILD = 4711
T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def a_session(**over: object) -> RecordedSession:
    base: dict[str, object] = {
        "id": 1,
        "started_at": T0,
        "ended_at": T0 + timedelta(hours=1),
        "documented": True,
        "participants": 3,
        "tracks": 3,
        "audio_seconds": 900.0,
        "speech_seconds": 300.0,
        "unmeasured_tracks": 0,
    }
    base.update(over)
    return RecordedSession(**base)  # type: ignore[arg-type]


def report(
    *sessions: RecordedSession,
    distinct_participants: int = 3,
    zone: object = UTC,
    zone_name: str = "UTC",
) -> dict[str, object]:
    return dict(
        guild_report(
            sessions,
            guild_id=GUILD,
            distinct_participants=distinct_participants,
            zone=zone,  # type: ignore[arg-type]
            zone_name=zone_name,
        )
    )


# ---------------------------------------------------------------------------
# The totals
# ---------------------------------------------------------------------------


def test_a_guild_that_has_never_recorded_reports_nothing_rather_than_zeroes() -> None:
    """An average over nothing is not zero.

    Reporting `0` for the average length of a guild's meetings states that
    its meetings are instantaneous, which is a claim about a guild that
    has never had one.
    """
    empty = report(distinct_participants=0)

    assert empty["sessions"] == 0
    assert empty["average_duration_seconds"] is None
    assert empty["longest_duration_seconds"] is None
    assert empty["average_participants"] is None
    assert empty["largest_meeting"] is None
    assert empty["first_session_at"] is None
    assert empty["months"] == []


def test_the_recorded_time_is_the_sum_of_what_actually_finished() -> None:
    summed = report(
        a_session(id=1, ended_at=T0 + timedelta(hours=1)),
        a_session(
            id=2, started_at=T0 + timedelta(days=1), ended_at=T0 + timedelta(days=1, hours=2)
        ),
    )

    assert summed["recorded_seconds"] == 3 * 3600
    assert summed["sessions"] == 2


def test_a_session_still_running_has_no_length_and_is_counted_separately() -> None:
    """ "Now minus started_at" renders a meeting that grows on every refresh.

    Reported as its own number rather than dropped: a guild with one
    session that has been "recording" for three days has a problem, and an
    average length computed over the others would hide it.
    """
    mixed = report(
        a_session(id=1, ended_at=T0 + timedelta(hours=1)),
        a_session(id=2, ended_at=None),
    )

    assert mixed["sessions"] == 2
    assert mixed["open_sessions"] == 1
    assert mixed["recorded_seconds"] == 3600
    assert mixed["average_duration_seconds"] == 3600


def test_how_many_sessions_produced_a_protocol_is_reported_against_the_total() -> None:
    # Against the total this is the pipeline's success rate as the guild
    # experienced it, which is the question the page exists to answer.
    rate = report(a_session(id=1, documented=True), a_session(id=2, documented=False))

    assert (rate["documented"], rate["sessions"]) == (1, 2)


# ---------------------------------------------------------------------------
# Null is not zero
# ---------------------------------------------------------------------------


def test_a_track_nobody_measured_does_not_count_as_silence() -> None:
    """The rule the whole codebase turns on for these three columns.

    Null means nobody ever measured; zero means somebody did and it was
    nothing. A total that folds the first into the second understates
    itself *and* destroys the distinction.
    """
    unmeasured = report(a_session(speech_seconds=None, tracks=3, unmeasured_tracks=3))

    assert unmeasured["speech_seconds"] == 0
    assert unmeasured["unmeasured_tracks"] == 3
    assert unmeasured["tracks"] == 3


def test_the_hole_in_the_speech_total_travels_with_the_total() -> None:
    # A total offered without it invites the reader to treat "we have no
    # measurement" as "they said nothing".
    partly = report(
        a_session(id=1, speech_seconds=300.0, tracks=3, unmeasured_tracks=1),
        a_session(id=2, speech_seconds=None, tracks=2, unmeasured_tracks=2),
    )

    assert partly["speech_seconds"] == 300.0
    assert partly["unmeasured_tracks"] == 3


# ---------------------------------------------------------------------------
# About meetings, never about the people in them
# ---------------------------------------------------------------------------


def test_the_report_says_how_big_the_meetings_get_and_never_who_was_in_them() -> None:
    """The boundary this module exists to hold.

    "How many people are usually in a meeting here" is a fact about a
    guild's meetings. "Which of them was in the most" is a means of
    monitoring performance and conduct, and it is not built here.
    """
    sizes = report(
        a_session(id=1, participants=2),
        a_session(id=2, participants=6),
        distinct_participants=7,
    )

    assert sizes["average_participants"] == 4
    assert sizes["largest_meeting"] == 6
    assert sizes["distinct_participants"] == 7
    assert not [key for key in sizes if "user" in key or "name" in key]


def test_the_guild_id_travels_as_a_string() -> None:
    # A snowflake exceeds JavaScript's safe integer range, where a JSON
    # number silently loses its last digits.
    assert report()["guild_id"] == str(GUILD)


def test_when_a_guild_first_and_last_recorded_are_both_reported() -> None:
    first = T0 - timedelta(days=400)
    span = report(
        a_session(id=1, started_at=first, ended_at=first + timedelta(hours=1)),
        a_session(id=2, started_at=T0),
    )

    assert span["first_session_at"] == first.isoformat()
    assert span["last_session_at"] == T0.isoformat()


# ---------------------------------------------------------------------------
# Months, in the guild's own calendar
# ---------------------------------------------------------------------------


def test_a_meeting_after_midnight_belongs_to_the_month_the_room_was_in() -> None:
    """23:30 UTC on 31 August is 01:30 on 1 September in Berlin.

    Bucketing by UTC would file this under August and disagree with the
    timestamps printed in the protocol of this very meeting.
    """
    late = datetime(2026, 8, 31, 23, 30, tzinfo=UTC)

    assert [m["month"] for m in months([a_session(started_at=late)], BERLIN)] == ["2026-09"]
    assert [m["month"] for m in months([a_session(started_at=late)], UTC)] == ["2026-08"]


def test_the_timezone_the_months_were_cut_in_is_named_in_the_payload() -> None:
    # A month boundary is a choice, and a reader who is not told which
    # calendar was used will assume theirs.
    assert report(zone=BERLIN, zone_name="Europe/Berlin")["timezone"] == "Europe/Berlin"


def test_months_come_back_oldest_first() -> None:
    # Lexicographic on `YYYY-MM` is chronological, which is the whole
    # reason the key is written that way round.
    spread = months(
        [
            a_session(id=1, started_at=datetime(2026, 3, 4, tzinfo=UTC)),
            a_session(id=2, started_at=datetime(2025, 11, 4, tzinfo=UTC)),
            a_session(id=3, started_at=datetime(2026, 1, 4, tzinfo=UTC)),
        ],
        UTC,
    )

    assert [m["month"] for m in spread] == ["2025-11", "2026-01", "2026-03"]


def test_a_month_with_no_sessions_is_absent_rather_than_zero() -> None:
    """A guild that met in March and again in November has eight empty months.

    A chart that draws them is a chart mostly of nothing, and a client that
    wants a continuous axis can fill the gaps -- knowing, because they are
    absent, which months were genuinely empty.
    """
    apart = months(
        [
            a_session(id=1, started_at=datetime(2026, 3, 4, tzinfo=UTC)),
            a_session(id=2, started_at=datetime(2026, 11, 4, tzinfo=UTC)),
        ],
        UTC,
    )

    assert [m["month"] for m in apart] == ["2026-03", "2026-11"]


def test_a_month_carries_its_own_sessions_seconds_and_protocols() -> None:
    march = datetime(2026, 3, 4, tzinfo=UTC)
    counted = months(
        [
            a_session(id=1, started_at=march, ended_at=march + timedelta(hours=1)),
            a_session(
                id=2,
                started_at=march + timedelta(days=1),
                ended_at=march + timedelta(days=1, minutes=30),
                documented=False,
            ),
        ],
        UTC,
    )

    assert counted[0]["sessions"] == 2
    assert counted[0]["recorded_seconds"] == 5400
    assert counted[0]["documented"] == 1


def test_a_month_containing_an_unfinished_session_counts_it_without_any_seconds() -> None:
    # The session is real and belongs in the count; it simply has no length
    # yet, and inventing one would make the month grow on every refresh.
    march = datetime(2026, 3, 4, tzinfo=UTC)
    counted = months([a_session(started_at=march, ended_at=None)], UTC)

    assert counted[0]["sessions"] == 1
    assert counted[0]["recorded_seconds"] == 0
