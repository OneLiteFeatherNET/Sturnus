"""Interactive components for the consent flow (Spec 3.3, 10).

Kept separate from the cog so the Discord-specific rendering rules that
apply to every reply here — ephemeral responses, the author-only check, the
timeout that disables the buttons — stay in one place. The decision of
*whether* granting or revoking would change anything lives in
`sturnus.application.consent_flow`, not here.

The embed and button copy is a plain module-level constant for now. Spec 8.2
moves this to a Jinja2 template shipped in the image; that arrives with
Plan 3's document adapter.
"""

from __future__ import annotations

import discord

from sturnus.application.ports import Clock
from sturnus.infrastructure.db.repositories import ConsentRepository

CONSENT_STRINGS: dict[str, str] = {
    "title": "Recording consent",
    "description": (
        "While this bot is recording the configured voice channel, your "
        "audio is captured and transcribed separately from everyone "
        "else's speech. Recordings are kept only for the configured "
        "retention period and then deleted; the resulting transcript is "
        "published to the configured document target.\n\n"
        "Full policy: {policy_url}"
    ),
    "granted": "Consent recorded. The {role} role has been assigned.",
    "declined": "No consent recorded; the {role} role was not assigned.",
    "role_assign_failed": (
        "Consent was recorded, but the {role} role could not be assigned "
        "({reason}). Please contact a server administrator — without the "
        "role you will not be treated as consenting."
    ),
    "role_remove_failed": (
        "Consent was withdrawn, but the {role} role could not be removed "
        "({reason}). Please contact a server administrator."
    ),
    "not_author": "Only {author} can respond to this prompt.",
}


class ConsentView(discord.ui.View):
    """Agree / Decline buttons for one person's consent prompt.

    Every response this view sends is ephemeral. `interaction_check`
    rejects presses from anyone but the command's invoker — Discord
    components are clickable by anyone who can see the message. On
    timeout the buttons disable themselves so a stale message cannot
    grant consent days later.
    """

    def __init__(
        self,
        *,
        author_id: int,
        guild_id: int,
        role: discord.Role,
        policy_version: str,
        consent_repo: ConsentRepository,
        clock: Clock,
        timeout: float = 180.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._author_id = author_id
        self._guild_id = guild_id
        self._role = role
        self._policy_version = policy_version
        self._consent_repo = consent_repo
        self._clock = clock
        #: Set by the cog right after sending, so `on_timeout` can disable
        #: the buttons on the message that carries them.
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                CONSENT_STRINGS["not_author"].format(author=f"<@{self._author_id}>"),
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            await self.message.edit(view=self)

    @discord.ui.button(label="Agree", style=discord.ButtonStyle.success)
    async def agree(
        self, interaction: discord.Interaction, _button: discord.ui.Button[ConsentView]
    ) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            self.stop()
            return

        try:
            await member.add_roles(self._role, reason="Recording consent granted")
        except discord.HTTPException as exc:
            # A record without the role would let someone believe they may
            # speak when they cannot, so this failure is reported, not
            # swallowed.
            await interaction.response.send_message(
                CONSENT_STRINGS["role_assign_failed"].format(
                    role=self._role.mention, reason=str(exc)
                ),
                ephemeral=True,
            )
            self.stop()
            return

        await self._consent_repo.record_grant(
            discord_user_id=member.id,
            guild_id=self._guild_id,
            policy_version=self._policy_version,
            source="button",
            now=self._clock.now(),
        )
        await interaction.response.send_message(
            CONSENT_STRINGS["granted"].format(role=self._role.mention), ephemeral=True
        )
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(
        self, interaction: discord.Interaction, _button: discord.ui.Button[ConsentView]
    ) -> None:
        await interaction.response.send_message(
            CONSENT_STRINGS["declined"].format(role=self._role.mention), ephemeral=True
        )
        self.stop()
