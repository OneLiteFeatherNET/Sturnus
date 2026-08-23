"""Setting a guild up against Discord, for `/setup` and for the console alike.

Two things live here. The first is the Discord half of setting a guild
up -- reading a voice channel's Speak overwrites, and writing them back --
which `SetupCog` and the console's intent applier both need and which
neither may own. Those two overwrites are the primary layer of the
consent protection (Spec 3.1): deny `Speak` to `@everyone`, allow it for
the consent role, on **every** allowed channel. A second implementation
that got one of them backwards would let somebody be recorded without
having consented, so there is one implementation, called twice, exactly
as there is one `plan_setup`.

The second is `apply_setup_intents`, the inverse of the mirrors. `api`
must never hold a Discord token (Spec 13.2), so the console cannot create
the consent role, write those overwrites, or register the command tree.
It writes down what should be true instead -- a row in
`guild_setup_intent` -- and this runs on the bot's ordinary ten-second
tick, makes it true through the same `plan_setup` the slash command uses,
and writes back what happened.

**The retry bound is one attempt, and it is the table's own design.** The
tick runs six times a minute forever. An intent left pending after being
applied would re-create the role and re-write the overwrites for the life
of the guild; one left pending after failing would retry a permission
error against Discord's rate limiter just as often. So an attempt settles
the intent whichever way it went, `error` says what Discord answered, and
an administrator who has fixed the permission asks again -- which is a new
row that says who asked and when. There is no back-off to tune because
there is no second attempt to back off from.

**A guild the bot has not joined is not a failure.** It has no
`discord.Guild` for the tick to iterate, so its intents are never
attempted at all and stay pending -- which is the honest state, and the
one the console renders as "waiting for the bot to arrive". They are
applied on the tick after it joins.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

import discord

from sturnus.application.setup_plan import ChannelPermissions, PermissionChange, plan_setup
from sturnus.domain import settings
from sturnus.domain.onboarding import APPLIED, FAILED, SUPERSEDED, SetupIntent, select_intent
from sturnus.observability.events import Event, log_event

log = logging.getLogger(__name__)

#: Reason recorded on Discord's audit log for every permission change and
#: every role creation setup makes. Names the specification section so
#: somebody reading a guild's audit log a year from now can find out why a
#: bot denied `@everyone` the ability to speak.
AUDIT_REASON = "Sturnus setup: consent protection (Spec 3.1)"


class GuildConfiguration(Protocol):
    """The per-guild configuration store, as narrowly as this module needs it."""

    async def get_stored(self, guild_id: int, key: str) -> str | None: ...

    async def set(self, guild_id: int, key: str, value: str | None, now: datetime) -> None: ...


class SetupIntents(Protocol):
    """What the console asked for, and where the answer goes."""

    async def pending_for(self, guild_id: int) -> Sequence[SetupIntent]: ...

    async def record_outcome(
        self, intent_id: int, *, outcome: str, error: str | None, now: datetime
    ) -> bool: ...


# ---------------------------------------------------------------------------
# The Discord half, shared by both callers
# ---------------------------------------------------------------------------


def resolve_voice_channel(
    guild: discord.Guild, channel_id: int, named: discord.VoiceChannel | None = None
) -> discord.VoiceChannel | None:
    """The voice channel with this id, or `None` if there is no such thing.

    `named` is a channel Discord's own picker resolved for this call, and
    it is returned for its own id without a cache lookup -- the object in
    hand is at least as good as anything the cache would yield, and it is
    the one the administrator actually chose. A console intent carries no
    such object and passes nothing.
    """
    if named is not None and channel_id == named.id:
        return named
    channel = guild.get_channel(channel_id)
    return channel if isinstance(channel, discord.VoiceChannel) else None


def read_channel_permissions(
    guild: discord.Guild,
    role: discord.Role | None,
    channel_ids: Sequence[int],
    *,
    named: discord.VoiceChannel | None = None,
) -> list[ChannelPermissions]:
    """Reads each allowed channel's Speak overwrites, skipping what it cannot see.

    A stored list can name a channel that has since been deleted, or one
    this bot has no view of. Such an entry is left out rather than guessed
    at: planning an overwrite for a channel that is not there would only
    produce an error the administrator can do nothing about, and it must
    not stop the channels that *do* exist from being fixed.
    """
    permissions: list[ChannelPermissions] = []
    for channel_id in sorted(set(channel_ids)):
        channel = resolve_voice_channel(guild, channel_id, named)
        if channel is None:
            continue
        permissions.append(
            ChannelPermissions(
                channel_id=channel_id,
                everyone_may_speak=(channel.overwrites_for(guild.default_role).speak is not False),
                role_may_speak=(role is not None and channel.overwrites_for(role).speak is True),
            )
        )
    return permissions


async def apply_permission_changes(
    guild: discord.Guild,
    everyone: discord.Role,
    consent_role: discord.Role | None,
    changes: Sequence[PermissionChange],
    *,
    named: discord.VoiceChannel | None = None,
) -> tuple[list[str], list[str]]:
    """Applies each overwrite independently; one failure never blocks the other.

    Returns the human-readable description of every change that succeeded,
    and of every one that did not -- a permission failure is reported,
    never swallowed, because a half-applied setup that claims success is
    worse than one that admits it stopped. That holds per *channel* as well
    as per overwrite: a guild whose second meeting room the bot has no
    Manage Permissions on must still have its first one configured, and
    must be told which one failed.

    The wording is written to be read by a person in either place it can
    surface -- an ephemeral `/setup` reply, or `guild_setup_intent.error`
    rendered in the console -- so it names the channel and the next action
    rather than quoting an HTTP status.
    """
    applied: list[str] = []
    errors: list[str] = []
    for change in changes:
        verb = "allow" if change.allow_speak else "deny"
        channel = resolve_voice_channel(guild, change.channel_id, named)
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
            await channel.set_permissions(target, overwrite=overwrite, reason=AUDIT_REASON)
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


# ---------------------------------------------------------------------------
# The console's half: applying an intent
# ---------------------------------------------------------------------------


async def apply_setup_intents(
    guild: discord.Guild,
    config: GuildConfiguration,
    intents: SetupIntents,
    now: datetime,
) -> None:
    """Applies this guild's newest unapplied intent, and buries the rest.

    Does nothing at all when there is nothing pending, which is every tick
    but the handful after somebody presses the button in the console.

    Which intent is applied is `select_intent`'s decision and is argued
    where it is made: the newest ask wins outright and every older one
    settles as `SUPERSEDED` without being applied. Applying them in
    sequence would end on whichever request happened to be older, which is
    a correction being overwritten by the mistake it corrected.
    """
    pending = await intents.pending_for(guild.id)
    if not pending:
        return

    selection = select_intent(pending)
    for stale in selection.supersede:
        await intents.record_outcome(stale.id, outcome=SUPERSEDED, error=None, now=now)
        log_event(
            log,
            logging.INFO,
            Event.SETUP_INTENT_SUPERSEDED,
            "A newer setup request replaced this one before the bot reached it",
            guild_id=guild.id,
            discord_user_id=stale.requested_by,
        )
    if selection.apply is None:
        return

    outcome, error = await _apply_one(guild, config, selection.apply, now)
    # Conditional on the intent still being unapplied, so two ticks racing
    # on one guild produce one application and one honest `False`. The
    # loser says nothing: the winner already logged what happened.
    if not await intents.record_outcome(selection.apply.id, outcome=outcome, error=error, now=now):
        return
    log_event(
        log,
        logging.WARNING if outcome == FAILED else logging.INFO,
        Event.SETUP_INTENT_APPLIED,
        "The bot applied a setup request the console wrote",
        guild_id=guild.id,
        discord_user_id=selection.apply.requested_by,
        outcome=outcome,
    )


async def _apply_one(
    guild: discord.Guild,
    config: GuildConfiguration,
    intent: SetupIntent,
    now: datetime,
) -> tuple[str, str | None]:
    """Configures one guild from one intent, and says how it went.

    The database writes happen whether or not the Discord calls succeed,
    exactly as `/setup`'s do: the two are independent, and refusing to
    store what we *did* determine because a permission edit failed would
    leave the guild worse off than before the request. `FAILED` therefore
    means "not everything the bot was asked to do happened", not "nothing
    happened" -- which is why `error` names each part that did not.
    """
    current = {
        key: await config.get_stored(guild.id, key)
        # The deprecated singular key rides along: a guild untouched since
        # the rename still has its channel there, and this must add to that
        # list rather than start a new one beside it.
        for key in settings.REQUIRED_KEYS | {settings.VOICE_CHANNEL_ID}
    }

    problems: list[str] = []
    try:
        stored_channel_ids = settings.recording_channel_ids(current)
    except settings.InvalidChannelList as exc:
        # Only reachable through a direct `UPDATE` -- `ConfigStore.set`
        # refuses an unparseable list at the write. Reported rather than
        # swallowed, because the list this request goes on to write will
        # not contain whatever the unreadable one meant.
        problems.append(
            f"The stored `{settings.VOICE_CHANNEL_IDS}` could not be read ({exc}), so it "
            "was replaced by the channels this request named rather than added to."
        )
        stored_channel_ids = ()

    try:
        requested = (
            () if intent.channel_ids is None else settings.parse_channel_ids(intent.channel_ids)
        )
    except settings.InvalidChannelList as exc:
        # The API validates the list before it writes one, so this is a row
        # somebody wrote by hand. Terminal, and named: there is nothing the
        # bot can do with a channel list it cannot read.
        return FAILED, f"The requested channel list could not be read: {exc}"

    named_role = _role_named(guild, intent.consent_role_name)
    stored_role = _stored_role(guild, current)
    # A request that names a role means that role, whatever is stored. If
    # it exists it is used as-is; if it does not, the stored one is
    # deliberately *not* kept, so the planner asks for a creation and the
    # new role gets the name that was asked for.
    keeps_stored_role = intent.consent_role_name is None and stored_role is not None
    role = named_role if named_role is not None else (stored_role if keeps_stored_role else None)

    channel_permissions = read_channel_permissions(guild, role, (*stored_channel_ids, *requested))
    # A channel this request named that the bot cannot see is refused
    # rather than added. The console picks from the mirror, so this means
    # the mirror is behind Discord -- and adding the channel anyway would
    # put a room on the allowed list whose Speak overwrites nobody wrote,
    # which is a hole in the consent protection that looks like a
    # configured guild. A channel already *stored* and now unseeable is a
    # different case and left alone: it is not this request's doing, and
    # failing every future request over it would help nobody.
    visible = {each.channel_id for each in channel_permissions}
    unseen = tuple(channel_id for channel_id in requested if channel_id not in visible)
    problems.extend(
        f"This request named <#{channel_id}>, which is not a voice channel I can see, "
        "so it was not added. Pick it again once the console shows it."
        for channel_id in unseen
    )

    plan = plan_setup(
        current=current,
        added_channel_ids=tuple(channel_id for channel_id in requested if channel_id in visible),
        stored_channel_ids=stored_channel_ids,
        channel_permissions=channel_permissions,
        role_id=named_role.id if named_role is not None else None,
        stored_role_valid=keeps_stored_role,
        # Never carried by an intent: the console sets the policy on the
        # settings page, and a second place to write `policy_version` is a
        # second way to invalidate every consent in the guild.
        policy_url=None,
        policy_version=None,
    )

    if plan.role_to_create is not None:
        # The name the console asked for wins over the planner's default,
        # which is what the planner falls back to when nobody named one.
        wanted = intent.consent_role_name or plan.role_to_create
        try:
            role = await guild.create_role(name=wanted, reason=AUDIT_REASON)
        except discord.Forbidden:
            problems.append(
                f"I am missing the Manage Roles permission, so I could not create the "
                f"`{wanted}` role. Grant it -- and make sure my own role sits above the "
                "consent role in Server Settings -> Roles -- then ask again."
            )
        except discord.HTTPException as exc:
            problems.append(f"Could not create the `{wanted}` role: {exc}")

    writes = dict(plan.writes)
    if plan.role_to_create is not None and role is not None:
        # A role was just created; its id is only known now, so the plan
        # could not have included it. A reused stored role is already
        # correctly stored and needs no write.
        writes[settings.CONSENT_ROLE_ID] = str(role.id)
    for key, value in writes.items():
        await config.set(guild.id, key, value, now)

    _, permission_errors = await apply_permission_changes(
        guild, guild.default_role, role, plan.permission_changes
    )
    problems.extend(permission_errors)

    if problems:
        return FAILED, "\n".join(problems)
    return APPLIED, None


def _role_named(guild: discord.Guild, name: str | None) -> discord.Role | None:
    """The guild's role with exactly this name, if it already has one.

    Asked before anything is created, so asking twice for the same consent
    role is idempotent rather than a guild slowly filling with identically
    named roles. Exact match and never fuzzy: "Recorded" and "recorded"
    are two roles in Discord, and picking the wrong one would grant
    recording consent through a role nobody was told about.
    """
    if name is None:
        return None
    return discord.utils.get(guild.roles, name=name)


def _stored_role(guild: discord.Guild, current: dict[str, str | None]) -> discord.Role | None:
    """The configured consent role, if the id still names one in this guild.

    A role deleted out from under a stale stored id is not usable, and
    counts the same as never having configured one -- the alternative is
    writing overwrites for a role that grants nobody anything.
    """
    stored = current.get(settings.CONSENT_ROLE_ID)
    if stored is None:
        return None
    try:
        return guild.get_role(int(stored))
    except ValueError:
        return None


__all__ = [
    "AUDIT_REASON",
    "GuildConfiguration",
    "SetupIntents",
    "apply_permission_changes",
    "apply_setup_intents",
    "read_channel_permissions",
    "resolve_voice_channel",
]
