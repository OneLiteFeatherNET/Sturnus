"""The numbers the console shows, without a database in sight.

Everything here is a pure function over dataclasses, which is the point:
the rules that are easy to get wrong -- a null measurement summed as a
zero, the viewer counted among the people they spoke with, a snowflake
serialised as a JSON number -- are decided here and are testable without
a server or a schema.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sturnus.console.statistics import (
    AttendedSession,
    Participant,
    Track,
    calendar_year,
    count_words,
    dashboard,
    day_bounds,
    day_timeline,
    session_json,
    year_bounds,
)

T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
ANNA, BEN, CARL = 100, 200, 300
CHANNEL = 555


def track(
    discord_user_id: int = ANNA,
    *,
    display_name: str | None = "anna",
    audio_seconds: float | None = 60.0,
    speech_seconds: float | None = 30.0,
    segment_count: int | None = 4,
) -> Track:
    return Track(
        discord_user_id=discord_user_id,
        display_name=display_name,
        audio_seconds=audio_seconds,
        speech_seconds=speech_seconds,
        segment_count=segment_count,
    )


def attended(
    session_id: int = 1,
    *,
    started_at: datetime = T0,
    ended_at: datetime | None = None,
    channel_id: int = CHANNEL,
    channel_name: str | None = "meeting",
    document_url: str | None = None,
    participants: tuple[Participant, ...] = (
        Participant(ANNA, "anna"),
        Participant(BEN, "ben"),
    ),
    tracks: tuple[Track, ...] = (),
) -> AttendedSession:
    return AttendedSession(
        id=session_id,
        channel_id=channel_id,
        channel_name=channel_name,
        started_at=started_at,
        ended_at=ended_at,
        document_url=document_url,
        participants=participants,
        tracks=tracks,
    )


# ---------------------------------------------------------------------------
# How long a session lasted
# ---------------------------------------------------------------------------


def test_a_session_that_has_not_ended_has_no_duration() -> None:
    """An open session is being recorded right now, or its process died.

    Either way its length is not yet a fact, and reporting the time since
    it started would show a meeting that grows longer forever.
    """
    assert attended(ended_at=None).duration_seconds is None


def test_a_closed_session_lasts_from_its_start_to_its_end() -> None:
    session = attended(started_at=T0, ended_at=datetime(2026, 8, 21, 13, 0, 0, tzinfo=UTC))
    assert session.duration_seconds == 3600.0


# ---------------------------------------------------------------------------
# One session, as JSON
# ---------------------------------------------------------------------------


def test_every_id_is_a_string() -> None:
    """A Discord snowflake exceeds JavaScript's safe integer range, where a
    JSON number silently loses its last digits -- producing an id that
    looks right and names nobody. The session id travels as a string for
    consistency rather than necessity: two id shapes in one payload is how
    the one that matters gets parsed with the wrong one.
    """
    session = attended(
        session_id=7,
        channel_id=386950399101370374,
        participants=(Participant(386950399101370375, "ben"),),
        tracks=(track(386950399101370376),),
    )
    body = session_json(session, viewer=ANNA)
    assert body["id"] == "7"
    assert body["channel_id"] == "386950399101370374"
    assert body["other_participants"][0]["discord_user_id"] == "386950399101370375"
    assert body["tracks"][0]["discord_user_id"] == "386950399101370376"


def test_the_viewer_is_not_listed_among_the_others_who_were_there() -> None:
    session = attended(participants=(Participant(ANNA, "anna"), Participant(BEN, "ben")))
    body = session_json(session, viewer=ANNA)
    assert body["other_participants"] == [{"discord_user_id": "200", "display_name": "ben"}]


def test_the_viewers_own_track_is_kept() -> None:
    """Unlike the participant list, the tracks are the recording itself --
    and the one a person most wants to see is their own.
    """
    session = attended(tracks=(track(ANNA), track(BEN, display_name="ben")))
    body = session_json(session, viewer=ANNA)
    assert [t["discord_user_id"] for t in body["tracks"]] == ["100", "200"]


def test_a_measurement_that_was_never_taken_stays_null() -> None:
    """Null means "never measured" -- a job that predates these columns.
    Zero means "measured, and it was nothing". Coercing the first into the
    second invents a fact about a recording nobody ever looked at.
    """
    session = attended(
        tracks=(track(ANNA, audio_seconds=None, speech_seconds=None, segment_count=None),)
    )
    measured = session_json(session, viewer=ANNA)["tracks"][0]
    assert measured["audio_seconds"] is None
    assert measured["speech_seconds"] is None
    assert measured["segment_count"] is None


def test_a_measured_silence_is_zero_rather_than_null() -> None:
    session = attended(
        tracks=(track(ANNA, audio_seconds=120.0, speech_seconds=0.0, segment_count=0),)
    )
    measured = session_json(session, viewer=ANNA)["tracks"][0]
    assert measured["speech_seconds"] == 0.0
    assert measured["segment_count"] == 0


def test_a_session_without_a_protocol_says_so() -> None:
    assert session_json(attended(document_url=None), viewer=ANNA)["document_url"] is None


def test_a_session_with_a_protocol_carries_its_link() -> None:
    session = attended(document_url="https://outline.example/doc/x")
    assert session_json(session, viewer=ANNA)["document_url"] == "https://outline.example/doc/x"


def test_times_are_serialised_as_iso_8601_with_an_offset() -> None:
    """Without the offset a browser reads the string as local time, and
    every session in the console moves by the viewer's own timezone.
    """
    session = attended(started_at=T0, ended_at=datetime(2026, 8, 21, 12, 30, 0, tzinfo=UTC))
    body = session_json(session, viewer=ANNA)
    assert body["started_at"] == "2026-08-21T12:00:00+00:00"
    assert body["ended_at"] == "2026-08-21T12:30:00+00:00"


def test_an_open_session_has_no_end() -> None:
    body = session_json(attended(ended_at=None), viewer=ANNA)
    assert body["ended_at"] is None
    assert body["duration_seconds"] is None


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------


def test_speaking_time_counts_only_the_viewers_own_tracks() -> None:
    """The other tracks in a session are other people talking. Summing them
    would tell somebody who sat silent through ten meetings that they are
    the most talkative person in the guild.
    """
    session = attended(tracks=(track(ANNA, speech_seconds=30.0), track(BEN, speech_seconds=900.0)))
    assert dashboard([session], viewer=ANNA, transcripts=())["total_speech_seconds"] == 30.0


def test_speaking_time_adds_up_across_sessions() -> None:
    sessions = [
        attended(1, tracks=(track(ANNA, speech_seconds=30.0),)),
        attended(2, tracks=(track(ANNA, speech_seconds=12.5),)),
    ]
    assert dashboard(sessions, viewer=ANNA, transcripts=())["total_speech_seconds"] == 42.5


def test_an_unmeasured_track_is_reported_rather_than_summed_as_zero() -> None:
    """Summing null as zero understates the total and says nothing about
    why. The count is what lets the console say "plus four we never
    measured" instead of quietly showing a smaller number.
    """
    sessions = [
        attended(1, tracks=(track(ANNA, speech_seconds=30.0),)),
        attended(2, tracks=(track(ANNA, speech_seconds=None),)),
    ]
    summary = dashboard(sessions, viewer=ANNA, transcripts=())
    assert summary["total_speech_seconds"] == 30.0
    assert summary["unmeasured_tracks"] == 1


def test_speaking_time_is_unknown_when_nothing_was_ever_measured() -> None:
    """Zero would be a claim: "we measured, and you said nothing". With no
    measurement anywhere there is no such claim to make.
    """
    session = attended(tracks=(track(ANNA, speech_seconds=None),))
    assert dashboard([session], viewer=ANNA, transcripts=())["total_speech_seconds"] is None


def test_sessions_attended_counts_what_was_handed_over() -> None:
    summary = dashboard([attended(1), attended(2), attended(3)], viewer=ANNA, transcripts=())
    assert summary["sessions_attended"] == 3


def test_only_the_sessions_that_produced_a_protocol_are_counted_as_such() -> None:
    sessions = [
        attended(1, document_url="https://outline.example/doc/a"),
        attended(2, document_url=None),
    ]
    assert dashboard(sessions, viewer=ANNA, transcripts=())["sessions_with_protocol"] == 1


def test_you_are_not_somebody_you_spoke_with() -> None:
    sessions = [
        attended(1, participants=(Participant(ANNA, "anna"), Participant(BEN, "ben"))),
        attended(2, participants=(Participant(ANNA, "anna"), Participant(CARL, "carl"))),
    ]
    assert dashboard(sessions, viewer=ANNA, transcripts=())["people_spoken_with"] == 2


def test_the_same_person_across_two_meetings_is_one_person() -> None:
    sessions = [
        attended(1, participants=(Participant(ANNA, "anna"), Participant(BEN, "ben"))),
        attended(2, participants=(Participant(ANNA, "anna"), Participant(BEN, "ben"))),
    ]
    assert dashboard(sessions, viewer=ANNA, transcripts=())["people_spoken_with"] == 1


def test_the_longest_session_is_the_one_that_ran_longest() -> None:
    short = attended(1, started_at=T0, ended_at=T0 + timedelta(minutes=10))
    long = attended(2, started_at=T0, ended_at=T0 + timedelta(minutes=90))
    longest = dashboard([short, long], viewer=ANNA, transcripts=())["longest_session"]
    assert longest is not None
    assert longest["id"] == "2"
    assert longest["duration_seconds"] == 5400.0


def test_a_session_still_running_cannot_be_the_longest_one() -> None:
    """It has no length yet, so it cannot win a comparison of lengths."""
    running = attended(1, started_at=T0, ended_at=None)
    finished = attended(2, started_at=T0, ended_at=T0 + timedelta(minutes=5))
    longest = dashboard([running, finished], viewer=ANNA, transcripts=())["longest_session"]
    assert longest is not None
    assert longest["id"] == "2"


def test_there_is_no_longest_session_when_none_has_ended() -> None:
    summary = dashboard([attended(ended_at=None)], viewer=ANNA, transcripts=())
    assert summary["longest_session"] is None


def test_the_first_and_most_recent_sessions_are_the_ends_of_the_history() -> None:
    sessions = [
        attended(1, started_at=datetime(2026, 3, 1, 9, 0, tzinfo=UTC)),
        attended(2, started_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC)),
        attended(3, started_at=datetime(2026, 5, 1, 9, 0, tzinfo=UTC)),
    ]
    summary = dashboard(sessions, viewer=ANNA, transcripts=())
    first, newest = summary["first_session"], summary["most_recent_session"]
    assert first is not None and newest is not None
    assert first["id"] == "1"
    assert newest["id"] == "2"


def test_a_person_who_has_been_in_nothing_gets_a_dashboard_rather_than_an_error() -> None:
    """Somebody who linked their account and has not yet been recorded is
    the ordinary first visit, not an edge case.
    """
    summary = dashboard([], viewer=ANNA, transcripts=())
    assert summary["sessions_attended"] == 0
    assert summary["first_session"] is None
    assert summary["most_recent_session"] is None
    assert summary["words_transcribed"] == 0


# ---------------------------------------------------------------------------
# Words, as a proxy for how much was said
# ---------------------------------------------------------------------------


def test_words_are_counted_across_every_segment_of_every_transcript() -> None:
    transcripts = [
        '{"language": "de", "segments": [{"start": 0.0, "end": 1.0, "text": "guten morgen"},'
        ' {"start": 1.0, "end": 2.0, "text": "alle zusammen"}]}',
        '{"language": "en", "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}]}',
    ]
    assert count_words(transcripts) == 5


def test_a_transcript_with_no_segments_contributes_nothing() -> None:
    assert count_words(['{"language": "de", "segments": []}']) == 0


def test_whitespace_is_not_a_word() -> None:
    assert (
        count_words(['{"language": "de", "segments": [{"start": 0, "end": 1, "text": "  "}]}']) == 0
    )


# ---------------------------------------------------------------------------
# The calendar
# ---------------------------------------------------------------------------


def test_a_day_with_two_sessions_is_one_entry() -> None:
    sessions = [
        attended(1, started_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC)),
        attended(2, started_at=datetime(2026, 8, 21, 17, 0, tzinfo=UTC)),
    ]
    days = calendar_year(sessions)
    assert len(days) == 1
    assert days[0]["date"] == "2026-08-21"
    assert days[0]["sessions"] == 2


def test_a_days_duration_is_the_sum_of_what_ran_on_it() -> None:
    sessions = [
        attended(
            1,
            started_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
            ended_at=datetime(2026, 8, 21, 9, 30, tzinfo=UTC),
        ),
        attended(
            2,
            started_at=datetime(2026, 8, 21, 17, 0, tzinfo=UTC),
            ended_at=datetime(2026, 8, 21, 17, 15, tzinfo=UTC),
        ),
    ]
    assert calendar_year(sessions)[0]["total_duration_seconds"] == 2700.0


def test_a_day_counts_everybody_who_was_there_including_the_viewer() -> None:
    """The tooltip answers "how many people were in this", and the viewer
    was one of them. `people_spoken_with` on the dashboard is the other
    question and excludes them on purpose.
    """
    sessions = [
        attended(
            1,
            started_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
            participants=(Participant(ANNA, "anna"), Participant(BEN, "ben")),
        ),
        attended(
            2,
            started_at=datetime(2026, 8, 21, 17, 0, tzinfo=UTC),
            participants=(Participant(ANNA, "anna"), Participant(CARL, "carl")),
        ),
    ]
    assert calendar_year(sessions)[0]["participants"] == 3


def test_days_come_back_in_calendar_order() -> None:
    sessions = [
        attended(1, started_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC)),
        attended(2, started_at=datetime(2026, 3, 4, 9, 0, tzinfo=UTC)),
    ]
    assert [day["date"] for day in calendar_year(sessions)] == ["2026-03-04", "2026-08-21"]


def test_a_days_timeline_reads_forwards() -> None:
    """A timeline is drawn left to right, and the newest-first order the
    session list uses would draw it backwards.
    """
    sessions = [
        attended(1, started_at=datetime(2026, 8, 21, 17, 0, tzinfo=UTC)),
        attended(2, started_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC)),
    ]
    assert [entry["id"] for entry in day_timeline(sessions)] == ["2", "1"]


def test_a_timeline_entry_says_when_it_started_how_long_it_ran_and_where() -> None:
    session = attended(
        4,
        started_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
        ended_at=datetime(2026, 8, 21, 9, 45, tzinfo=UTC),
        channel_id=386950399101370374,
        channel_name="standup",
    )
    entry = day_timeline([session])[0]
    assert entry == {
        "id": "4",
        "started_at": "2026-08-21T09:00:00+00:00",
        "duration_seconds": 2700.0,
        "channel_id": "386950399101370374",
        "channel_name": "standup",
    }


# ---------------------------------------------------------------------------
# The windows the queries filter on
# ---------------------------------------------------------------------------


def test_a_year_runs_from_its_first_instant_to_its_last() -> None:
    first, last = year_bounds(2026)
    assert first == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert last == datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)


def test_the_last_representable_year_does_not_overflow() -> None:
    """An exclusive upper bound would be `datetime(year + 1, 1, 1)`, which
    for year 9999 is not a datetime at all. The inclusive end exists so
    that a nonsense year in a query string is an empty calendar rather
    than a 500.
    """
    _, last = year_bounds(9999)
    assert last == datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)


def test_a_day_runs_from_midnight_to_the_last_instant_before_the_next() -> None:
    first, last = day_bounds(datetime(2026, 8, 21, tzinfo=UTC).date())
    assert first == datetime(2026, 8, 21, 0, 0, 0, tzinfo=UTC)
    assert last == datetime(2026, 8, 21, 23, 59, 59, 999999, tzinfo=UTC)
