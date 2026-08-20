"""The decisions behind `/setup`, separated from Discord (Spec 10.1).

`/setup` exists so the six required configuration keys can be set from
typed command parameters -- Discord renders native pickers for a channel
and a role, instead of an administrator hand-typing ids copied out of
developer mode. It also configures the channel's voice permissions itself:
denying Speak for `@everyone` and allowing it for the consent role is the
primary layer of the consent protection (Spec 3.1), and leaving that step
to prose in an operations guide would put the one step nobody may get
wrong into the hands of whoever reads the guide least carefully.

This module holds only the comparison between the desired state and what
is already configured -- no Discord object reaches it, only the ids and
permission facts already read off them. That is what makes it testable
without a guild, and what makes the command safe to run twice: re-running
`plan_setup` against a correctly configured guild returns an empty plan,
not a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sturnus.domain import settings

#: Required keys always supplied through `/setup`'s own parameters -- either
#: written this call or already correct, so they are never reported missing.
_ALWAYS_SUPPLIED: frozenset[str] = frozenset(
    {settings.VOICE_CHANNEL_ID, settings.POLICY_URL, settings.POLICY_VERSION}
)


@dataclass(frozen=True)
class PermissionChange:
    """One voice-channel permission overwrite that setup needs to apply.

    `target` is `"everyone"` or `"consent_role"` -- which Discord role the
    change applies to is resolved by the caller, not carried here.
    """

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


#: Name given to the consent role setup offers to create when none exists.
_DEFAULT_ROLE_NAME = "Sturnus Consent"


def plan_setup(
    current: dict[str, str | None],
    channel_id: int,
    role_id: int | None,
    stored_role_valid: bool,
    policy_url: str,
    policy_version: str,
    everyone_may_speak: bool,
    role_may_speak: bool,
) -> SetupPlan:
    """Computes what `/setup` still needs to do.

    `current` maps configuration keys to their stored value; a key absent
    from `current` means the caller has no information about it, and it is
    left out of `missing` rather than assumed unset -- `missing` reports
    only what the caller already knows (an explicit `None`) is still open.
    A real caller (`SetupCog`) reads every required key before building
    `current`, so in practice every gap does surface; this function itself
    stays conservative about keys it was never told about.

    `document_target` cannot be derived from anything Discord exposes -- it
    names an Outline collection -- so it is never guessed here. If the
    caller reports it unset, it is reported in `missing`, exactly like a
    guild that has never run `/setup` at all.

    `role_id` is the consent role explicitly supplied to `/setup`'s
    `consent_role` parameter this call, or `None` if that argument was
    omitted -- omitting it must never itself be destructive (Spec 10.1),
    so it never means "create a new role" the way it once did. What
    happens when it is omitted is governed by `stored_role_valid`: `True`
    means `current[settings.CONSENT_ROLE_ID]` names a role the caller has
    confirmed still exists in the guild, so it is kept as-is; `False` means
    there is nothing usable to keep, and a new role is requested. Discord
    role existence cannot be checked from here -- there is no guild object
    in this module -- so the caller resolves that before calling in.
    """
    writes: dict[str, str] = {}

    def _maybe_write(key: str, desired: str) -> None:
        if current.get(key) != desired:
            writes[key] = desired

    _maybe_write(settings.VOICE_CHANNEL_ID, str(channel_id))
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
    if everyone_may_speak:
        permission_changes.append(PermissionChange("everyone", allow_speak=False))
    if not role_may_speak:
        permission_changes.append(PermissionChange("consent_role", allow_speak=True))

    missing: list[str] = []
    for key in sorted(settings.REQUIRED_KEYS):
        if key in writes:
            continue
        if key == settings.CONSENT_ROLE_ID:
            # Either already correct (role_id given, nothing to write), or a
            # new role is about to be created and will get an id once it
            # exists -- setup does not fabricate one in advance.
            continue
        if key in _ALWAYS_SUPPLIED:
            continue
        if key in current and current[key] is None:
            missing.append(key)

    return SetupPlan(
        writes=writes,
        permission_changes=permission_changes,
        role_to_create=role_to_create,
        role_action=role_action,
        missing=missing,
    )
