from datetime import UTC, datetime

from sturnus.application.publishing import sessions_to_announce

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


def session(
    session_id: int,
    status: str = "documented",
    document_url: str | None = "https://outline.example/doc/1",
    announced: datetime | None = None,
) -> dict[str, object]:
    return {
        "id": session_id,
        "status": status,
        "document_url": document_url,
        "announced_at": announced,
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
