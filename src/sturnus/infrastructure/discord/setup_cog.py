"""Guided setup for the six required configuration keys (Spec 10.1).

Setting each key one at a time through `/config set` means typing a channel
id and a role id by hand -- which needs Discord's developer mode turned on
and the id copied out of a context menu. `/setup` takes a channel and a
role as typed command parameters instead, so Discord renders native
pickers for both.

One channel at a time, and **added** rather than substituted. A guild
allows a list of recording channels, so running `/setup` for the second
meeting room used to un-configure the first one silently -- a failure
nobody notices until the meeting that should have been recorded was not.
There is deliberately no `/setup remove`: removing a channel is
`/config set voice_channel_ids <the list, minus one>`, and the reply
prints the current list so that edit is a copy and a deletion rather than
a lookup. A second command for one rare edit would be a second place
where the list is written.

It also configures the voice channels' permissions itself: Speak denied
for `@everyone`, allowed for the consent role, on **every** allowed
channel rather than only the one just named. That is deliberate, not
incidental -- those two permission overwrites are the primary layer of the
consent protection (Spec 3.1), and leaving that step to prose in an
operations guide would put the one step nobody may get wrong into the
hands of whoever reads the guide least carefully. A channel Sturnus may
record in whose `@everyone` can still Speak is a hole in that protection
whichever call added it.

All the decisions -- what to write, what permissions to change, whether a
role needs creating, what is still missing -- are computed by
`sturnus.application.setup_plan.plan_setup`, a pure function tested without
a guild. This module only reads Discord state into that function's
parameters and turns its result into Discord API calls, reporting exactly
what succeeded and what did not.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from sturnus.application.ports import Clock
from sturnus.application.reconfigure import Reconfigure, ReconfigureResult
from sturnus.application.setup_plan import (
    ChannelPermissions,
    PermissionChange,
    RoleAction,
    SetupPlan,
    plan_setup,
)
from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.discord.config_cog import render_write_result
from sturnus.infrastructure.discord.permissions import require_admin

log = logging.getLogger(__name__)

#: Reason recorded on the audit log for every permission change setup makes.
_AUDIT_REASON = "Sturnus /setup: consent protection (Spec 3.1)"


class SetupCog(commands.Cog):
    """Admin-only `/setup` command; every reply is ephemeral."""

    def __init__(self, store: ConfigStore, clock: Clock, reconcile: Reconfigure) -> None:
        self._store = store
        self._clock = clock
        #: `/setup` writes the two keys that decide whether a guild can
        #: record at all, so it has exactly the same obligation `/config
        #: set` has: apply them without a restart, and say what took
        #: effect rather than confirming a write and implying the rest.
        self._reconcile = reconcile
        super().__init__()

    @app_commands.command(
        name="setup",
        description="Guided setup: adds a voice channel and configures consent role and policy.",
    )
    @app_commands.describe(
        channel="Voice channel to add to the list Sturnus may record in",
        policy_url="URL of the recording/consent policy shown to participants",
        policy_version="Version identifier of the policy currently in force",
        consent_role=(
            "Role that grants recording consent; if omitted, the already-configured "
            "role is kept, or a new one is created if none exists"
        ),
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

        # The deprecated singular key is read alongside the required ones:
        # a guild that has not been touched since the rename still has its
        # channel there, and `/setup` must add to that list rather than
        # start a new one beside it.
        current = {
            key: await self._store.get_stored(guild.id, key)
            for key in settings.REQUIRED_KEYS | {settings.VOICE_CHANNEL_ID}
        }

        list_error: str | None = None
        try:
            stored_channel_ids = settings.recording_channel_ids(current)
        except settings.InvalidChannelList as exc:
            # Only reachable through a direct `UPDATE` -- `ConfigStore.set`
            # refuses an unparseable list at the write. Reported rather
            # than swallowed, because the list this call goes on to write
            # will not contain whatever the unreadable one meant.
            list_error = (
                f"The stored `{settings.VOICE_CHANNEL_IDS}` could not be read ({exc}), so "
                "it is being replaced by the channels named below rather than added to."
            )
            stored_channel_ids = ()

        # Omitting `consent_role` must never be the destructive path (Spec
        # 10.1): the most natural way to re-run `/setup` is to repeat it
        # with fewer arguments, so when the argument is absent this looks up
        # whatever role is already configured and confirms it still exists
        # in the guild -- a role deleted out from under a stale stored id
        # is not usable and falls through to creating a new one, same as a
        # guild that never configured one at all.
        stored_role: discord.Role | None = None
        if consent_role is None:
            stored_role_id = current.get(settings.CONSENT_ROLE_ID)
            if stored_role_id is not None:
                try:
                    stored_role = guild.get_role(int(stored_role_id))
                except ValueError:
                    stored_role = None

        # The role in effect for this call before any creation below: the
        # explicitly typed argument if given, otherwise the still-valid
        # stored role, otherwise nothing yet.
        role = consent_role if consent_role is not None else stored_role

        # Every channel the guild will allow after this call, the new one
        # included -- so the plan can report which of them still need the
        # consent protection applied, not merely this one.
        channel_permissions = self._read_permissions(
            guild, role, (*stored_channel_ids, channel.id), named=channel
        )

        plan = plan_setup(
            current=current,
            channel_id=channel.id,
            stored_channel_ids=stored_channel_ids,
            channel_permissions=channel_permissions,
            role_id=consent_role.id if consent_role is not None else None,
            stored_role_valid=stored_role is not None,
            policy_url=policy_url,
            policy_version=policy_version,
        )

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
        if plan.role_to_create is not None and role_error is None and role is not None:
            # A role was just created; its id is only known now, so
            # plan_setup could not have included it in `writes`. (A reused
            # stored role, by contrast, is already correctly stored --
            # nothing to write.)
            writes[settings.CONSENT_ROLE_ID] = str(role.id)

        now = self._clock.now()
        for key, value in writes.items():
            await self._store.set(guild.id, key, value, now)

        try:
            result: ReconfigureResult | None = await self._reconcile(guild.id)
        except Exception:
            log.exception("Reconcile after /setup failed for guild %d", guild.id)
            result = None

        applied, permission_errors = await self._apply_permission_changes(
            guild, guild.default_role, role, plan.permission_changes, named=channel
        )

        missing = list(plan.missing)
        if role_error is not None and settings.CONSENT_ROLE_ID not in missing:
            missing.append(settings.CONSENT_ROLE_ID)

        await interaction.followup.send(
            _render_summary(
                writes,
                applied,
                permission_errors,
                role_error,
                list_error,
                missing,
                plan.role_action,
                role,
                plan.channel_ids,
                result,
            ),
            ephemeral=True,
        )

    def _resolve(
        self, guild: discord.Guild, channel_id: int, named: discord.VoiceChannel
    ) -> discord.VoiceChannel | None:
        """The voice channel with this id, or `None` if there is no such thing.

        `named` is the channel Discord's own picker resolved for this call,
        and it is returned for its own id without a cache lookup -- the
        object in hand is at least as good as anything the cache would
        yield, and it is the one the administrator actually chose.
        """
        if channel_id == named.id:
            return named
        channel = guild.get_channel(channel_id)
        return channel if isinstance(channel, discord.VoiceChannel) else None

    def _read_permissions(
        self,
        guild: discord.Guild,
        role: discord.Role | None,
        channel_ids: tuple[int, ...],
        *,
        named: discord.VoiceChannel,
    ) -> list[ChannelPermissions]:
        """Reads each allowed channel's Speak overwrites, skipping what it cannot see.

        A stored list can name a channel that has since been deleted, or one
        this bot has no view of. Such an entry is left out rather than
        guessed at: planning an overwrite for a channel that is not there
        would only produce an error the administrator can do nothing about,
        and it must not stop the channels that *do* exist from being fixed.
        """
        permissions: list[ChannelPermissions] = []
        for channel_id in sorted(set(channel_ids)):
            channel = self._resolve(guild, channel_id, named)
            if channel is None:
                continue
            permissions.append(
                ChannelPermissions(
                    channel_id=channel_id,
                    everyone_may_speak=(
                        channel.overwrites_for(guild.default_role).speak is not False
                    ),
                    role_may_speak=(
                        role is not None and channel.overwrites_for(role).speak is True
                    ),
                )
            )
        return permissions

    async def _apply_permission_changes(
        self,
        guild: discord.Guild,
        everyone: discord.Role,
        consent_role: discord.Role | None,
        changes: list[PermissionChange],
        *,
        named: discord.VoiceChannel,
    ) -> tuple[list[str], list[str]]:
        """Applies each overwrite independently; one failure never blocks the other.

        Returns the human-readable description of every change that
        succeeded, and of every one that did not -- a permission failure is
        reported, never swallowed, because a half-applied setup that claims
        success is worse than one that admits it stopped. That now holds
        per *channel* as well as per overwrite: a guild whose second
        meeting room the bot has no Manage Permissions on must still have
        its first one configured, and must be told which one failed.
        """
        applied: list[str] = []
        errors: list[str] = []
        for change in changes:
            verb = "allow" if change.allow_speak else "deny"
            channel = self._resolve(guild, change.channel_id, named)
            if channel is None:
                errors.append(
                    f"Could not {verb} Speak in <#{change.channel_id}>: it is not a voice "
                    "channel I can see. Remove it with "
                    f"`/config set {settings.VOICE_CHANNEL_IDS} <the remaining ids>`."
                )
                continue
            target: discord.Role | None
            label: str
            if change.target == "everyone":
                target, label = everyone, "@everyone"
            else:
                target, label = consent_role, "the consent role"
                if target is None:
                    errors.append(
                        f"Could not {verb} Speak for the consent role in "
                        f"{channel.mention}: no consent role exists yet. Create one and "
                        "run /setup again, or set it manually and run "
                        f"`/config set {settings.CONSENT_ROLE_ID} <role id>`."
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
                    f"Could not {verb} Speak for {label} in {channel.mention}: I am "
                    "missing the Manage Permissions permission on that channel. Set it "
                    "manually."
                )
            except discord.HTTPException as exc:
                errors.append(f"Could not {verb} Speak for {label} in {channel.mention}: {exc}")
            else:
                applied.append(f"{verb.capitalize()}ed Speak for {label} in {channel.mention}.")
        return applied, errors


def _render_summary(
    writes: dict[str, str],
    applied_permissions: list[str],
    permission_errors: list[str],
    role_error: str | None,
    list_error: str | None,
    missing: list[str],
    role_action: RoleAction,
    role: discord.Role | None,
    channel_ids: tuple[int, ...],
    result: ReconfigureResult | None,
) -> str:
    """Builds the ephemeral reply: what changed, what did not, what is still missing.

    Names keys, never values -- with one deliberate exception: the list of
    allowed channels is printed in full. Today's required keys hold nothing
    sensitive, but this summary is otherwise written so that stays true
    even if a future key does; it reports *that* `policy_url` was written,
    not what it was written to. The channel list is different because it is
    the one value an administrator has to edit by hand to remove an entry,
    and making them go and look it up is how a stale channel stays in the
    list -- channel ids are also already visible to anyone in the server.

    `role_action` is named explicitly (Spec 10.1): a re-run that silently
    replaced an already-working consent role with a fresh, empty one is
    exactly the failure this command must never repeat, so it always says
    plainly whether the role was reused, created, or explicitly replaced.
    """
    lines: list[str] = ["**Setup result**"]

    if writes:
        lines.append("Configuration written: " + ", ".join(f"`{key}`" for key in sorted(writes)))
    else:
        lines.append("Configuration: nothing needed writing, everything was already correct.")

    lines.append(_channel_list_line(channel_ids))

    lines.append(_role_action_line(role_action, role))

    lines.extend(applied_permissions)
    lines.extend(f"⚠️ {error}" for error in permission_errors)
    if role_error is not None:
        lines.append(f"⚠️ {role_error}")
    if list_error is not None:
        lines.append(f"⚠️ {list_error}")

    if missing:
        lines.append("**Still missing:** " + ", ".join(f"`{key}`" for key in sorted(missing)))
    else:
        lines.append("All required configuration is now set.")

    lines.extend(_effect_lines(writes, result))

    return "\n".join(lines)


def _channel_list_line(channel_ids: tuple[int, ...]) -> str:
    """States the whole allowed list, and how to take a channel out of it.

    `/setup` only ever adds, which is what stops a second meeting room from
    un-configuring the first. That makes removal the operation with no
    command of its own, so the reply hands over both halves of it: the list
    as it now stands, and the one command that rewrites it. Copy, delete an
    id, send.

    The one-connection limit is stated here rather than left to the docs
    because this is the moment somebody forms an expectation about it: they
    have just named a second channel and are entitled to know that Sturnus
    will not be in both at once.
    """
    mentions = " ".join(f"<#{channel_id}>" for channel_id in channel_ids)
    ids = settings.render_channel_ids(channel_ids)
    return (
        f"Sturnus may now record in: {mentions}. It joins **one at a time** — a "
        "Discord bot holds a single voice connection per server — and follows "
        "whichever of them has the most consenting members until that session "
        f"ends. To remove one, run `/config set {settings.VOICE_CHANNEL_IDS} {ids}` "
        "with the id you no longer want deleted from the list."
    )


def _effect_lines(writes: dict[str, str], result: ReconfigureResult | None) -> list[str]:
    """Says, per written key, whether it is in force yet.

    Reuses `/config set`'s own wording (`render_write_result`) rather than
    a second phrasing of the same four outcomes: two renderings of "this
    is deferred behind a recording" would drift, and the one that drifts
    is the one that starts lying.
    """
    if not writes:
        return []
    lines = ["**In effect**"]
    for key in sorted(writes):
        lines.append(f"- {render_write_result(key, writes[key], result)}")
    return lines


def _role_action_line(role_action: RoleAction, role: discord.Role | None) -> str:
    """Names, in plain language, which of the three consent-role outcomes happened.

    `role` is `None` here only when a creation was requested but failed
    (see `role_error` at the call site) -- that case is worded around the
    warning already printed above it, rather than claiming a role exists.
    """
    if role_action == "created":
        if role is None:
            return "Consent role: creation was requested (see warning below)."
        return f"Consent role: created {role.mention}."
    if role_action == "reused":
        mention = role.mention if role is not None else "the stored role"
        return f"Consent role: kept the existing {mention} -- nothing changed."
    mention = role.mention if role is not None else "the supplied role"
    return f"Consent role: set to the explicitly supplied {mention}."


__all__ = ["SetupCog", "SetupPlan"]
