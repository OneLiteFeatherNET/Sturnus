"""What an accumulating run of decode failures means (Spec 6.1).

Pure: no libopus, no Discord, no clock. That is the point of putting the
policy in `domain` -- the decision that the production incident turned on
(is this one bad frame, or has this stream stopped working?) is decidable
logic over counters, and it can therefore be pinned exhaustively.

The property these tests exist to protect is that escalation is
*edge-triggered*. A stream failing at 50 frames per second must produce
one WARNING, not three thousand log lines a minute -- an alert nobody can
read is the same as no alert, which is how the original failure stayed
invisible for a whole session.
"""

from __future__ import annotations

import pytest

from sturnus.domain.stream_health import (
    FRAMES_PER_SECOND,
    DecodePolicy,
    StreamHealth,
    StreamState,
)

CORRUPTED_STREAM = -4  # OpusError(-4), literally "corrupted stream"


def fail(health: StreamHealth, times: int) -> list[StreamState | None]:
    return [health.record_discarded(CORRUPTED_STREAM) for _ in range(times)]


def test_isolated_failures_stay_healthy_and_report_nothing() -> None:
    health = StreamHealth()
    health.record_decoded()

    transitions = fail(health, DecodePolicy().degraded_after_consecutive - 1)

    assert transitions == [None] * len(transitions)
    assert health.state is StreamState.HEALTHY


def test_degraded_is_reported_exactly_once_however_long_the_run_lasts() -> None:
    policy = DecodePolicy()
    health = StreamHealth(policy)
    health.record_decoded()

    transitions = fail(health, policy.degraded_after_consecutive)
    assert transitions[-1] is StreamState.DEGRADED
    assert transitions[:-1] == [None] * (policy.degraded_after_consecutive - 1)

    # A full second of further failures at Discord's frame rate.
    assert fail(health, FRAMES_PER_SECOND) == [None] * FRAMES_PER_SECOND
    assert health.state is StreamState.DEGRADED


def test_unusable_is_reported_once_after_five_seconds_of_failure() -> None:
    policy = DecodePolicy()
    health = StreamHealth(policy)
    health.record_decoded()

    transitions = fail(health, policy.unusable_after_consecutive)

    assert transitions.count(StreamState.DEGRADED) == 1
    assert transitions.count(StreamState.UNUSABLE) == 1
    assert transitions[-1] is StreamState.UNUSABLE
    assert health.state is StreamState.UNUSABLE


def test_one_success_clears_the_run_but_keeps_the_totals() -> None:
    policy = DecodePolicy()
    health = StreamHealth(policy)
    health.record_decoded()
    fail(health, policy.degraded_after_consecutive)

    health.record_decoded()

    stats = health.stats()
    assert stats.consecutive_failures == 0
    assert stats.state is StreamState.HEALTHY
    # The history is not rewritten: the speaker still lost that audio, and
    # the published protocol has to be able to say so.
    assert stats.frames_discarded == policy.degraded_after_consecutive
    assert stats.frames_decoded == 2
    assert stats.frames_seen == policy.degraded_after_consecutive + 2


def test_a_stream_that_never_decoded_reports_never_decoded_not_degraded() -> None:
    """A different diagnosis, on purpose.

    `NEVER_DECODED` means the input shape is wrong -- the wrong bytes
    reached the decoder, or Discord changed the payload -- not that one
    stream went bad partway through. Reporting `DEGRADED` first would send
    somebody looking at the network.
    """
    policy = DecodePolicy()
    health = StreamHealth(policy)

    transitions = fail(health, policy.never_decoded_after)

    assert StreamState.DEGRADED not in transitions
    assert transitions[-1] is StreamState.NEVER_DECODED
    assert transitions[:-1] == [None] * (policy.never_decoded_after - 1)
    assert health.state is StreamState.NEVER_DECODED


def test_never_decoded_does_not_earn_a_decoder_rebuild() -> None:
    """A fresh decoder cannot help bytes that were never decodable."""
    policy = DecodePolicy()
    health = StreamHealth(policy)
    fail(health, policy.never_decoded_after)

    assert health.state is StreamState.NEVER_DECODED
    assert health.may_recycle is False


def test_unusable_earns_exactly_one_rebuild_over_the_stream_lifetime() -> None:
    policy = DecodePolicy()
    health = StreamHealth(policy)
    health.record_decoded()
    fail(health, policy.unusable_after_consecutive)
    assert health.may_recycle is True

    health.record_recycled()

    # The fresh decoder is judged on its own record...
    assert health.state is StreamState.HEALTHY
    assert health.stats().consecutive_failures == 0
    # ...but the budget is spent for good, so a permanent fault cannot
    # turn into a permanent, invisible retry loop.
    assert fail(health, policy.unusable_after_consecutive)[-1] is StreamState.UNUSABLE
    assert health.may_recycle is False


def test_concealment_is_capped_and_re_armed_by_a_successful_decode() -> None:
    policy = DecodePolicy()
    health = StreamHealth(policy)
    health.record_decoded()

    verdicts = [health.record_lost() for _ in range(policy.conceal_max_consecutive + 2)]
    assert verdicts == [True] * policy.conceal_max_consecutive + [False, False]

    health.record_decoded()
    assert health.record_lost() is True


def test_nothing_is_concealed_before_the_stream_has_ever_decoded() -> None:
    """libopus reconstructs a lost frame from its memory of the previous one.

    A decoder that has never seen a real frame has nothing to reconstruct
    from, so concealing there would be inventing audio outright.
    """
    health = StreamHealth()

    assert health.record_lost() is False


def test_lost_frames_alone_never_escalate() -> None:
    """Packet loss says nothing about whether the stream is decodable."""
    policy = DecodePolicy()
    health = StreamHealth(policy)

    for _ in range(policy.never_decoded_after * 2):
        health.record_lost()

    assert health.state is StreamState.HEALTHY


def test_stats_report_the_audio_that_never_reached_the_file() -> None:
    health = StreamHealth()
    health.record_decoded()
    fail(health, 25)
    for _ in range(5):
        if health.record_lost():
            health.record_concealed()

    stats = health.stats()
    assert stats.frames_discarded == 25
    assert stats.frames_lost == 5
    assert stats.frames_concealed == 5
    assert stats.last_error_code == CORRUPTED_STREAM
    # 25 discarded frames, five lost but concealed: half a second gone.
    assert stats.lost_seconds == pytest.approx(0.5)


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"degraded_after_consecutive": 0}, "degraded_after_consecutive"),
        ({"unusable_after_consecutive": 0}, "unusable_after_consecutive"),
        ({"never_decoded_after": 0}, "never_decoded_after"),
        ({"conceal_max_consecutive": 0}, "conceal_max_consecutive"),
        ({"recycle_attempts": -1}, "recycle_attempts"),
        # A policy that reaches UNUSABLE without ever reporting DEGRADED
        # escalates in the wrong order, which fails exactly the way the
        # incident did: quietly.
        (
            {"degraded_after_consecutive": 20, "unusable_after_consecutive": 20},
            "must exceed",
        ),
    ],
)
def test_a_policy_that_cannot_escalate_is_rejected(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        DecodePolicy(**kwargs)
