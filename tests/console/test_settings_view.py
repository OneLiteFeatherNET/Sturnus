"""What the console is allowed to say about a configuration key.

Every assertion here runs without a database, a request or a Discord
gateway, because every decision it pins is a decision about the *key
registry* and nothing else: which keys exist, what each one's metadata is,
whether it may be cleared, and how long it takes before the running system
is actually using it.

Two of those are worth stating plainly, because getting them wrong is not
a cosmetic defect:

* **`policy_version` invalidates consent.** Bumping it makes every stored
  consent record naming the old version inactive, and role-holders stop
  being recorded mid-session within the consent cache's TTL
  (`docs/operations.md` section 6). The change is allowed; discovering it
  afterwards is not.
* **A stored value is not a value in force.** The console's API process
  has no Discord gateway and cannot reconcile anything, so what a write
  achieves depends entirely on how the *bot* reads that key. The three
  classes are derived from `sturnus.application.reconfigure`, never
  restated here.
"""

from __future__ import annotations

import pytest

from sturnus.console import settings_view
from sturnus.domain import settings

#: What `ConfigStore.snapshot` hands back for a guild with no rows of its
#: own: the defaults, already resolved. The view never resolves a fallback
#: itself -- there is one fallback rule and it lives in the store, where
#: `ConfigStore.get` uses it too.
NOTHING_CONFIGURED: dict[str, str] = dict(settings.DEFAULTS)


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------


def test_the_view_offers_exactly_the_keys_the_store_accepts() -> None:
    """`ConfigStore.set` refuses anything outside `DEFAULTS | REQUIRED_KEYS`.

    A console that offered a key the store refuses would render a field
    that can never be saved; one that hid a key the store accepts would
    make a setting reachable only from Discord. Both are the same bug --
    two lists of keys -- so this one is derived from the store's union
    rather than typed out again.
    """
    assert frozenset(settings.DEFAULTS) | settings.REQUIRED_KEYS == settings_view.KNOWN_KEYS


def test_a_required_key_never_also_has_a_default() -> None:
    """The invariant the clear endpoint rests on.

    `DELETE` restores a default. If a key were both required and
    defaulted, "required" would stop meaning "must be set before this
    guild can go live" -- and if a key were neither, clearing it would
    leave a hole nothing falls back into. The two sets being disjoint, and
    their union being every key, is what makes "may this be cleared?" a
    question with a total answer.
    """
    assert settings.REQUIRED_KEYS.isdisjoint(settings.DEFAULTS)


def test_an_unknown_key_is_not_a_key() -> None:
    assert not settings_view.is_known("delete_everything")


def test_keys_are_listed_in_a_stable_order() -> None:
    """`KNOWN_KEYS` is a frozenset and offers no ordering of its own.

    Unordered, the same configuration renders in a different order on
    every page load, which makes the settings page unreadable and a
    screenshot in a bug report worthless.
    """
    listed = [view.key for view in settings_view.describe_all(NOTHING_CONFIGURED)]
    assert listed == sorted(listed)
    assert set(listed) == settings_view.KNOWN_KEYS


# ---------------------------------------------------------------------------
# Values: what is stored, what is the default, what is neither
# ---------------------------------------------------------------------------


def test_a_fresh_guild_reports_the_default_as_the_value_in_force() -> None:
    """The value the bot would read, not "unset with a default beside it".

    A settings page that showed an empty box next to "default: 15" invites
    somebody to type 15 into it, storing a row that says exactly what the
    absence of a row already said.
    """
    view = settings_view.describe(settings.IDLE_TIMEOUT_MINUTES, NOTHING_CONFIGURED)
    assert view.value == "15"
    assert view.default == "15"


def test_a_value_the_store_resolved_is_reported_beside_the_default_it_replaced() -> None:
    """Both halves, because the console has to offer a way back.

    Without `default`, "reset this to what it was" is a value the front
    end would have to hardcode.
    """
    view = settings_view.describe(
        settings.IDLE_TIMEOUT_MINUTES, {**NOTHING_CONFIGURED, settings.IDLE_TIMEOUT_MINUTES: "45"}
    )
    assert view.value == "45"
    assert view.default == "15"


def test_a_required_key_with_nothing_stored_has_no_value_at_all() -> None:
    """Not the empty string, and not a placeholder: `None`.

    A required key has no default by construction, so "unset" is a real
    third state the console has to render -- and rendering it as an empty
    text box that saves an empty string would store a value the bot then
    fails to parse.
    """
    view = settings_view.describe(settings.VOICE_CHANNEL_ID, NOTHING_CONFIGURED)
    assert view.value is None
    assert view.default is None


def test_a_key_stored_by_hand_that_nobody_reads_is_not_shown() -> None:
    """A row can reach `guild_config` by a direct `UPDATE`.

    `ConfigStore.set` refuses an unknown key, but nothing stops an
    operator writing one straight into the table. The console lists the
    registry, not the table, so such a row is invisible here rather than
    appearing as a field the API would then refuse to save.
    """
    stored = {**NOTHING_CONFIGURED, "left_over_from_2024": "yes"}
    listed = {view.key for view in settings_view.describe_all(stored)}
    assert "left_over_from_2024" not in listed


# ---------------------------------------------------------------------------
# Metadata the form needs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["voice_channel_id", "policy_url"])
def test_a_key_with_no_default_is_marked_required(key: str) -> None:
    assert settings_view.describe(key, NOTHING_CONFIGURED).required is True


@pytest.mark.parametrize("key", ["timezone", "document_provider"])
def test_a_key_with_a_default_is_not_required(key: str) -> None:
    assert settings_view.describe(key, NOTHING_CONFIGURED).required is False


@pytest.mark.parametrize("key", ["idle_timeout_minutes", "admin_role_id"])
def test_a_key_the_store_parses_as_a_number_is_marked_integer(key: str) -> None:
    """So the console can refuse the typo before the round trip.

    A rendering hint and never a control: `ConfigStore.set` is what
    actually rejects a non-integer, and the API turns that into a 400 --
    see `tests/console/test_settings_routes.py`.
    """
    assert settings_view.describe(key, NOTHING_CONFIGURED).integer is True


@pytest.mark.parametrize("key", ["timezone", "transcription_prompt"])
def test_a_free_text_key_is_not_marked_integer(key: str) -> None:
    assert settings_view.describe(key, NOTHING_CONFIGURED).integer is False


# ---------------------------------------------------------------------------
# The warning that must not be discovered afterwards
# ---------------------------------------------------------------------------


def test_the_policy_version_is_flagged_as_invalidating_consent() -> None:
    """Documented behaviour, surfaced as a flag rather than as prose.

    A sentence in a response body is a sentence a front end may or may not
    render. A boolean is something it has to decide what to do with.
    """
    assert settings_view.describe("policy_version", NOTHING_CONFIGURED).invalidates_consent is True


def test_no_other_key_carries_the_consent_warning() -> None:
    """Because a warning on everything is a warning on nothing.

    `audio_retention_days` is the near miss: `docs/operations.md` says to
    bump `policy_version` when it changes, but changing it does not
    itself invalidate anything -- the bump does.
    """
    flagged = {
        view.key
        for view in settings_view.describe_all(NOTHING_CONFIGURED)
        if view.invalidates_consent
    }
    assert flagged == {"policy_version"}


# ---------------------------------------------------------------------------
# Stored is not the same as in force
# ---------------------------------------------------------------------------


def test_a_key_the_bot_caches_takes_effect_on_the_next_reconcile() -> None:
    """The console cannot reconcile, so it must not claim the value is live.

    `/config set` writes *and* reconciles, then reports what actually
    happened. The API process has no gateway and can only do the first
    half, so the honest answer is "the bot picks this up on its next pass"
    -- which `SturnusClient._tick_guild` runs every ten seconds.
    """
    view = settings_view.describe(settings.EMPTY_GRACE_SECONDS, NOTHING_CONFIGURED)
    assert view.takes_effect == settings_view.NEXT_RECONCILE


def test_a_key_read_once_at_process_start_says_a_restart_is_needed() -> None:
    """Otherwise the operator waits forever for a change that cannot land."""
    view = settings_view.describe(settings.PUBLISH_POLL_SECONDS, NOTHING_CONFIGURED)
    assert view.takes_effect == settings_view.PROCESS_RESTART


def test_a_key_read_per_use_is_in_force_at_once() -> None:
    """`policy_version` is not cached by the reconcile pass at all.

    `ConsentCache` re-reads it per check behind a five second TTL, so
    telling the operator to wait for a reconcile that never touches this
    key would be its own small lie.
    """
    view = settings_view.describe(settings.POLICY_VERSION, NOTHING_CONFIGURED)
    assert view.takes_effect == settings_view.IMMEDIATELY


def test_a_key_that_names_the_channel_may_wait_for_the_recording_in_progress() -> None:
    """The one thing a reconcile deliberately will not do at once.

    `sturnus.application.reconfigure` defers the identity keys while a
    session is recording rather than dropping a live voice connection --
    a recording is never discarded to make a setting land sooner. The
    console has no way to know whether a session is in progress, so it
    says the change *may* wait rather than pretending it will not.
    """
    view = settings_view.describe(settings.VOICE_CHANNEL_ID, NOTHING_CONFIGURED)
    assert view.deferred_while_recording is True


def test_a_tunable_never_waits_for_the_recording_in_progress() -> None:
    assert (
        settings_view.describe(
            settings.IDLE_TIMEOUT_MINUTES, NOTHING_CONFIGURED
        ).deferred_while_recording
        is False
    )


# ---------------------------------------------------------------------------
# Clearing
# ---------------------------------------------------------------------------


def test_a_required_key_may_not_be_cleared() -> None:
    """There is nothing to fall back to, and the guild would stop recording."""
    assert settings_view.may_clear("policy_url") is False


def test_a_defaulted_key_may_be_cleared() -> None:
    assert settings_view.may_clear("timezone") is True


def test_every_clearable_key_has_something_to_fall_back_to() -> None:
    """The property that makes `DELETE` a restore rather than a deletion."""
    for key in settings_view.KNOWN_KEYS:
        if settings_view.may_clear(key):
            assert settings_view.describe(key, NOTHING_CONFIGURED).default is not None
