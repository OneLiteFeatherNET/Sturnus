"""Discord commands for linking a Discord identity to an Outline account (Spec 8.4).

Sturnus never asks a participant to paste an id or a token. `/link start`
issues a single-use, unguessable state (`sturnus.infrastructure.db.
link_state.LinkStateStore`, backed by `sturnus.application.linking.
new_state`) and hands back the URL that begins Outline's own OAuth flow,
built by `OutlineOAuth.authorize_url`. Completing that flow is entirely the
`link` deployment's job (`sturnus.infrastructure.linkserver`): its callback
consumes the state, exchanges the code, and saves the mapping. This cog
never does either of those things -- it only starts the flow and reports
what is already stored.

Discord does not allow a command group to be invoked directly once it has
subcommands (see `ConsentCog`'s docstring for the same constraint), so the
flow that issues the authorization URL lives at `/link start` rather than
a bare `/link`.

**The authorization URL must never be posted publicly.** It carries the
state that grants linking to the invoking user's Discord identity -- anyone
else who read it could use it to link *their own* Outline account to
*that* Discord identity instead of their own. Every reply from this cog is
ephemeral for exactly that reason, not merely as a courtesy the way some
other commands' ephemeral replies are.

Outline is the only external provider Sturnus links against today --
`PROVIDER` mirrors the literal `sturnus.entrypoints.worker` already renders
documents with (see that module's `links = AccountLinkRepository(...,
"outline")`), not a value read per guild from `document_provider`: there is
exactly one `OutlineOAuth` client wired up in the bot process, not one
selected per guild.
"""

from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from sturnus.application.ports import Clock
from sturnus.infrastructure.db.link_state import LinkStateStore
from sturnus.infrastructure.db.repositories import AccountLinkRepository
from sturnus.infrastructure.documents.outline_oauth import OutlineOAuth

#: The only external account provider linked against today. See the module
#: docstring for why this is a constant here rather than a per-guild
#: configuration read.
PROVIDER = "outline"

#: How long an issued state remains valid before `/link start` must be run
#: again. Long enough to cover the round trip through a browser and
#: Outline's own login/consent screens; short enough that a state nobody
#: ever used does not sit in `oauth_state` indefinitely.
STATE_TTL = timedelta(minutes=15)

#: Shown after a successful `/link remove` (Spec 8.4): unlinking must never
#: read as "and it's as if this never happened" -- a protocol document
#: already published under the display name this link supplied is a
#: separate, already-completed processing result, not something removing
#: the mapping can or should reach back and rewrite.
_PAST_PROTOCOLS_NOTICE = (
    "Protocol documents already published keep the name they were written "
    "with -- that is a separate processing result, already published, and "
    "removing this link does not rewrite it."
)


def _authorize_text(url: str) -> str:
    """The `/link start` reply: the URL, and a warning about what it grants."""
    return (
        "Follow this link to authorize Sturnus against your Outline account:\n"
        f"{url}\n\n"
        "This link is shown only to you. Do not share it -- it grants linking "
        "to your Discord identity, so anyone who has it could link their own "
        "Outline account to yours instead of you linking your own."
    )


def _already_linked_text(display_name: str) -> str:
    """The `/link start` reply when a link already exists."""
    return (
        f"Your Discord account is already linked, as **{display_name}**. "
        "Run `/link remove` first if you want to link a different account."
    )


def _status_text(existing: tuple[str, str] | None) -> str:
    """The `/link status` reply, for either an existing link or none."""
    if existing is None:
        return "Your Discord account is not linked yet. Run `/link start` to begin."
    _, display_name = existing
    return f"Your Discord account is linked, as **{display_name}**."


def _remove_text(removed: bool) -> str:
    """The `/link remove` reply: reports whether anything actually existed to remove."""
    if not removed:
        return "Your Discord account was not linked; there was nothing to remove."
    return f"Your account link has been removed. {_PAST_PROTOCOLS_NOTICE}"


@app_commands.guild_only()
class LinkCog(commands.GroupCog, name="link", description="Link your Discord account to Outline."):
    """`/link` command group; every reply is ephemeral (see module docstring)."""

    def __init__(
        self,
        oauth: OutlineOAuth,
        states: LinkStateStore,
        links: AccountLinkRepository,
        clock: Clock,
    ) -> None:
        self._oauth = oauth
        self._states = states
        self._links = links
        self._clock = clock
        super().__init__()

    @app_commands.command(
        name="start",
        description="Get a one-time link to authorize Sturnus against your Outline account.",
    )
    async def start(self, interaction: discord.Interaction) -> None:
        existing = await self._links.external_identity(interaction.user.id)
        if existing is not None:
            _, display_name = existing
            await interaction.response.send_message(
                _already_linked_text(display_name), ephemeral=True
            )
            return

        state = await self._states.issue(
            interaction.user.id, PROVIDER, self._clock.now(), STATE_TTL
        )
        url = self._oauth.authorize_url(state)
        await interaction.response.send_message(_authorize_text(url), ephemeral=True)

    @app_commands.command(name="status", description="Show whether your account is linked.")
    async def status(self, interaction: discord.Interaction) -> None:
        existing = await self._links.external_identity(interaction.user.id)
        await interaction.response.send_message(_status_text(existing), ephemeral=True)

    @app_commands.command(name="remove", description="Remove your Outline account link.")
    async def remove(self, interaction: discord.Interaction) -> None:
        removed = await self._links.delete(interaction.user.id, PROVIDER)
        await interaction.response.send_message(_remove_text(removed), ephemeral=True)


__all__ = ["PROVIDER", "STATE_TTL", "LinkCog"]
