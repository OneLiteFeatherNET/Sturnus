"""Reading a guild's list of recording channels, and the key it was renamed from.

Pure: `sturnus.domain.settings` imports nothing but the standard library,
so the deprecation -- "the plural key wins, the singular one still works
forever" -- is pinned down here rather than being re-derived by each of
the three callers that has to honour it.
"""

from __future__ import annotations

import pytest

from sturnus.domain import settings


def test_a_list_of_channels_is_read_as_every_channel_it_names() -> None:
    assert settings.recording_channel_ids({settings.VOICE_CHANNEL_IDS: "10,20,30"}) == (10, 20, 30)


def test_spaces_around_the_commas_are_how_people_actually_type_a_list() -> None:
    assert settings.recording_channel_ids({settings.VOICE_CHANNEL_IDS: " 10 , 20 "}) == (10, 20)


def test_the_order_a_list_was_typed_in_carries_no_meaning() -> None:
    """Sorted, so re-ordering a list is not mistaken for changing it.

    Which channel is served is decided by who is sitting in them, never by
    where an administrator put it in the list -- so a re-order that reads
    as an identity change would retarget a guild for nothing.
    """
    assert settings.recording_channel_ids({settings.VOICE_CHANNEL_IDS: "30,10,20"}) == (10, 20, 30)


def test_a_guild_configured_before_the_rename_still_records() -> None:
    """The whole reason the singular key stays readable: no migration."""
    assert settings.recording_channel_ids({settings.VOICE_CHANNEL_ID: "10"}) == (10,)


def test_the_list_wins_over_the_key_it_replaced() -> None:
    """A stale row nobody deleted must not overrule a deliberate write."""
    snapshot = {settings.VOICE_CHANNEL_IDS: "20,30", settings.VOICE_CHANNEL_ID: "10"}
    assert settings.recording_channel_ids(snapshot) == (20, 30)


def test_a_guild_that_has_configured_neither_key_is_allowed_nowhere() -> None:
    """An answer, not an error -- it is what leads to a teardown."""
    assert settings.recording_channel_ids({}) == ()


@pytest.mark.parametrize("value", ["ten", "10,twenty", "10,,20", "", "   ", "10,-20", "0"])
def test_a_list_that_cannot_be_read_is_refused_rather_than_repaired(value: str) -> None:
    with pytest.raises(settings.InvalidChannelList):
        settings.recording_channel_ids({settings.VOICE_CHANNEL_IDS: value})


def test_naming_one_channel_twice_is_reported_rather_than_collapsed() -> None:
    """Always a mistake, and one the person who made it should hear about."""
    with pytest.raises(settings.InvalidChannelList, match="more than once"):
        settings.recording_channel_ids({settings.VOICE_CHANNEL_IDS: "10,20,10"})


def test_the_stored_spelling_round_trips_through_the_parser() -> None:
    rendered = settings.render_channel_ids((30, 10, 20))
    assert settings.parse_channel_ids(rendered) == (10, 20, 30)


def test_a_guild_with_neither_channel_key_is_told_to_set_the_list() -> None:
    """Reported under the plural key -- naming the deprecated one would
    tell an administrator to configure a setting we are moving off."""
    missing = settings.missing_required({})
    assert settings.VOICE_CHANNEL_IDS in missing
    assert settings.VOICE_CHANNEL_ID not in missing


def test_the_old_key_alone_still_satisfies_the_requirement() -> None:
    missing = settings.missing_required({settings.VOICE_CHANNEL_ID: "10"})
    assert settings.VOICE_CHANNEL_IDS not in missing


def test_the_new_key_alone_satisfies_the_requirement() -> None:
    missing = settings.missing_required({settings.VOICE_CHANNEL_IDS: "10,20"})
    assert settings.VOICE_CHANNEL_IDS not in missing


def test_every_other_required_key_is_still_reported_on_its_own() -> None:
    missing = settings.missing_required({settings.VOICE_CHANNEL_IDS: "10"})
    assert missing == settings.REQUIRED_KEYS - {settings.VOICE_CHANNEL_IDS}


def test_the_deprecated_key_is_still_a_key_the_store_accepts() -> None:
    """It must stay writable and clearable, or a guild cannot move off it."""
    assert settings.VOICE_CHANNEL_ID in settings.KNOWN_KEYS
    assert settings.VOICE_CHANNEL_ID not in settings.REQUIRED_KEYS


def test_a_write_to_the_old_name_is_recognised_as_a_write_to_the_new_one() -> None:
    assert settings.canonical_key(settings.VOICE_CHANNEL_ID) == settings.VOICE_CHANNEL_IDS
    assert settings.canonical_key(settings.CONSENT_ROLE_ID) == settings.CONSENT_ROLE_ID
