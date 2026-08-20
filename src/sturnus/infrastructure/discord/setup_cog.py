"""Guided setup for the five required configuration keys (Spec 10.1).

Setting each key one at a time through `/config set` means typing a channel
id and a role id by hand -- which needs Discord's developer mode turned on
and the id copied out of a context menu. `/setup` takes a channel and a
role as typed command parameters instead, so Discord renders native
pickers for both.

It also configures the voice channel's permissions itself: Speak denied
for `@everyone`, allowed for the consent role. That is deliberate, not
incidental -- those two permission overwrites are the primary layer of the
consent protection (Spec 3.1), and leaving that step to prose in an
operations guide would put the one step nobody may get wrong into the
hands of whoever reads the guide least carefully.

All the decisions -- what to write, what permissions to change, whether a
role needs creating, what is still missing -- are computed by
`sturnus.application.setup_plan.plan_setup`, a pure function tested without
a guild. This module only reads Discord state into that function's
parameters and turns its result into Discord API calls, reporting exactly
what succeeded and what did not.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from sturnus.application.ports import Clock
from sturnus.application.setup_plan import PermissionChange, SetupPlan, plan_setup
from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.discord.permissions import require_admin

#: Reason recorded on the audit log for every permission change setup makes.
_AUDIT_REASON = "Sturnus /setup: consent protection (Spec 3.1)"


class SetupCog(commands.Cog):
    """Admin-only `/setup` command; every reply is ephemeral."""

    def __init__(self, store: ConfigStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        super().__init__()

    @app_commands.command(
        name="setup",
        description="Guided setup: configures the voice channel, consent role and policy.",
    )
    @app_commands.describe(
        channel="Voice channel Sturnus should record",
        policy_url="URL of the recording/consent policy shown to participants",
        policy_version="Version identifier of the policy currently in force",
        consent_role="Role that grants recording consent; a new role is created if omitted",
    )
    @app_commands.guild_only()
    @require_admin()
    async def setup(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        policy_url: str,
        policy_version: str,
        consent_role: discord.Role | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        # Creating a role and editing channel overwrites are real Discord
        # API round-trips, on top of the database writes -- comfortably
        # over the 3 second window for the initial response.
        await interaction.response.defer(ephemeral=True, thinking=True)

        current = {
            key: await self._store.get_stored(guild.id, key) for key in settings.REQUIRED_KEYS
        }

        everyone_overwrite = channel.overwrites_for(guild.default_role)
        everyone_may_speak = everyone_overwrite.speak is not False
        if consent_role is not None:
            role_may_speak = channel.overwrites_for(consent_role).speak is True
        else:
            role_may_speak = False

        plan = plan_setup(
            current=current,
            channel_id=channel.id,
            role_id=consent_role.id if consent_role is not None else None,
            policy_url=policy_url,
            policy_version=policy_version,
            everyone_may_speak=everyone_may_speak,
            role_may_speak=role_may_speak,
        )

        role = consent_role
        role_error: str | None = None
        if plan.role_to_create is not None:
            try:
                role = await guild.create_role(name=plan.role_to_create, reason=_AUDIT_REASON)
            except discord.Forbidden:
                role_error = (
                    "I am missing the Manage Roles permission, so I could not create "
                    f"the `{plan.role_to_create}` role. Create it yourself and run "
                    f"`/config set {settings.CONSENT_ROLE_ID} <role id>`."
                )
            except discord.HTTPException as exc:
                role_error = f"Could not create the `{plan.role_to_create}` role: {exc}"

        # The database writes happen unconditionally, whether or not the
        # permission changes below succeed: the two are independent, and
        # refusing to store what we *did* manage to determine because a
        # permission edit failed would leave the guild worse off than
        # before this command ran.
        writes = dict(plan.writes)
        if role is not None and role is not consent_role:
            # A role was just created; its id is only known now, so
            # plan_setup could not have included it in `writes`.
            writes[settings.CONSENT_ROLE_ID] = str(role.id)

        now = self._clock.now()
        for key, value in writes.items():
            await self._store.set(guild.id, key, value, now)

        applied, permission_errors = await self._apply_permission_changes(
            channel, guild.default_role, role, plan.permission_changes
        )

        missing = list(plan.missing)
        if role_error is not None and settings.CONSENT_ROLE_ID not in missing:
            missing.append(settings.CONSENT_ROLE_ID)

        await interaction.followup.send(
            _render_summary(writes, applied, permission_errors, role_error, missing),
            ephemeral=True,
        )

    async def _apply_permission_changes(
        self,
        channel: discord.VoiceChannel,
        everyone: discord.Role,
        consent_role: discord.Role | None,
        changes: list[PermissionChange],
    ) -> tuple[list[str], list[str]]:
        """Applies each overwrite independently; one failure never blocks the other.

        Returns the human-readable description of every change that
        succeeded, and of every one that did not -- a permission failure is
        reported, never swallowed, because a half-applied setup that claims
        success is worse than one that admits it stopped.
        """
        applied: list[str] = []
        errors: list[str] = []
        for change in changes:
            verb = "allow" if change.allow_speak else "deny"
            target: discord.Role | None
            label: str
            if change.target == "everyone":
                target, label = everyone, "@everyone"
            else:
                target, label = consent_role, "the consent role"
                if target is None:
                    errors.append(
                        f"Could not {verb} Speak for the consent role: no consent role "
                        "exists yet. Create one and run /setup again, or set it manually "
                        f"and run `/config set {settings.CONSENT_ROLE_ID} <role id>`."
                    )
                    continue

            overwrite = channel.overwrites_for(target)
            # `.update()` rather than `overwrite.speak = ...`: PermissionOverwrite
            # defines its permission attributes only under `TYPE_CHECKING`, which
            # mypy strict does not accept as a real `__slots__` member for plain
            # attribute assignment.
            overwrite.update(speak=change.allow_speak)
            try:
                await channel.set_permissions(target, overwrite=overwrite, reason=_AUDIT_REASON)
            except discord.Forbidden:
                errors.append(
                    f"Could not {verb} Speak for {label}: I am missing the Manage "
                    "Permissions permission on this channel. Set it manually."
                )
            except discord.HTTPException as exc:
                errors.append(f"Could not {verb} Speak for {label}: {exc}")
            else:
                applied.append(f"{verb.capitalize()}ed Speak for {label}.")
        return applied, errors


def _render_summary(
    writes: dict[str, str],
    applied_permissions: list[str],
    permission_errors: list[str],
    role_error: str | None,
    missing: list[str],
) -> str:
    """Builds the ephemeral reply: what changed, what did not, what is still missing.

    Names keys, never values: today's five required keys hold nothing
    sensitive, but this summary is written so that stays true even if a
    future key does -- it reports *that* `policy_url` was written, not what
    it was written to.
    """
    lines: list[str] = ["**Setup result**"]

    if writes:
        lines.append("Configuration written: " + ", ".join(f"`{key}`" for key in sorted(writes)))
    else:
        lines.append("Configuration: nothing needed writing, everything was already correct.")

    lines.extend(applied_permissions)
    lines.extend(f"⚠️ {error}" for error in permission_errors)
    if role_error is not None:
        lines.append(f"⚠️ {role_error}")

    if missing:
        lines.append("**Still missing:** " + ", ".join(f"`{key}`" for key in sorted(missing)))
    else:
        lines.append("All required configuration is now set.")

    return "\n".join(lines)


__all__ = ["SetupCog", "SetupPlan"]
