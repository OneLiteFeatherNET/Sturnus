"""The decisions behind setting a guild up, separated from Discord (Spec 10.1).

`/setup` exists so the six required configuration keys can be set from
typed command parameters -- Discord renders native pickers for a channel
and a role, instead of an administrator hand-typing ids copied out of
developer mode. It also configures the channel's voice permissions itself:
denying Speak for `@everyone` and allowing it for the consent role is the
primary layer of the consent protection (Spec 3.1), and leaving that step
to prose in an operations guide would put the one step nobody may get
wrong into the hands of whoever reads the guide least carefully.

A guild names a *list* of channels Sturnus may record in, so setup
**adds** the channels it was given to that list rather than replacing it.
Setting up a second meeting room used to silently un-configure the first,
which is the kind of failure nobody notices until the meeting that was
supposed to be recorded was not. The permissions are then planned for
*every* allowed channel, not only the ones just named: a channel that is
allowed but whose `@everyone` may still Speak is a hole in the consent
protection whether or not this call is the one that added it.

**One planner, two callers.** `/setup` is one of them;
`sturnus.infrastructure.discord.setup_apply`, which applies what the
console asked for through `guild_setup_intent`, is the other. A second
implementation of the consent protection is the last thing this system
should grow -- one that got either overwrite backwards would let somebody
be recorded without having consented -- so the two callers differ only in
where their arguments come from. That is what `added_channel_ids` being a
tuple and the policy pair being optional are for: the slash command names
one room and is always told the policy, the console names a list and is
never told it.

This module holds only the comparison between the desired state and what
is already configured -- no Discord object reaches it, only the ids and
permission facts already read off them. That is what makes it testable
without a guild, and what makes setup safe to run twice: re-running
`plan_setup` against a correctly configured guild returns an empty plan,
not a rewrite.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sturnus.domain import settings


@dataclass(frozen=True)
class ChannelPermissions:
    """What one allowed channel's Speak overwrites say today.

    Read off a `discord.VoiceChannel` by the caller and handed in as two
    booleans, so this module keeps knowing nothing about Discord. A channel
    the caller cannot resolve is simply not in the list it passes -- and is
    therefore not planned for, rather than being planned for wrongly.
    """

    channel_id: int
    everyone_may_speak: bool
    role_may_speak: bool


@dataclass(frozen=True)
class PermissionChange:
    """One voice-channel permission overwrite that setup needs to apply.

    `target` is `"everyone"` or `"consent_role"` -- which Discord role the
    change applies to is resolved by the caller, not carried here.

    `channel_id` says *where*. It has to: the plan now covers every allowed
    channel, and the caller applies each change to the channel it names
    rather than to the one that happened to be typed into this call.
    """

    channel_id: int
    target: str
    allow_speak: bool | None


#: What `/setup` decided about the consent role: an existing stored role was
#: kept (`"reused"`), a new one needs creating (`"created"`), or an
#: explicitly supplied role took over (`"replaced"`) -- whether or not that
#: supplied role happens to match what was already stored.
RoleAction = Literal["reused", "created", "replaced"]


@dataclass(frozen=True)
class SetupPlan:
    """The difference between the desired state and what is already configured.

    `writes` holds only the keys whose value actually changes -- an unchanged
    value is left out so applying the plan never rewrites what is already
    correct. `permission_changes` is empty when the channel's overwrites
    already match the desired policy. `role_to_create` is set only when
    `role_action` is `"created"`, since a role missing from Discord cannot be
    assigned an id to write. `missing` lists required keys that setup cannot
    fill in on its own.
    """

    writes: dict[str, str]
    permission_changes: list[PermissionChange]
    role_to_create: str | None
    role_action: RoleAction
    missing: list[str]
    #: Every channel the guild allows once this call has been applied --
    #: what was already stored plus the channel just named. Reported so the
    #: reply can print the list an administrator would edit to remove one.
    channel_ids: tuple[int, ...]


#: Name given to the consent role setup offers to create when none exists.
_DEFAULT_ROLE_NAME = "Sturnus Consent"


def plan_setup(
    current: dict[str, str | None],
    added_channel_ids: tuple[int, ...],
    stored_channel_ids: tuple[int, ...],
    channel_permissions: Sequence[ChannelPermissions],
    role_id: int | None,
    stored_role_valid: bool,
    policy_url: str | None,
    policy_version: str | None,
) -> SetupPlan:
    """Computes what setting this guild up still needs to do.

    `current` maps configuration keys to their stored value; a key absent
    from `current` means the caller has no information about it, and it is
    left out of `missing` rather than assumed unset -- `missing` reports
    only what the caller already knows (an explicit `None`) is still open.
    Both real callers read every required key before building `current`,
    so in practice every gap does surface; this function itself stays
    conservative about keys it was never told about.

    `document_target` cannot be derived from anything Discord exposes -- it
    names an Outline collection -- so it is never guessed here. If the
    caller reports it unset, it is reported in `missing`, exactly like a
    guild that has never been set up at all.

    `stored_channel_ids` is what the guild already allows, read through
    `settings.recording_channel_ids` so the key this replaced still counts.
    `added_channel_ids` is **added** to it, never substituted for it:
    setting up a second meeting room must not stop the first one being
    recorded. Removing one is `/config set voice_channel_ids`, which is why
    the resulting list is reported back rather than merely written.

    A tuple rather than one id because the two callers ask differently:
    `/setup` names one room, since Discord renders one channel picker per
    parameter, and the console names the whole list a person ticked. Empty
    is a legitimate ask from the console -- "leave the channels as they
    are and fix the rest" -- and leaves the stored list untouched.

    `channel_permissions` describes the Speak overwrites of every allowed
    channel the caller could resolve, the newly added ones included. Every
    allowed channel is planned for, not only the ones this call named: a
    channel Sturnus may record in whose `@everyone` can still Speak is a
    hole in the consent protection regardless of which call added it.

    `role_id` is a consent role the caller resolved for this call -- the
    slash command's `consent_role` parameter, or the role a console intent
    named by name and that turned out to exist -- or `None` if there was
    none. Omitting it must never itself be destructive (Spec 10.1), so it
    never means "create a new role" the way it once did. What happens when
    it is omitted is governed by `stored_role_valid`: `True` means
    `current[settings.CONSENT_ROLE_ID]` names a role the caller has
    confirmed still exists in the guild, so it is kept as-is; `False` means
    there is nothing usable to keep, and a new role is requested. Discord
    role existence cannot be checked from here -- there is no guild object
    in this module -- so the caller resolves that before calling in.

    `policy_url` and `policy_version` are what this call was *told* the
    policy is, and `None` means it was told nothing. `/setup` always has
    both, because they are required command parameters an administrator
    typed. A console intent has neither: the console sets them on the
    settings page, where they are ordinary keys, and an onboarding request
    that carried them would be a second place to write the two values that
    decide whose consent is still valid. `None` therefore writes nothing
    and leaves the key to be reported in `missing` if the guild has not
    set it -- which is the honest answer for a guild that cannot record
    yet.
    """
    writes: dict[str, str] = {}

    def _maybe_write(key: str, desired: str | None) -> None:
        # `None` is "this caller was not told", never "clear it": nothing
        # in setup removes a value, and a caller with no opinion about a
        # key must leave what is stored alone.
        if desired is not None and current.get(key) != desired:
            writes[key] = desired

    channel_ids = tuple(sorted({*stored_channel_ids, *added_channel_ids}))
    # The plural key, always -- setup is one of the two things that moves a
    # guild off the singular one. The old row is left alone rather than
    # deleted: it is read only when the plural key is unset, so from this
    # write on it is inert, and removing it would be a second write nobody
    # asked for. Skipped entirely for a guild that allows nothing and was
    # given nothing, because `render_channel_ids(())` is the empty string
    # and `ConfigStore.set` refuses it -- rightly, since "allowed to record
    # nowhere" is what `/config clear` is for.
    if channel_ids:
        _maybe_write(settings.VOICE_CHANNEL_IDS, settings.render_channel_ids(channel_ids))
    _maybe_write(settings.POLICY_URL, policy_url)
    _maybe_write(settings.POLICY_VERSION, policy_version)

    role_to_create: str | None = None
    role_action: RoleAction
    if role_id is not None:
        role_action = "replaced"
        _maybe_write(settings.CONSENT_ROLE_ID, str(role_id))
    elif stored_role_valid:
        role_action = "reused"
        # The stored id is already correct and still valid -- nothing to
        # write, nothing to create.
    else:
        role_action = "created"
        role_to_create = _DEFAULT_ROLE_NAME

    permission_changes: list[PermissionChange] = []
    for channel in sorted(channel_permissions, key=lambda each: each.channel_id):
        if channel.everyone_may_speak:
            permission_changes.append(
                PermissionChange(channel.channel_id, "everyone", allow_speak=False)
            )
        if not channel.role_may_speak:
            permission_changes.append(
                PermissionChange(channel.channel_id, "consent_role", allow_speak=True)
            )

    # Required keys this call supplied itself: either written above or
    # already correct, so neither is still open. Derived from what the
    # caller was actually told rather than tabulated, because the two
    # callers are told different things -- a console intent carries no
    # policy, and a guild that is allowed to record nowhere and was named
    # no channel genuinely is missing `voice_channel_ids`.
    supplied = {
        # Either already correct (`role_id` given, nothing to write), or a
        # new role is about to be created and will get an id once it
        # exists -- setup does not fabricate one in advance.
        settings.CONSENT_ROLE_ID,
    }
    if channel_ids:
        supplied.add(settings.VOICE_CHANNEL_IDS)
    if policy_url is not None:
        supplied.add(settings.POLICY_URL)
    if policy_version is not None:
        supplied.add(settings.POLICY_VERSION)

    missing: list[str] = []
    for key in sorted(settings.REQUIRED_KEYS):
        if key in writes or key in supplied:
            continue
        if key in current and current[key] is None:
            missing.append(key)

    return SetupPlan(
        writes=writes,
        permission_changes=permission_changes,
        role_to_create=role_to_create,
        role_action=role_action,
        missing=missing,
        channel_ids=channel_ids,
    )
