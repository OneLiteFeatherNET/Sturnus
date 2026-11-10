"""Decisions behind the channel prompt: who is asked, and who is asked again.

No clock is read here. Every instant is built from `T0`, so each test is
the same on every run and at any time of day.
"""

from datetime import UTC, datetime, timedelta

from sturnus.application.consent_prompt import PromptNeeds, PromptRecord, needs_prompt
from sturnus.domain.consent import ConsentRecord

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(days=1)
POLICY = "2026-08-01"
CHANNEL = 111
OTHER_CHANNEL = 222
ANNA = 100
BEN = 200


def consented(version: str = POLICY, revoked_at: datetime | None = None) -> ConsentRecord:
    return ConsentRecord(granted_at=T0, revoked_at=revoked_at, policy_version=version)


def test_a_member_without_consent_is_prompted() -> None:
    needs = needs_prompt(None, linked=False, policy_version=POLICY, now=NOW)
    assert needs.consent is True, "a member with no consent record must be asked"


def test_a_member_with_active_consent_and_a_link_is_not_prompted() -> None:
    needs = needs_prompt(consented(), linked=True, policy_version=POLICY, now=NOW)
    assert needs == PromptNeeds(consent=False, link=False)
    assert needs.any is False


def test_a_member_with_consent_and_no_link_is_prompted_for_the_link_only() -> None:
    needs = needs_prompt(consented(), linked=False, policy_version=POLICY, now=NOW)
    assert needs == PromptNeeds(consent=False, link=True)


def test_consent_given_under_an_old_policy_version_is_prompted_again() -> None:
    needs = needs_prompt(
        consented(version="2026-01-01"), linked=True, policy_version=POLICY, now=NOW
    )
    assert needs.consent is True


def test_revoked_consent_is_prompted_again() -> None:
    revoked = consented(revoked_at=NOW - timedelta(seconds=1))
    needs = needs_prompt(revoked, linked=True, policy_version=POLICY, now=NOW)
    assert needs.consent is True


def test_active_consent_is_not_prompted_weeks_later() -> None:
    """Consent lasts until revoked or the policy changes, never by elapsed time."""
    much_later = T0 + timedelta(weeks=8)
    needs = needs_prompt(consented(), linked=True, policy_version=POLICY, now=much_later)
    assert needs.any is False, "a join weeks later under the same policy must not ping"


def test_a_new_prompt_record_has_no_entries() -> None:
    record = PromptRecord()
    assert record.should_prompt(CHANNEL, ANNA) is True


def test_a_second_join_while_the_channel_is_occupied_is_not_prompted() -> None:
    record = PromptRecord()
    record.mark(CHANNEL, ANNA)
    assert record.should_prompt(CHANNEL, ANNA) is False


def test_the_same_person_in_another_channel_is_prompted() -> None:
    record = PromptRecord()
    record.mark(CHANNEL, ANNA)
    assert record.should_prompt(OTHER_CHANNEL, ANNA) is True


def test_another_person_in_the_same_channel_is_prompted() -> None:
    record = PromptRecord()
    record.mark(CHANNEL, ANNA)
    assert record.should_prompt(CHANNEL, BEN) is True


def test_clearing_the_channel_makes_the_next_join_prompt_again() -> None:
    record = PromptRecord()
    record.mark(CHANNEL, ANNA)
    record.clear_channel(CHANNEL)
    assert record.should_prompt(CHANNEL, ANNA) is True


def test_clearing_one_channel_leaves_the_others_marked() -> None:
    record = PromptRecord()
    record.mark(CHANNEL, ANNA)
    record.mark(OTHER_CHANNEL, ANNA)
    record.clear_channel(CHANNEL)
    assert record.should_prompt(OTHER_CHANNEL, ANNA) is False


def test_the_message_reference_is_kept_per_channel_and_person() -> None:
    record = PromptRecord()
    record.mark(CHANNEL, ANNA, message_ref="message-1")
    assert record.message_ref(CHANNEL, ANNA) == "message-1"
    assert record.message_ref(CHANNEL, BEN) is None


def test_clearing_a_channel_drops_its_message_references() -> None:
    record = PromptRecord()
    record.mark(CHANNEL, ANNA, message_ref="message-1")
    record.clear_channel(CHANNEL)
    assert record.message_ref(CHANNEL, ANNA) is None
