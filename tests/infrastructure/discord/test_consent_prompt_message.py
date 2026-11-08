"""The consent prompt posted in a recording channel (tasks 3.1-3.6).

Every test builds its own services, fake stores and fake interaction, and
reads time only from the fake clock. The discord objects are real where
the code only reads ids (`discord.Object`) and fakes where a method is
awaited (`send_message`, `send_modal`, `edit`). Nothing touches a gateway.

Each callback is invoked directly, the way Discord's dispatch would call
it. That exercises the decision the button makes without a connection.
"""

# The fakes mirror the real protocols' parameter names, which the callers pass by
# keyword or position; the arguments they do not read are part of that contract.
# ruff: noqa: ARG002

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import discord
import pytest
from discord import ui

from sturnus.application.consent_flow import GrantOutcome
from sturnus.application.consent_prompt import PromptNeeds, PromptRecord
from sturnus.domain.consent import ConsentRecord
from sturnus.infrastructure.discord.consent_prompt import (
    CONSENT_BUTTON_ID,
    LINK_BUTTON_ID,
    PROMPT_DYNAMIC_ITEMS,
    ConsentPromptConsentButton,
    ConsentPromptLinkButton,
    ConsentPromptModal,
    ConsentPromptServices,
    build_prompt,
    submit_consent,
)
from sturnus.infrastructure.discord.views import CONSENT_STRINGS

T0 = datetime(2026, 11, 8, 20, 0, 0, tzinfo=UTC)
POLICY = "2026-11-01"
HOST = "transcribe.example.invalid"
POLICY_URL = "https://example.invalid/policy"
GUILD = 500
CHANNEL = 600
JOINER = 100
PRESSER = 200
ROLE_MENTION = "<@&700>"
AUTHORIZE = "https://auth.example.invalid/authorize"
CONSENT_ALREADY = "Consent is already recorded for you under the current policy."
NOT_RECORDED = "No consent was recorded. Tick the box to agree, then submit again."


class FakeClock:
    def now(self) -> datetime:
        return T0


class FakeConsents:
    """Stands in for `ConsentRepository`: one current record, grants recorded."""

    def __init__(self, record: ConsentRecord | None = None) -> None:
        self.record = record
        self.grants: list[tuple[int, int, str, str]] = []

    async def current(
        self,
        discord_user_id: int,
        guild_id: int,
    ) -> ConsentRecord | None:
        return self.record

    async def record_grant(
        self,
        *,
        discord_user_id: int,
        guild_id: int,
        policy_version: str,
        source: str,
        now: datetime,
    ) -> None:
        self.grants.append((discord_user_id, guild_id, policy_version, source))


class FakeLinks:
    def __init__(self, identity: tuple[str, str] | None = None) -> None:
        self._identity = identity

    async def external_identity(
        self,
        discord_user_id: int,
        provider: str | None = None,
    ) -> tuple[str, str] | None:
        return self._identity


class FakeStates:
    def __init__(self) -> None:
        self.issued: list[tuple[int, str]] = []

    async def issue(
        self,
        discord_user_id: int,
        provider: str,
        now: datetime,
        ttl: timedelta,
    ) -> str:
        self.issued.append((discord_user_id, provider))
        return f"state-for-{discord_user_id}"


class FakeOAuth:
    def authorize_url(self, state: str) -> str:
        return f"{AUTHORIZE}?state={state}"


class FakeMessage:
    """The prompt message as stored in `PromptRecord`; records each edit."""

    def __init__(self) -> None:
        self.edits: list[tuple[ui.LayoutView, discord.AllowedMentions]] = []

    async def edit(self, *, view: ui.LayoutView, allowed_mentions: discord.AllowedMentions) -> None:
        self.edits.append((view, allowed_mentions))


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[tuple[str | None, bool, ui.View | None]] = []
        self.modal: ui.Modal | None = None

    async def send_message(
        self,
        content: str | None = None,
        *,
        ephemeral: bool = False,
        view: ui.View | None = None,
    ) -> None:
        self.sent.append((content, ephemeral, view))

    async def send_modal(self, modal: ui.Modal) -> None:
        self.modal = modal


class FakeInteraction:
    def __init__(
        self,
        *,
        services: ConsentPromptServices,
        user_id: int = PRESSER,
        guild_id: int | None = GUILD,
        channel_id: int | None = CHANNEL,
    ) -> None:
        self.user = discord.Object(id=user_id)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.client = SimpleNamespace(consent_prompt_services=services)
        self.response = FakeResponse()


def as_interaction(fake: FakeInteraction) -> discord.Interaction:
    return cast(discord.Interaction, fake)


async def no_role(_guild_id: int) -> None:
    return None


def make_services(
    *,
    consents: FakeConsents | None = None,
    links: FakeLinks | None = None,
    states: FakeStates | None = None,
    prompts: PromptRecord | None = None,
) -> ConsentPromptServices:
    return ConsentPromptServices(
        consents=consents or FakeConsents(),
        links=links or FakeLinks(),
        link_states=states or FakeStates(),
        oauth=FakeOAuth(),
        clock=FakeClock(),
        policy_version=POLICY,
        policy_url=POLICY_URL,
        transcription_host=HOST,
        consent_role=no_role,
        prompts=prompts or PromptRecord(),
    )


def active_consent() -> ConsentRecord:
    return ConsentRecord(granted_at=T0, revoked_at=None, policy_version=POLICY)


def text_of(view: ui.LayoutView) -> str:
    return " ".join(
        item.content for item in view.walk_children() if isinstance(item, ui.TextDisplay)
    )


def custom_ids_of(view: ui.LayoutView) -> list[str]:
    ids: list[str] = []
    for item in view.walk_children():
        if isinstance(item, ui.DynamicItem):
            ids.append(str(item.item.custom_id))
    return ids


def prompt(
    needs: PromptNeeds, member_id: int = JOINER
) -> tuple[ui.LayoutView, discord.AllowedMentions]:
    return build_prompt(discord.Object(id=member_id), needs, transcription_host=HOST)


# 3.1 The message: Components V2, mentions only the joining member


def test_the_prompt_is_a_components_v2_layout_view() -> None:
    view, _ = prompt(PromptNeeds(consent=True, link=True))

    assert isinstance(view, ui.LayoutView)


async def test_the_prompt_text_mentions_the_joining_member() -> None:
    view, _ = prompt(PromptNeeds(consent=True, link=True))

    assert f"<@{JOINER}>" in text_of(view)


async def test_the_prompt_text_mentions_no_one_but_the_joining_member() -> None:
    view, _ = prompt(PromptNeeds(consent=True, link=True))

    assert text_of(view).count("<@") == 1


async def test_the_prompt_allows_a_ping_for_the_joining_member_only() -> None:
    _, mentions = prompt(PromptNeeds(consent=True, link=True))

    users = mentions.users
    assert isinstance(users, list)
    assert [user.id for user in users] == [JOINER]
    assert mentions.everyone is False
    assert mentions.roles is False


async def test_the_prompt_names_the_transcription_host() -> None:
    view, _ = prompt(PromptNeeds(consent=True, link=True))

    assert HOST in text_of(view)


async def test_the_prompt_never_expires_so_its_buttons_keep_working() -> None:
    view, _ = prompt(PromptNeeds(consent=True, link=True))

    assert view.timeout is None


# 3.2 Buttons follow the missing steps


async def test_a_member_missing_only_consent_gets_the_consent_button_alone() -> None:
    view, _ = prompt(PromptNeeds(consent=True, link=False))

    assert custom_ids_of(view) == [CONSENT_BUTTON_ID]


async def test_a_member_missing_only_the_link_gets_the_link_button_alone() -> None:
    view, _ = prompt(PromptNeeds(consent=False, link=True))

    assert custom_ids_of(view) == [LINK_BUTTON_ID]


async def test_a_member_missing_both_steps_gets_both_buttons() -> None:
    view, _ = prompt(PromptNeeds(consent=True, link=True))

    assert custom_ids_of(view) == [CONSENT_BUTTON_ID, LINK_BUTTON_ID]


async def test_a_prompt_with_no_missing_step_is_refused() -> None:
    """A member with consent and a link is never pinged, so no prompt may exist."""
    with pytest.raises(ValueError):
        prompt(PromptNeeds(consent=False, link=False))


# 3.3 Link button: ephemeral, personal, never in the channel


def test_the_link_button_has_the_fixed_custom_id() -> None:
    assert ConsentPromptLinkButton().item.custom_id == "sturnus:consent-prompt:link"


def test_the_link_button_custom_id_matches_its_template() -> None:
    assert ConsentPromptLinkButton().template.match(LINK_BUTTON_ID) is not None


async def test_pressing_link_replies_ephemerally() -> None:
    services = make_services()
    interaction = FakeInteraction(services=services)

    await ConsentPromptLinkButton().callback(as_interaction(interaction))

    assert [ephemeral for _, ephemeral, _ in interaction.response.sent] == [True]


async def test_the_link_reply_carries_a_link_button_to_the_authorization_url() -> None:
    services = make_services()
    interaction = FakeInteraction(services=services)

    await ConsentPromptLinkButton().callback(as_interaction(interaction))

    _, _, view = interaction.response.sent[0]
    assert view is not None
    buttons = [child for child in view.children if isinstance(child, ui.Button)]
    assert [button.style for button in buttons] == [discord.ButtonStyle.link]
    assert str(buttons[0].url).startswith(f"{AUTHORIZE}?state=state-for-{PRESSER}")


async def test_the_state_is_issued_for_the_presser_and_not_the_mentioned_member() -> None:
    states = FakeStates()
    services = make_services(states=states)
    interaction = FakeInteraction(services=services, user_id=PRESSER)

    await ConsentPromptLinkButton().callback(as_interaction(interaction))

    assert states.issued == [(PRESSER, "outline")]


async def test_pressing_link_for_an_already_linked_presser_offers_no_link() -> None:
    services = make_services(links=FakeLinks(identity=("ext-1", "Anna")))
    interaction = FakeInteraction(services=services)

    await ConsentPromptLinkButton().callback(as_interaction(interaction))

    content, ephemeral, view = interaction.response.sent[0]
    assert ephemeral is True
    assert view is None
    assert content is not None
    assert "Anna" in content


async def test_the_link_is_never_part_of_the_channel_message() -> None:
    view, _ = prompt(PromptNeeds(consent=True, link=True))

    assert "://" not in text_of(view)
    assert "http" not in text_of(view)


# 3.4 Consent button: modal first, none for someone already consenting


def test_the_consent_button_has_the_fixed_custom_id() -> None:
    assert ConsentPromptConsentButton().item.custom_id == "sturnus:consent-prompt:consent"


def test_the_consent_button_custom_id_matches_its_template() -> None:
    assert ConsentPromptConsentButton().template.match(CONSENT_BUTTON_ID) is not None


async def test_pressing_consent_opens_a_modal_as_the_first_response() -> None:
    interaction = FakeInteraction(services=make_services())

    await ConsentPromptConsentButton().callback(as_interaction(interaction))

    assert isinstance(interaction.response.modal, ConsentPromptModal)
    assert interaction.response.sent == []


async def test_the_modal_text_names_the_host_and_the_policy_version() -> None:
    interaction = FakeInteraction(services=make_services())

    await ConsentPromptConsentButton().callback(as_interaction(interaction))

    modal = interaction.response.modal
    assert isinstance(modal, ConsentPromptModal)
    texts = [item.content for item in modal.walk_children() if isinstance(item, ui.TextDisplay)]
    assert len(texts) == 1
    assert HOST in texts[0]
    assert POLICY in texts[0]


async def test_the_modal_asks_for_the_agreement_through_an_unticked_checkbox_in_a_label() -> None:
    interaction = FakeInteraction(services=make_services())

    await ConsentPromptConsentButton().callback(as_interaction(interaction))

    modal = interaction.response.modal
    assert isinstance(modal, ConsentPromptModal)
    labels = [child for child in modal.children if isinstance(child, ui.Label)]
    assert [label.text for label in labels] == [
        "I agree to my voice being recorded and transcribed"
    ]
    assert isinstance(labels[0].component, ui.Checkbox)
    assert labels[0].component.value is False


async def test_pressing_consent_while_consent_is_active_opens_no_modal() -> None:
    services = make_services(consents=FakeConsents(record=active_consent()))
    interaction = FakeInteraction(services=services)

    await ConsentPromptConsentButton().callback(as_interaction(interaction))

    assert interaction.response.modal is None
    content, ephemeral, _ = interaction.response.sent[0]
    assert ephemeral is True
    assert content == CONSENT_ALREADY


# 3.5 Modal submission: ticked grants, unticked writes nothing


@dataclass
class Replies:
    texts: list[str] = field(default_factory=list)

    async def send(self, text: str) -> None:
        self.texts.append(text)


async def test_a_ticked_checkbox_assigns_the_role_and_records_the_grant_from_the_prompt() -> None:
    consents = FakeConsents()
    services = make_services(consents=consents)
    replies = Replies()
    assigned: list[int] = []

    async def assign_role() -> None:
        assigned.append(PRESSER)

    outcome = await submit_consent(
        services,
        send=replies.send,
        user_id=PRESSER,
        guild_id=GUILD,
        channel_id=CHANNEL,
        ticked=True,
        role_mention=ROLE_MENTION,
        assign_role=assign_role,
    )

    assert outcome is GrantOutcome.GRANTED
    assert assigned == [PRESSER]
    assert consents.grants == [(PRESSER, GUILD, POLICY, "prompt")]
    assert replies.texts == [CONSENT_STRINGS["granted"].format(role=ROLE_MENTION)]


async def test_an_unticked_checkbox_records_nothing_and_assigns_no_role() -> None:
    consents = FakeConsents()
    services = make_services(consents=consents)
    replies = Replies()
    assigned: list[int] = []

    async def assign_role() -> None:
        assigned.append(PRESSER)

    outcome = await submit_consent(
        services,
        send=replies.send,
        user_id=PRESSER,
        guild_id=GUILD,
        channel_id=CHANNEL,
        ticked=False,
        role_mention=ROLE_MENTION,
        assign_role=assign_role,
    )

    assert outcome is GrantOutcome.NOT_CONFIRMED
    assert assigned == []
    assert consents.grants == []
    assert replies.texts == [NOT_RECORDED]


async def test_a_failed_role_assignment_records_no_grant_and_reports_the_reason() -> None:
    from sturnus.application.consent_flow import RoleAssignmentError

    consents = FakeConsents()
    services = make_services(consents=consents)
    replies = Replies()

    async def assign_role() -> None:
        raise RoleAssignmentError("missing permissions")

    outcome = await submit_consent(
        services,
        send=replies.send,
        user_id=PRESSER,
        guild_id=GUILD,
        channel_id=CHANNEL,
        ticked=True,
        role_mention=ROLE_MENTION,
        assign_role=assign_role,
    )

    assert outcome is GrantOutcome.ROLE_FAILED
    assert consents.grants == []
    assert replies.texts == [
        CONSENT_STRINGS["role_assign_failed"].format(
            role=ROLE_MENTION, reason="missing permissions"
        )
    ]


# 3.6 After consent by the mentioned member, the prompt says so


def _mentioned_prompt(
    *, linked: bool, message: FakeMessage | None = None
) -> tuple[ConsentPromptServices, PromptRecord, FakeMessage]:
    prompts = PromptRecord()
    sent = message or FakeMessage()
    prompts.mark(CHANNEL, JOINER, sent)
    links = FakeLinks(identity=("ext-1", "Anna")) if linked else FakeLinks()
    return make_services(links=links, prompts=prompts), prompts, sent


async def _consent_as(services: ConsentPromptServices, user_id: int) -> None:
    async def assign_role() -> None:
        return None

    async def discard(_text: str) -> None:
        return None

    await submit_consent(
        services,
        send=discard,
        user_id=user_id,
        guild_id=GUILD,
        channel_id=CHANNEL,
        ticked=True,
        role_mention=ROLE_MENTION,
        assign_role=assign_role,
    )


async def test_consent_through_the_prompt_edits_the_message_to_consented() -> None:
    services, _, message = _mentioned_prompt(linked=True)

    await _consent_as(services, JOINER)

    assert len(message.edits) == 1
    view, _ = message.edits[0]
    assert text_of(view) == f"<@{JOINER}> consented"


async def test_the_consented_edit_pings_no_one() -> None:
    services, _, message = _mentioned_prompt(linked=True)

    await _consent_as(services, JOINER)

    _, mentions = message.edits[0]
    assert mentions.users is False
    assert mentions.everyone is False
    assert mentions.roles is False


async def test_the_consented_edit_keeps_the_link_button_while_unlinked() -> None:
    services, _, message = _mentioned_prompt(linked=False)

    await _consent_as(services, JOINER)

    view, _ = message.edits[0]
    assert custom_ids_of(view) == [LINK_BUTTON_ID]


async def test_the_consented_edit_drops_all_buttons_once_linked() -> None:
    services, _, message = _mentioned_prompt(linked=True)

    await _consent_as(services, JOINER)

    view, _ = message.edits[0]
    assert custom_ids_of(view) == []


async def test_another_member_consenting_leaves_the_prompt_untouched() -> None:
    services, _, message = _mentioned_prompt(linked=False)

    await _consent_as(services, PRESSER)

    assert message.edits == []


async def test_the_edit_is_skipped_without_error_when_the_message_reference_is_gone() -> None:
    prompts = PromptRecord()
    prompts.mark(CHANNEL, JOINER, None)
    services = make_services(prompts=prompts)

    await _consent_as(services, JOINER)


async def test_the_edit_is_skipped_without_error_after_a_restart_lost_the_record() -> None:
    services = make_services(prompts=PromptRecord())

    await _consent_as(services, JOINER)


# Dependencies and registration


async def test_the_services_are_read_from_the_client_the_buttons_are_attached_to() -> None:
    """A press without the services attribute is a wiring error, not a silent no-op."""
    interaction = FakeInteraction(services=make_services())
    interaction.client = SimpleNamespace()

    with pytest.raises(AttributeError):
        await ConsentPromptLinkButton().callback(as_interaction(interaction))


def test_the_prompt_dynamic_items_are_both_exported_for_registration() -> None:
    assert set(PROMPT_DYNAMIC_ITEMS) == {ConsentPromptConsentButton, ConsentPromptLinkButton}


async def test_a_modal_submission_outside_a_server_is_refused_without_a_grant() -> None:
    """`interaction.user` is a `User` in a DM; there is no role to assign there."""
    consents = FakeConsents()
    services = make_services(consents=consents)
    modal = ConsentPromptModal(services=services, guild_id=GUILD, channel_id=CHANNEL)
    interaction = FakeInteraction(services=services)

    await modal.on_submit(as_interaction(interaction))

    assert interaction.response.sent[0][1] is True
    assert consents.grants == []
