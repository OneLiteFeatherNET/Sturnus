"""Which setup intent the bot applies, and the link that gets it invited.

Two decisions live in `sturnus.domain.onboarding`, and both of them are
about something the console cannot do for itself.

The first is what happens when a guild has more than one unapplied
intent. The tick runs six times a minute; two administrators asking
thirty seconds apart, or one impatient person pressing twice, must not
leave the bot configuring a guild twice in a row and finishing on
whichever request happened to be older. The rule is that the newest ask
wins and the rest settle as superseded -- see `select_intent`.

The second is the invite URL. It is the one onboarding step that is
genuinely web-doable, because a `bot`-scope authorize link is public and
buildable from the application's client id alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from sturnus.domain.onboarding import (
    APPLIED,
    FAILED,
    INVITE_PERMISSIONS,
    INVITE_SCOPES,
    OUTCOMES,
    SUPERSEDED,
    SetupIntent,
    invite_url,
    select_intent,
)

GUILD = 1
ANNA, BEN = 100, 200
T0 = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)

#: A real Discord application id: nineteen digits, past 2^53.
CLIENT_ID = "1289374650912837465"


def intent(
    intent_id: int,
    *,
    at: datetime = T0,
    by: int = ANNA,
    channel_ids: str | None = "10",
    outcome: str | None = None,
) -> SetupIntent:
    return SetupIntent(
        id=intent_id,
        guild_id=GUILD,
        requested_by=by,
        requested_at=at,
        channel_ids=channel_ids,
        consent_role_name=None,
        applied_at=None if outcome is None else at,
        outcome=outcome,
        error=None,
    )


def minutes(count: int) -> timedelta:
    return timedelta(minutes=count)


# ---------------------------------------------------------------------------
# Which intent the bot applies
# ---------------------------------------------------------------------------


def test_a_guild_with_nothing_pending_has_nothing_to_apply() -> None:
    selection = select_intent(())
    assert selection.apply is None
    assert selection.supersede == ()


def test_one_pending_intent_is_the_one_applied() -> None:
    only = intent(1)
    selection = select_intent((only,))
    assert selection.apply is only
    assert selection.supersede == ()


def test_the_newest_of_two_contradicting_intents_is_the_one_applied() -> None:
    """The rule this module exists for.

    Two administrators asking thirty seconds apart are not a queue of two
    jobs -- an intent says what should be *true*, and two statements of
    what should be true do not compose. Applying both in request order
    would end on the older one's channel list, which is precisely the
    correction being overwritten by the mistake it corrected.
    """
    first = intent(1, at=T0, by=ANNA)
    second = intent(2, at=T0 + minutes(1), by=BEN)

    selection = select_intent((first, second))

    assert selection.apply is second
    assert selection.supersede == (first,)


def test_the_order_it_is_handed_does_not_decide_which_wins() -> None:
    """`requested_at` decides, not the sequence the caller read rows in."""
    first = intent(1, at=T0)
    second = intent(2, at=T0 + minutes(1))
    assert select_intent((second, first)).apply is second


def test_two_asks_in_the_same_instant_are_broken_by_the_row_that_was_written_last() -> None:
    """Two clicks a clock cannot separate still have to have an answer.

    `requested_at` is written by the caller's clock, so a pinned clock or
    a coarse one can produce two rows sharing an instant. The id is
    monotonic, so it settles the tie in the same direction the timestamp
    would have.
    """
    first = intent(1, at=T0)
    second = intent(2, at=T0)
    assert select_intent((first, second)).apply is second


def test_every_older_intent_is_superseded_not_only_the_previous_one() -> None:
    """Three asks leave one application and two settled rows, never a queue."""
    first, second, third = (
        intent(1, at=T0),
        intent(2, at=T0 + minutes(1)),
        intent(3, at=T0 + minutes(2)),
    )
    selection = select_intent((first, second, third))
    assert selection.apply is third
    assert [each.id for each in selection.supersede] == [1, 2]


def test_an_already_settled_row_is_neither_applied_nor_settled_again() -> None:
    """A settled intent is finished with, whatever list it arrives in.

    The store only ever hands over unapplied rows, so this is belt and
    braces -- but re-settling a row is how an `applied` outcome would be
    quietly rewritten to `superseded` and the audit trail lost.
    """
    settled = intent(1, at=T0 + minutes(5), outcome=APPLIED)
    pending = intent(2, at=T0)

    selection = select_intent((settled, pending))

    assert selection.apply is pending
    assert selection.supersede == ()


def test_superseded_is_a_terminal_outcome_like_the_other_two() -> None:
    """It settles the row: the bot never comes back to it."""
    assert SUPERSEDED in OUTCOMES
    assert {APPLIED, FAILED, SUPERSEDED} == OUTCOMES


# ---------------------------------------------------------------------------
# The invite link
# ---------------------------------------------------------------------------


def test_the_invite_link_carries_the_scopes_and_permissions_setup_needs() -> None:
    """`Manage Roles` is the one that fails late if it is left out.

    It covers both halves of what `/setup` does -- creating the consent
    role and writing the Speak overwrites -- and a bot invited without it
    joins, mirrors, and then fails the first intent it is handed.
    """
    parsed = urlparse(invite_url(CLIENT_ID))
    query = parse_qs(parsed.query)

    assert parsed.netloc == "discord.com"
    assert query["client_id"] == [CLIENT_ID]
    assert query["permissions"] == [INVITE_PERMISSIONS]
    assert query["scope"] == [" ".join(INVITE_SCOPES)]


def test_the_permissions_are_the_bitmask_the_deployment_guide_states() -> None:
    """View Channel, Connect, Send Messages, Manage Roles.

    Pinned as the literal rather than recomputed, because the guide an
    operator follows by hand (`docs/first-deployment.md` section 2) states
    this number and the two must not drift.
    """
    assert INVITE_PERMISSIONS == "269487104"


def test_a_client_id_that_is_not_a_snowflake_is_refused() -> None:
    """The one value here that reaches a URL somebody is asked to open.

    An application id is digits. Anything else is a misconfiguration, and
    building a link out of it would put whatever was configured into a
    query string the console hands to an administrator to click.
    """
    with pytest.raises(ValueError):
        invite_url("not-a-snowflake")


def test_a_blank_client_id_is_refused_for_the_same_reason() -> None:
    with pytest.raises(ValueError):
        invite_url("")
