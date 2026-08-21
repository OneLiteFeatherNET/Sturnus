from datetime import UTC, datetime

import pytest

from sturnus.application.publishing import (
    announce_ready_sessions,
    render_announcement,
    render_silent_audio_warning,
    sessions_to_announce,
)

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


def session(
    session_id: int,
    status: str = "documented",
    document_url: str | None = "https://outline.example/doc/1",
    announced: datetime | None = None,
    channel_id: int = 999,
    participant_ids: tuple[int, ...] = (),
) -> dict[str, object]:
    return {
        "id": session_id,
        "status": status,
        "document_url": document_url,
        "announced_at": announced,
        "channel_id": channel_id,
        "participant_ids": participant_ids,
    }


def test_a_documented_unannounced_session_is_selected() -> None:
    assert [s["id"] for s in sessions_to_announce([session(1)])] == [1]


def test_a_session_that_is_not_yet_documented_is_not_selected() -> None:
    assert sessions_to_announce([session(1, status="open")]) == []
    assert sessions_to_announce([session(1, status="closed")]) == []


def test_an_already_announced_session_is_never_announced_twice() -> None:
    """`announced_at` is what stops a restart from re-posting every link ever published."""
    assert sessions_to_announce([session(1, announced=T0)]) == []


def test_a_documented_session_without_a_url_yet_is_not_selected() -> None:
    """Defensive: there is nothing to post without a link, even if status says documented."""
    assert sessions_to_announce([session(1, document_url=None)]) == []


def test_several_sessions_are_filtered_independently() -> None:
    sessions = [
        session(1, status="open"),
        session(2),
        session(3, announced=T0),
        session(4, status="closed"),
        session(5),
    ]
    assert [s["id"] for s in sessions_to_announce(sessions)] == [2, 5]


# ---------------------------------------------------------------------------
# `announce_ready_sessions` -- the periodic sweep that actually calls
# `sessions_to_announce`, posts the link, and stamps `announced_at`.
# ---------------------------------------------------------------------------


class FakeSessions:
    def __init__(self, candidates: list[dict[str, object]] | None = None) -> None:
        self._candidates = candidates or []
        self.announced: list[int] = []

    async def candidates_for_announcement(self) -> list[dict[str, object]]:
        return self._candidates

    async def mark_announced(self, session_id: int, _now: datetime) -> None:
        self.announced.append(session_id)


class FakeAnnouncer:
    def __init__(self, fail_channel_ids: set[int] | None = None) -> None:
        self.posted: list[tuple[int, str]] = []
        self._fail_channel_ids = fail_channel_ids or set()

    async def post(self, channel_id: int, text: str) -> None:
        if channel_id in self._fail_channel_ids:
            raise RuntimeError("Discord API is briefly unreachable")
        self.posted.append((channel_id, text))


async def test_announce_ready_sessions_posts_and_stamps_a_ready_session() -> None:
    sessions = FakeSessions([session(1, channel_id=42)])
    announcer = FakeAnnouncer()
    await announce_ready_sessions(sessions, announcer, T0)
    assert len(announcer.posted) == 1
    channel_id, text = announcer.posted[0]
    assert channel_id == 42
    assert "https://outline.example/doc/1" in text
    assert sessions.announced == [1]


async def test_announce_ready_sessions_skips_a_session_not_selected() -> None:
    sessions = FakeSessions([session(1, status="open")])
    announcer = FakeAnnouncer()
    await announce_ready_sessions(sessions, announcer, T0)
    assert announcer.posted == []
    assert sessions.announced == []


async def test_announce_ready_sessions_does_not_stamp_a_session_whose_post_failed() -> None:
    """A failed post must be retried on the next sweep, not silently marked done."""
    sessions = FakeSessions([session(1, channel_id=42)])
    announcer = FakeAnnouncer(fail_channel_ids={42})
    await announce_ready_sessions(sessions, announcer, T0)  # must not raise
    assert announcer.posted == []
    assert sessions.announced == []


async def test_announce_ready_sessions_survives_one_sessions_failure() -> None:
    """One channel being unreachable must not stop every other session's
    announcement in the same sweep.
    """
    sessions = FakeSessions(
        [session(1, channel_id=42), session(2, channel_id=43)],
    )
    announcer = FakeAnnouncer(fail_channel_ids={42})
    await announce_ready_sessions(sessions, announcer, T0)  # must not raise
    assert [channel_id for channel_id, _ in announcer.posted] == [43]
    assert sessions.announced == [2]


# ---------------------------------------------------------------------------
# The silent-audio warning: the other message this system posts into a
# recording channel, rendered through the same engine as the link above.
# ---------------------------------------------------------------------------


def test_the_silent_audio_warning_mentions_the_speaker() -> None:
    """`<@id>` is Discord's mention syntax, and the point of posting publicly.

    The person whose microphone is dead is frequently the last one to
    notice, and a message nobody is addressed by scrolls past unread.
    """
    assert "<@100>" in render_silent_audio_warning(100)


def test_the_silent_audio_warning_says_the_recording_continues() -> None:
    """Naming somebody in the channel is already uncomfortable enough.

    Without this, the obvious reading of the message is "you are not being
    recorded, do something now" -- which is false: everything after the
    warning is still captured, and a fixed microphone simply starts
    arriving audibly.
    """
    assert "Recording continues." in render_silent_audio_warning(100)


# ---------------------------------------------------------------------------
# The mentions: a protocol is written for the people who were in the room,
# so the message carrying its link addresses them by name.
# ---------------------------------------------------------------------------


def test_the_announcement_mentions_every_participant() -> None:
    text = render_announcement("https://outline.example/doc/1", (100, 200))
    assert "<@100>" in text
    assert "<@200>" in text


def test_the_announcement_still_carries_the_link_alongside_the_mentions() -> None:
    """The link is the message; the mentions only decide who is told."""
    text = render_announcement("https://outline.example/doc/1", (100,))
    assert "https://outline.example/doc/1" in text


def test_the_mentions_come_before_the_link_on_their_own_line() -> None:
    """Discord renders a mention as a chip: leading with them is what makes
    the message read as addressed to somebody rather than as a stray link.
    """
    text = render_announcement("https://outline.example/doc/1", (100, 200))
    first_line, _, rest = text.partition("\n")
    assert first_line == "<@100> <@200>"
    assert "https://outline.example/doc/1" in rest


def test_a_session_with_no_known_participants_posts_the_link_alone() -> None:
    """No mentions is a degraded announcement, not a missing one -- and the
    template must not leave a blank first line where they would have been.
    """
    text = render_announcement("https://outline.example/doc/1")
    assert not text.startswith("\n")
    assert "https://outline.example/doc/1" in text
    assert "<@" not in text


def test_a_participant_id_cannot_smuggle_a_broadcast_mention() -> None:
    """`format_mentions` puts every id through `int()`.

    Nothing user-controlled reaches these ids today -- they come from a
    `BigInteger` column -- but this template does not autoescape and its
    output is posted verbatim into a channel, so an id that arrived as a
    string must not be able to carry `@everyone` with it.
    """
    with pytest.raises(ValueError):
        render_announcement("https://outline.example/doc/1", ["@everyone"])  # type: ignore[list-item]


async def test_the_posted_announcement_mentions_the_sessions_participants() -> None:
    """End to end through the sweep: the ids the reader supplies are the
    ids that reach the channel.
    """
    sessions = FakeSessions([session(1, channel_id=42, participant_ids=(100, 200))])
    announcer = FakeAnnouncer()
    await announce_ready_sessions(sessions, announcer, T0)
    _, text = announcer.posted[0]
    assert "<@100>" in text
    assert "<@200>" in text


async def test_a_reader_that_supplies_no_participants_still_gets_the_link_posted() -> None:
    """`participant_ids` is read with `.get`: an implementation of
    `SessionReader` that predates it loses the mentions, not the
    announcement.
    """
    candidate = session(1, channel_id=42)
    del candidate["participant_ids"]
    sessions = FakeSessions([candidate])
    announcer = FakeAnnouncer()
    await announce_ready_sessions(sessions, announcer, T0)
    assert sessions.announced == [1]
    assert "https://outline.example/doc/1" in announcer.posted[0][1]
