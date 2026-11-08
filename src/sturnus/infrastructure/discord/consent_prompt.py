"""Presentation of the consent prompt posted in a recording channel.

`sturnus.application.consent_prompt` decides which steps a joining member is
still missing, and `sturnus.application.consent_flow` and
`sturnus.application.linking` decide what a grant or a link start does. This
module renders those decisions as a Components V2 message and handles the two
buttons on it. It does not decide anything the application layer decides.

The buttons are `ui.DynamicItem`s with fixed custom ids. Discord sends a press
without any object of ours attached, so everything a press needs comes from
`ConsentPromptServices`, read from `interaction.client`. The part that wires
the services onto the client and registers `PROMPT_DYNAMIC_ITEMS` lives in the
bot's startup code, not here.

Every button acts for `interaction.user`, never for the member mentioned in the
message. The channel message holds no URL: the link is sent only to the
presser, as an ephemeral reply.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

import discord
from discord import ui

from sturnus.application.consent_flow import (
    ConsentWriter,
    GrantOutcome,
    RoleAssignmentError,
    grant_consent,
)
from sturnus.application.consent_prompt import PromptNeeds, PromptRecord
from sturnus.application.linking import LinkedIdentities, StateIssuer, begin_link
from sturnus.application.ports import Clock
from sturnus.domain.consent import ConsentRecord, is_consent_active
from sturnus.infrastructure.discord.link_cog import PROVIDER, STATE_TTL
from sturnus.infrastructure.discord.views import CONSENT_STRINGS

log = logging.getLogger(__name__)

CONSENT_BUTTON_ID = "sturnus:consent-prompt:consent"
LINK_BUTTON_ID = "sturnus:consent-prompt:link"

AGREE_LABEL = "I agree to my voice being recorded and transcribed"

_ALREADY_CONSENTED = "Consent is already recorded for you under the current policy."
_NOT_RECORDED = "No consent was recorded. Tick the box to agree, then submit again."
_NOT_CONFIGURED = (
    "Consent is not fully configured for this server yet. "
    "Ask an administrator to run `/config show`."
)
_SERVER_ONLY = "This prompt can only be used in a server."
_LINK_REPLY = (
    "Follow this link to authorize Sturnus against your Outline account. "
    "Only you can see this message."
)
_ALREADY_LINKED = "Your Discord account is already linked, as **{name}**."
_CONSENTED = "<@{user_id}> consented"

_PROMPT_BOTH = (
    "<@{user_id}>, this voice channel is recorded, and speech is transcribed at {host}. "
    "Press **Consent** to agree to being recorded. Linking an Outline account is optional."
)
_PROMPT_CONSENT_ONLY = (
    "<@{user_id}>, this voice channel is recorded, and speech is transcribed at {host}. "
    "Press **Consent** to agree. Only members who have consented are recorded."
)
_PROMPT_LINK_ONLY = (
    "<@{user_id}>, your consent is recorded. Linking an Outline account is optional; "
    "press **Link account** to start it."
)

_SERVICES_ATTRIBUTE = "consent_prompt_services"


class ConsentStore(ConsentWriter, Protocol):
    """What the consent buttons read and write: `ConsentRepository` in production."""

    async def current(self, discord_user_id: int, guild_id: int) -> ConsentRecord | None: ...


class AuthorizationUrls(Protocol):
    """What the link button builds its URL with: `OutlineOAuth` in production."""

    def authorize_url(self, state: str) -> str: ...


class PromptMessage(Protocol):
    """The stored prompt: a `discord.Message`, edited once the member consents."""

    async def edit(
        self, *, view: ui.LayoutView, allowed_mentions: discord.AllowedMentions
    ) -> object: ...


@dataclass(frozen=True)
class ConsentPromptServices:
    """Everything the prompt's buttons need, attached once to the client.

    `consent_role` takes a guild id and returns the role, or `None` when the
    guild has none configured. `policy_version` and `policy_url` are the ones
    the `/consent` command shows. `prompts` is the in-memory record of who
    was already asked, shared with the join hook.
    """

    consents: ConsentStore
    links: LinkedIdentities
    link_states: StateIssuer
    oauth: AuthorizationUrls
    clock: Clock
    policy_version: str
    policy_url: str
    transcription_host: str
    consent_role: Callable[[int], Awaitable[discord.Role | None]]
    prompts: PromptRecord


def services_of(interaction: discord.Interaction) -> ConsentPromptServices:
    """Reads the services the bot attached to its client. Raises if they are missing."""
    services: ConsentPromptServices = getattr(interaction.client, _SERVICES_ATTRIBUTE)
    return services


def _prompt_text(user_id: int, needs: PromptNeeds, host: str) -> str:
    if needs.consent and needs.link:
        template = _PROMPT_BOTH
    elif needs.consent:
        template = _PROMPT_CONSENT_ONLY
    else:
        template = _PROMPT_LINK_ONLY
    return template.format(user_id=user_id, host=host)


def _mentions_only(member: discord.abc.Snowflake) -> discord.AllowedMentions:
    return discord.AllowedMentions(users=[member], everyone=False, roles=False)


def build_prompt(
    member: discord.abc.Snowflake,
    needs: PromptNeeds,
    *,
    transcription_host: str,
) -> tuple[ui.LayoutView, discord.AllowedMentions]:
    """The channel message for a joining member who is missing `needs`.

    The view is a Components V2 layout, so the message carries no `content`:
    the mention lives in the Text Display, and `allowed_mentions` lets that one
    user be pinged. Raises `ValueError` when nothing is missing, because a
    member with consent and a link is never pinged.
    """
    if not needs.any:
        raise ValueError("a consent prompt needs at least one missing step")
    buttons: list[ui.Item[Any]] = []
    if needs.consent:
        buttons.append(ConsentPromptConsentButton())
    if needs.link:
        buttons.append(ConsentPromptLinkButton())
    view = ui.LayoutView(timeout=None)
    text = _prompt_text(member.id, needs, transcription_host)
    view.add_item(ui.Container(ui.TextDisplay(text), ui.ActionRow(*buttons)))
    return view, _mentions_only(member)


def build_consented(
    user_id: int, *, link_needed: bool
) -> tuple[ui.LayoutView, discord.AllowedMentions]:
    """The prompt after its member consented: the same message, marked done.

    Keeps the link button only while the member is unlinked. The edit uses
    `AllowedMentions.none()`, so the mention in the text does not ping again.
    """
    container_children: list[ui.Item[Any]] = [ui.TextDisplay(_CONSENTED.format(user_id=user_id))]
    if link_needed:
        container_children.append(ui.ActionRow(ConsentPromptLinkButton()))
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*container_children))
    return view, discord.AllowedMentions.none()


class ConsentPromptLinkButton(
    ui.DynamicItem[ui.Button[Any]], template=r"^sturnus:consent-prompt:link$"
):
    """Starts account linking for the presser, with the reply visible only to them."""

    def __init__(self) -> None:
        super().__init__(
            ui.Button(
                label="Link account",
                style=discord.ButtonStyle.secondary,
                custom_id=LINK_BUTTON_ID,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        _interaction: discord.Interaction,
        _item: ui.Item[Any],
        _match: re.Match[str],
        /,
    ) -> ConsentPromptLinkButton:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        services = services_of(interaction)
        started = await begin_link(
            interaction.user.id,
            provider=PROVIDER,
            identities=services.links,
            states=services.link_states,
            now=services.clock.now(),
            ttl=STATE_TTL,
        )
        if started.state is None:
            log.info(
                "consent prompt: %s guild=%s user=%s",
                "already_linked",
                interaction.guild_id,
                interaction.user.id,
            )
            await interaction.response.send_message(
                _ALREADY_LINKED.format(name=started.linked_as or ""), ephemeral=True
            )
            return

        log.info(
            "consent prompt: %s guild=%s user=%s",
            "link_started",
            interaction.guild_id,
            interaction.user.id,
        )
        view = ui.View(timeout=None)
        view.add_item(
            ui.Button(
                label="Open account linking",
                style=discord.ButtonStyle.link,
                url=services.oauth.authorize_url(started.state),
            )
        )
        await interaction.response.send_message(_LINK_REPLY, ephemeral=True, view=view)


class ConsentPromptConsentButton(
    ui.DynamicItem[ui.Button[Any]], template=r"^sturnus:consent-prompt:consent$"
):
    """Opens the consent modal for the presser, unless they already consent."""

    def __init__(self) -> None:
        super().__init__(
            ui.Button(
                label="Consent",
                style=discord.ButtonStyle.success,
                custom_id=CONSENT_BUTTON_ID,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        _interaction: discord.Interaction,
        _item: ui.Item[Any],
        _match: re.Match[str],
        /,
    ) -> ConsentPromptConsentButton:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        services = services_of(interaction)
        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        if guild_id is None or channel_id is None:
            await interaction.response.send_message(_SERVER_ONLY, ephemeral=True)
            return

        user_id = interaction.user.id
        record = await services.consents.current(user_id, guild_id)
        if is_consent_active(record, services.policy_version, services.clock.now()):
            log.info("consent prompt: %s guild=%s user=%s", "already_consented", guild_id, user_id)
            await interaction.response.send_message(_ALREADY_CONSENTED, ephemeral=True)
            return

        # The modal is the first response: Discord allows three seconds for it.
        await interaction.response.send_modal(
            ConsentPromptModal(services=services, guild_id=guild_id, channel_id=channel_id)
        )


def _modal_text(services: ConsentPromptServices) -> str:
    explanation = CONSENT_STRINGS["description"].format(
        policy_url=services.policy_url, transcription_host=services.transcription_host
    )
    return f"{explanation}\n\nPolicy version: {services.policy_version}"


class ConsentPromptModal(ui.Modal):
    """The consent dialog: an explanation and a checkbox that must be ticked."""

    def __init__(
        self,
        *,
        services: ConsentPromptServices,
        guild_id: int,
        channel_id: int,
    ) -> None:
        super().__init__(title=CONSENT_STRINGS["title"], timeout=300)
        self._services = services
        self._guild_id = guild_id
        self._channel_id = channel_id
        self._agree: ui.Checkbox[Any] = ui.Checkbox()
        self.add_item(ui.TextDisplay(_modal_text(services)))
        self.add_item(ui.Label(text=AGREE_LABEL, component=self._agree))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(_SERVER_ONLY, ephemeral=True)
            return
        role = await self._services.consent_role(self._guild_id)
        if role is None:
            await interaction.response.send_message(_NOT_CONFIGURED, ephemeral=True)
            return

        async def assign_role() -> None:
            try:
                await member.add_roles(role, reason="Recording consent granted")
            except discord.HTTPException as exc:
                raise RoleAssignmentError(str(exc)) from exc

        async def send(text: str) -> None:
            await interaction.response.send_message(text, ephemeral=True)

        await submit_consent(
            self._services,
            send=send,
            user_id=member.id,
            guild_id=self._guild_id,
            channel_id=self._channel_id,
            ticked=self._agree.value,
            role_mention=role.mention,
            assign_role=assign_role,
        )


async def submit_consent(
    services: ConsentPromptServices,
    *,
    send: Callable[[str], Awaitable[object]],
    user_id: int,
    guild_id: int,
    channel_id: int,
    ticked: bool,
    role_mention: str,
    assign_role: Callable[[], Awaitable[None]],
) -> GrantOutcome:
    """Records consent from the prompt, then marks the prompt if the member is its subject.

    The grant itself is `grant_consent`, the same use case `/consent grant`
    runs, so the role-first order and the checkbox guard are shared. The
    prompt is edited only when the presser has an entry in the record for
    this channel: that is what "mentioned member" means here. After a restart
    the record is empty, so the edit is skipped and the stale message keeps its
    buttons.
    """
    result = await grant_consent(
        discord_user_id=user_id,
        guild_id=guild_id,
        policy_version=services.policy_version,
        source="prompt",
        now=services.clock.now(),
        checkbox_ticked=ticked,
        assign_role=assign_role,
        consents=services.consents,
    )
    if result.outcome is GrantOutcome.NOT_CONFIRMED:
        log.info("consent prompt: %s guild=%s user=%s", "not_confirmed", guild_id, user_id)
        await send(_NOT_RECORDED)
        return result.outcome
    if result.outcome is GrantOutcome.ROLE_FAILED:
        log.info("consent prompt: %s guild=%s user=%s", "role_failed", guild_id, user_id)
        await send(
            CONSENT_STRINGS["role_assign_failed"].format(
                role=role_mention, reason=result.reason or ""
            )
        )
        return result.outcome

    log.info("consent prompt: %s guild=%s user=%s", "consented_via_prompt", guild_id, user_id)
    await send(CONSENT_STRINGS["granted"].format(role=role_mention))
    await _mark_prompt_consented(services, channel_id=channel_id, user_id=user_id)
    return result.outcome


async def _mark_prompt_consented(
    services: ConsentPromptServices, *, channel_id: int, user_id: int
) -> None:
    prompts = services.prompts
    if prompts.should_prompt(channel_id, user_id):
        # Not the mentioned member, or the record was lost in a restart.
        return
    ref = prompts.message_ref(channel_id, user_id)
    if ref is None:
        log.info("consent prompt: %s channel=%s user=%s", "edit_skipped", channel_id, user_id)
        return

    linked = await services.links.external_identity(user_id) is not None
    view, mentions = build_consented(user_id, link_needed=not linked)
    message = cast(PromptMessage, ref)
    try:
        await message.edit(view=view, allowed_mentions=mentions)
    except discord.HTTPException:
        log.warning("consent prompt: %s channel=%s user=%s", "edit_failed", channel_id, user_id)


PROMPT_DYNAMIC_ITEMS: tuple[type[ui.DynamicItem[ui.Button[Any]]], ...] = (
    ConsentPromptConsentButton,
    ConsentPromptLinkButton,
)
