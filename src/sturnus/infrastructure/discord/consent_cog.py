"""Consent slash commands (Spec 3.3, 10).

Recording a voice channel makes consent a legal precondition, not a
feature: every reply from this cog is ephemeral, because an interaction
about someone's consent is nobody else's business. The branching that
decides whether granting or revoking would actually change anything lives
in `sturnus.application.consent_flow`, tested without a gateway connection;
this module only turns interactions into those calls and renders the
result.

Discord does not allow a command group to be invoked directly once it has
subcommands, so the granting flow lives at `/consent grant` rather than a
bare `/consent`.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from sturnus.application.consent_flow import ConsentStatus, grant_needed, revoke_needed
from sturnus.application.ports import Clock
from sturnus.domain import settings
from sturnus.domain.consent import ConsentRecord, is_consent_active
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.repositories import ConsentRepository
from sturnus.infrastructure.discord.views import CONSENT_STRINGS, ConsentView


@app_commands.guild_only()
class ConsentCog(commands.GroupCog, name="consent", description="Manage recording consent."):
    """Admin-free `/consent` command group; every reply is ephemeral."""

    def __init__(
        self,
        consent_repo: ConsentRepository,
        config_store: ConfigStore,
        clock: Clock,
    ) -> None:
        self._consent_repo = consent_repo
        self._config_store = config_store
        self._clock = clock
        super().__init__()

    async def _policy(self, guild_id: int) -> tuple[str | None, str | None]:
        version = await self._config_store.get(guild_id, settings.POLICY_VERSION)
        url = await self._config_store.get(guild_id, settings.POLICY_URL)
        return version, url

    async def _role(self, guild: discord.Guild, guild_id: int) -> discord.Role | None:
        role_id = await self._config_store.get(guild_id, settings.CONSENT_ROLE_ID)
        if role_id is None:
            return None
        return guild.get_role(int(role_id))

    @staticmethod
    def _has_role(member: discord.Member | discord.User, role: discord.Role | None) -> bool:
        return role is not None and isinstance(member, discord.Member) and role in member.roles

    @app_commands.command(name="grant", description="Review and grant recording consent.")
    async def grant(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None or interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        policy_version, policy_url = await self._policy(guild_id)
        role = await self._role(interaction.guild, guild_id)
        if policy_version is None or policy_url is None or role is None:
            await interaction.response.send_message(
                "Consent is not fully configured for this server yet. "
                "Ask an administrator to run `/config show`.",
                ephemeral=True,
            )
            return

        member = interaction.user
        record = await self._consent_repo.current(member.id, guild_id)
        has_role = self._has_role(member, role)
        if not grant_needed(record, policy_version, has_role, self._clock.now()):
            await interaction.response.send_message(
                "You have already consented under the current policy.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=CONSENT_STRINGS["title"],
            description=CONSENT_STRINGS["description"].format(policy_url=policy_url),
        )
        view = ConsentView(
            author_id=member.id,
            guild_id=guild_id,
            role=role,
            policy_version=policy_version,
            consent_repo=self._consent_repo,
            clock=self._clock,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @app_commands.command(name="revoke", description="Withdraw recording consent.")
    async def revoke(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None or interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        policy_version, _ = await self._policy(guild_id)
        role = await self._role(interaction.guild, guild_id)
        member = interaction.user
        record = await self._consent_repo.current(member.id, guild_id)
        has_role = self._has_role(member, role)

        if policy_version is None or not revoke_needed(
            record, policy_version, has_role, self._clock.now()
        ):
            await interaction.response.send_message(
                "There is no consent or role to withdraw.", ephemeral=True
            )
            return

        role_error: str | None = None
        if has_role and isinstance(member, discord.Member) and role is not None:
            try:
                await member.remove_roles(role, reason="Recording consent withdrawn")
            except discord.HTTPException as exc:
                # A withdrawal that silently keeps the role would let someone
                # believe they can no longer speak when in fact they still
                # can, so this failure is reported, not swallowed.
                role_error = str(exc)

        await self._consent_repo.record_revocation(member.id, guild_id, self._clock.now())

        if role_error is not None and role is not None:
            await interaction.response.send_message(
                CONSENT_STRINGS["role_remove_failed"].format(role=role.mention, reason=role_error),
                ephemeral=True,
            )
            return

        suffix = f" and the {role.mention} role removed." if has_role and role else "."
        await interaction.response.send_message(f"Consent withdrawn{suffix}", ephemeral=True)

    @app_commands.command(name="status", description="Show your current consent status.")
    async def status(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None or interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        policy_version, _ = await self._policy(guild_id)
        role = await self._role(interaction.guild, guild_id)
        member = interaction.user
        record: ConsentRecord | None = await self._consent_repo.current(member.id, guild_id)
        status = ConsentStatus(
            has_role=self._has_role(member, role),
            consent_active=(
                policy_version is not None
                and is_consent_active(record, policy_version, self._clock.now())
            ),
            policy_version=record.policy_version if record is not None else None,
            # Account linking is handled by a later task; not reported yet.
            linked=False,
        )
        lines = [
            f"Role assigned: {'yes' if status.has_role else 'no'}",
            f"Consent active: {'yes' if status.consent_active else 'no'}",
            f"Policy version consented to: {status.policy_version or '*(none)*'}",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
