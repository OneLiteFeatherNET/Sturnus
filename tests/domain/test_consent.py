"""What a consent covers, and when it stops covering it.

Two things changed here and both are about honesty rather than features.
`scope` exists so a record can say that somebody agreed to audio and not
to video, before anything in this system is capable of recording video at
all. `revoked_at` became an instant rather than a tombstone, so "withdraw
from the end of the month" is a thing the schema can hold.

Both are read against a `now` the caller supplies. The domain has no
clock and must not grow one: a rule that decides whether consent is in
force from a time it read itself is a rule no test can pin at the
boundary that matters, which is precisely the second before and the
second after.
"""

from datetime import UTC, datetime, timedelta

import pytest

from sturnus.domain.consent import (
    ConsentRecord,
    ConsentScope,
    is_consent_active,
    may_record,
    may_record_video,
    scope_of,
)

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
LATER = T0 + timedelta(days=7)
POLICY = "2026-08-01"


def granted(
    version: str = POLICY,
    *,
    revoked_at: datetime | None = None,
    scope: ConsentScope = ConsentScope.AUDIO,
) -> ConsentRecord:
    return ConsentRecord(granted_at=T0, revoked_at=revoked_at, policy_version=version, scope=scope)


def test_granted_consent_is_active() -> None:
    assert is_consent_active(granted(), POLICY, LATER) is True


def test_missing_record_is_not_active() -> None:
    assert is_consent_active(None, POLICY, LATER) is False


def test_outdated_policy_version_invalidates_consent() -> None:
    assert is_consent_active(granted("2026-01-01"), POLICY, LATER) is False


def test_recording_requires_both_role_and_consent() -> None:
    # The role check alone isn't enough: administrators bypass channel
    # permissions, which is why the record is also checked.
    assert may_record(granted(), POLICY, has_consent_role=True, now=LATER) is True
    assert may_record(granted(), POLICY, has_consent_role=False, now=LATER) is False
    assert may_record(None, POLICY, has_consent_role=True, now=LATER) is False


def test_blank_current_policy_version_is_never_active() -> None:
    # A record with policy_version=None is forbidden by the schema, and the
    # type signature forbids passing None as current_policy_version - but an
    # empty string satisfies both and must not be treated as "no policy set
    # yet equals no policy required".
    record = ConsentRecord(granted_at=T0, revoked_at=None, policy_version="")
    assert is_consent_active(record, "", LATER) is False


def test_a_naive_current_time_is_refused_rather_than_guessed_at() -> None:
    """The convention `sturnus.domain._time` already establishes.

    A naive datetime compared against a stored `revoked_at` either raises
    deep inside the comparison or silently means a different moment in
    every zone that reads it. Neither is a thing to discover from a
    revocation that did not take effect.
    """
    with pytest.raises(ValueError):
        is_consent_active(granted(), POLICY, datetime(2026, 8, 19, 20, 0, 0))


# ---------------------------------------------------------------------------
# `revoked_at` as an effective instant
# ---------------------------------------------------------------------------


def test_a_revocation_dated_next_week_still_permits_recording_today() -> None:
    """The whole point of the change, in one assertion.

    Before this, any non-null `revoked_at` meant "not active", so a
    scheduled withdrawal was indistinguishable from an immediate one and
    the only way to express "from the end of the month" was to remember to
    come back and press the button.
    """
    record = granted(revoked_at=LATER)
    assert is_consent_active(record, POLICY, T0 + timedelta(days=1)) is True


def test_a_revocation_dated_last_week_has_already_ended_the_consent() -> None:
    record = granted(revoked_at=T0 + timedelta(days=1))
    assert is_consent_active(record, POLICY, LATER) is False


def test_consent_stops_at_the_instant_itself_rather_than_after_it() -> None:
    """The boundary, pinned in both directions.

    A revocation "effective at 14:00" that still permitted the 14:00:00
    frame would be a system that recorded one packet past a decision
    somebody made, every time -- and the argument for why that is fine is
    an argument nobody should have to make.
    """
    record = granted(revoked_at=LATER)
    assert is_consent_active(record, POLICY, LATER - timedelta(microseconds=1)) is True
    assert is_consent_active(record, POLICY, LATER) is False


def test_revoked_user_with_stale_role_may_not_be_recorded() -> None:
    assert may_record(granted(revoked_at=T0), POLICY, has_consent_role=True, now=LATER) is False


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_a_record_that_does_not_say_what_it_covers_covers_audio() -> None:
    """Every row written before the column existed was an audio grant."""
    assert ConsentRecord(granted_at=T0, revoked_at=None, policy_version=POLICY).scope is (
        ConsentScope.AUDIO
    )


def test_consenting_to_audio_is_not_consenting_to_video() -> None:
    record = granted(scope=ConsentScope.AUDIO)
    assert may_record(record, POLICY, has_consent_role=True, now=LATER) is True
    assert may_record_video(record, POLICY, has_consent_role=True, now=LATER) is False


def test_consenting_to_video_covers_audio_as_well() -> None:
    """There is no video-without-audio scope, and this is why one is not needed."""
    record = granted(scope=ConsentScope.AUDIO_VIDEO)
    assert may_record(record, POLICY, has_consent_role=True, now=LATER) is True
    assert may_record_video(record, POLICY, has_consent_role=True, now=LATER) is True


def test_video_consent_needs_everything_audio_consent_needs() -> None:
    """The wider scope is not a way around the checks the narrow one passes.

    A superseded policy, a missing role and a withdrawal each end video
    consent exactly as they end audio consent -- `may_record_video` is
    `may_record` plus a scope, in that order, rather than a second rule
    beside it.
    """
    wide = granted(scope=ConsentScope.AUDIO_VIDEO)
    assert may_record_video(wide, POLICY, has_consent_role=False, now=LATER) is False
    assert (
        may_record_video(
            granted("2026-01-01", scope=ConsentScope.AUDIO_VIDEO),
            POLICY,
            has_consent_role=True,
            now=LATER,
        )
        is False
    )
    assert (
        may_record_video(
            granted(revoked_at=T0, scope=ConsentScope.AUDIO_VIDEO),
            POLICY,
            has_consent_role=True,
            now=LATER,
        )
        is False
    )


def test_nobody_without_a_record_may_have_their_video_asked_for() -> None:
    assert may_record_video(None, POLICY, has_consent_role=True, now=LATER) is False


def test_a_scope_this_code_cannot_name_reads_as_the_narrow_one() -> None:
    """Whatever goes wrong, it goes wrong towards recording less.

    A row carrying a scope nothing here understands is a row about which
    nothing is known, and the two ways of being wrong about it are not
    symmetric: one costs somebody a capability they can ask for again, the
    other has the bot ask Discord for a camera on the strength of a string
    it cannot read.
    """
    assert scope_of("audio_video_and_screen") is ConsentScope.AUDIO
    assert scope_of(None) is ConsentScope.AUDIO
    assert scope_of("audio_video") is ConsentScope.AUDIO_VIDEO
