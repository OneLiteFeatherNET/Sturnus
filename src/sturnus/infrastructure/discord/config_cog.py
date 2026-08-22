"""Admin slash commands for per-guild runtime configuration (Spec 11).

`REQUIRED_KEYS` has no defaults and must be set explicitly before a guild's
capture pipeline can go live; `missing_required` is what finally checks that.

Every command that writes also *reconciles* and then reports what actually
took effect. That is not decoration: writing a value and replying "`key`
set to `value`" while the running process keeps using the old one is the
defect this whole change exists to fix, and a reply that still implies the
write is the end of the story would only move the lie one layer up. So
this module distinguishes four honest outcomes — in force now, waiting for
the recording in progress, needs a process restart, and stored-but-the-
guild-still-is-not-watchable — and says which one happened.

The rendering is deliberately pure functions taking a `ReconfigureResult`
rather than inline string building inside command bodies, so the wording
can be tested without an `Interaction`.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from sturnus.application.reconfigure import (
    RESTART_REQUIRED_KEYS,
    Reconfigure,
    ReconfigureAction,
    ReconfigureResult,
    RunningState,
    RunningStateReader,
)
from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.discord.permissions import require_admin

log = logging.getLogger(__name__)

#: Every key `ConfigStore.set` accepts — the same union it validates against.
_KNOWN_KEYS: frozenset[str] = frozenset(settings.DEFAULTS) | settings.REQUIRED_KEYS

#: Re-exported from `sturnus.application.reconfigure`, which is where the
#: fact now lives: "read once at process start" is a property of how the bot
#: reads a key, exactly like `IDENTITY_KEYS` and `TUNABLE_KEYS` beside it —
#: and it has a second reader now. The console's API process has no gateway
#: and cannot reconcile at all (Spec 13.2), so it must tell an operator
#: which of the three classes their write falls into, and a second
#: hand-maintained list of restart-only keys is a list that disagrees with
#: this one the day a fourth key joins it.
__all__ = ["RESTART_REQUIRED_KEYS", "ConfigCog"]

#: Keys whose shortening can close the session already in progress.
_TIMEOUT_KEYS: frozenset[str] = frozenset(
    {
        settings.EMPTY_GRACE_SECONDS,
        settings.IDLE_TIMEOUT_MINUTES,
        settings.MAX_SESSION_HOURS,
    }
)

_EXCEEDED_WARNING = (
    "⚠️ The session in progress already exceeds this and will close on the next "
    "tick — it will be uploaded and transcribed normally, nothing is lost."
)


async def missing_required(store: ConfigStore, guild_id: int) -> list[str]:
    """Lists required keys that have neither a stored value nor a default.

    Sorted so repeated calls and command output stay stable — `REQUIRED_KEYS`
    is a frozenset and offers no ordering guarantee of its own.
    """
    missing = [key for key in settings.REQUIRED_KEYS if await store.get(guild_id, key) is None]
    return sorted(missing)


async def _effective(store: ConfigStore, guild_id: int, key: str) -> tuple[str | None, str]:
    """Returns the effective value of `key` and its source.

    Source is one of "stored", "default", or "unset".
    """
    stored = await store.get_stored(guild_id, key)
    if stored is not None:
        return stored, "stored"
    default = settings.DEFAULTS.get(key)
    if default is not None:
        return default, "default"
    return None, "unset"


@app_commands.guild_only()
class ConfigCog(
    commands.GroupCog, name="config", description="Manage Sturnus's runtime configuration."
):
    """Admin-only `/config` command group.

    None of today's keys hold a secret, so `/config show` is safe to print
    in full — that must be re-checked before any key that does gets added.
    """

    def __init__(
        self,
        store: ConfigStore,
        reconcile: Reconfigure,
        running_state: RunningStateReader,
    ) -> None:
        self._store = store
        #: Bound methods of the client rather than the client itself: a cog
        #: importing `SturnusClient` would close an import cycle, and one
        #: holding it could do far more than ask it to re-read config.
        self._reconcile = reconcile
        self._running_state = running_state
        super().__init__()

    async def _reconcile_quietly(
        self, guild_id: int, *, force: bool = False
    ) -> ReconfigureResult | None:
        """Reconciles, returning `None` if the pass itself failed.

        The write already succeeded by the time this runs, so a failure
        here must not read as a failed command — but it must not be
        silently rendered as "in effect now" either. `None` is the third
        answer, and the reply says so.
        """
        try:
            return await self._reconcile(guild_id, force=force)
        except Exception:
            log.exception("Reconcile after a /config command failed for guild %d", guild_id)
            return None

    @app_commands.command(
        name="get", description="Show the effective value of a configuration key."
    )
    @app_commands.describe(key="Configuration key, e.g. voice_channel_id")
    @require_admin()
    async def get(self, interaction: discord.Interaction, key: str) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if key not in _KNOWN_KEYS:
            await interaction.response.send_message(
                f"Unknown configuration key: `{key}`.", ephemeral=True
            )
            return
        value, source = await _effective(self._store, guild_id, key)
        rendered = value if value is not None else "*(unset)*"
        await interaction.response.send_message(f"`{key}` = {rendered} ({source})", ephemeral=True)

    @app_commands.command(name="set", description="Set a configuration key for this server.")
    @app_commands.describe(key="Configuration key", value="New value")
    @require_admin()
    async def set(self, interaction: discord.Interaction, key: str, value: str) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        # Everything below is I/O: a database write, then a full reconcile
        # pass that re-reads the guild's configuration and may build a
        # whole recording pipeline. Discord fails the interaction if the
        # *initial* response has not gone out within three seconds, and
        # then reports "The application did not respond" over a `/config
        # set` that in fact wrote the value -- the worst of both. Deferring
        # first buys fifteen minutes, exactly as `/setup` and `/config
        # apply` already do.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._store.set(guild_id, key, value, discord.utils.utcnow())
        except ValueError as exc:
            await interaction.followup.send(f"Rejected: {exc}", ephemeral=True)
            return
        result = await self._reconcile_quietly(guild_id)
        await interaction.followup.send(render_write_result(key, value, result), ephemeral=True)

    @app_commands.command(
        name="clear", description="Clear a configuration key, restoring its default."
    )
    @app_commands.describe(key="Configuration key")
    @require_admin()
    async def clear(self, interaction: discord.Interaction, key: str) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if key not in _KNOWN_KEYS:
            await interaction.response.send_message(
                f"Unknown configuration key: `{key}`.", ephemeral=True
            )
            return
        # The key check above is a frozenset lookup and answers instantly;
        # from here on it is a write plus a reconcile, so the same
        # three-second deadline applies as in `set` above.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._store.set(guild_id, key, None, discord.utils.utcnow())
        result = await self._reconcile_quietly(guild_id)
        await interaction.followup.send(render_write_result(key, None, result), ephemeral=True)

    @app_commands.command(
        name="show", description="List every configuration key and what is still missing."
    )
    @require_admin()
    async def show(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        lines: list[str] = []
        for key in sorted(_KNOWN_KEYS):
            value, source = await _effective(self._store, guild_id, key)
            rendered = value if value is not None else "*(unset)*"
            lines.append(f"`{key}` = {rendered} ({source})")
        missing = await missing_required(self._store, guild_id)
        lines.append("")
        if missing:
            lines.append("**Missing required keys:** " + ", ".join(f"`{k}`" for k in missing))
        else:
            lines.append("All required keys are set.")
        # The line that stops this command from insisting a value is in use
        # when the running process is still on the previous one.
        lines.append(render_running_state(self._running_state(guild_id), bool(missing)))
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(
        name="apply",
        description="Re-read the configuration now (e.g. after a direct database edit).",
    )
    @app_commands.describe(
        force=(
            "Also end the recording in progress so a deferred channel/role change "
            "applies immediately. The recording is uploaded and transcribed normally."
        )
    )
    @require_admin()
    async def apply(self, interaction: discord.Interaction, force: bool = False) -> None:
        """Re-syncs this guild without writing anything.

        Useful after a direct `UPDATE` against `guild_config`, or to see
        plainly what the process is doing. Without `force` it is a
        no-op re-sync; the periodic reconcile would reach the same state
        within ten seconds anyway.
        """
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        # `force` ends the session in progress the ordinary way -- encrypt,
        # upload, enqueue -- which is real S3 and database I/O and can
        # comfortably exceed Discord's three second initial-response window.
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._reconcile_quietly(guild_id, force=force)
        await interaction.followup.send(render_apply_result(result, force=force), ephemeral=True)


def render_write_result(key: str, value: str | None, result: ReconfigureResult | None) -> str:
    """States what a `/config set` or `/config clear` actually achieved.

    `value` is `None` for a clear. The four outcomes, in the order they are
    checked: the key cannot be picked up without a restart at all; the
    reconcile failed and we do not know; the key is stored but waiting
    behind a recording; the key is in force.
    """
    written = f"`{key}` set to `{value}`." if value is not None else f"`{key}` cleared."

    if key in RESTART_REQUIRED_KEYS:
        return (
            f"{written} Stored — but this key is read once at process start, so it "
            "takes effect only after the bot pod restarts."
        )

    if result is None:
        return (
            f"{written} Stored — but Sturnus could not re-read its configuration just "
            "now, so it may not be in force yet. It retries automatically within 10 "
            "seconds; `/config apply` forces another attempt."
        )

    lines = [f"{written} {_effect_sentence(key, result)}"]
    if key in _TIMEOUT_KEYS and result.session_exceeds_timeouts:
        lines.append(_EXCEEDED_WARNING)
    return "\n".join(lines)


def _effect_sentence(key: str, result: ReconfigureResult) -> str:
    """The one sentence that says whether this key is actually being used."""
    if result.action is ReconfigureAction.DEFER_TEARDOWN:
        return (
            "Stored — a recording is in progress. It will finish and upload "
            "normally, and then Sturnus stops watching that channel. "
            "**The recording will not be lost.**"
        )
    if key in result.deferred_keys:
        return (
            "Stored — but a recording is in progress in the old channel. It takes "
            f"effect when that session ends (at the latest after "
            f"`{settings.MAX_SESSION_HOURS}`). **The recording will not be lost.**"
        )
    if result.action is ReconfigureAction.TEARDOWN:
        return "Sturnus has stopped watching this server's voice channel."
    if result.became_live:
        return (
            "All required keys are now set — Sturnus is watching the configured "
            "voice channel, with no restart needed."
        )
    if result.is_live:
        return "In effect now."
    return (
        "Stored, but Sturnus is not watching this server yet — run `/config show` "
        "to see which required keys are still missing."
    )


def render_running_state(state: RunningState, has_missing_keys: bool) -> str:
    """The `/config show` line about what the process is *doing*, not storing."""
    if not state.is_live:
        if has_missing_keys:
            return (
                "Running configuration: not applied — Sturnus is not watching this "
                "server, see the missing keys above."
            )
        return (
            "Running configuration: not applied yet — Sturnus is not watching this "
            "server. `/config apply` re-reads immediately."
        )
    if state.pending_teardown:
        return (
            "Running configuration: a recording is in progress; when it ends "
            "Sturnus stops watching this server."
        )
    if state.pending_keys:
        keys = ", ".join(f"`{key}`" for key in state.pending_keys)
        return (
            f"Running configuration: {len(state.pending_keys)} key(s) waiting for the "
            f"current recording to end: {keys}."
        )
    return "Running configuration: in effect."


def render_apply_result(result: ReconfigureResult | None, *, force: bool) -> str:
    """The `/config apply` reply, including what `force` did before it applied."""
    lines: list[str] = []
    if force:
        lines.append(
            "**`force` was used:** any recording in progress was ended first and "
            "uploaded and transcribed normally, before the new configuration applied."
        )
    if result is None:
        lines.append("Could not re-read the configuration; nothing changed. See the bot's logs.")
        return "\n".join(lines)

    if result.deferred_keys:
        keys = ", ".join(f"`{key}`" for key in result.deferred_keys)
        lines.append(
            f"Re-read. {keys} is waiting for the recording in progress to end; run "
            "`/config apply force:true` to end it now instead (it is still uploaded "
            "and transcribed)."
        )
    elif result.became_live:
        lines.append("Re-read. Sturnus is now watching this server's voice channel.")
    elif result.applied_keys:
        keys = ", ".join(f"`{key}`" for key in result.applied_keys)
        lines.append(f"Re-read. Now in force: {keys}.")
    elif result.is_live:
        lines.append("Re-read. Nothing had changed; the running configuration was already correct.")
    else:
        lines.append(
            "Re-read. Sturnus is not watching this server — run `/config show` to see "
            "which required keys are still missing."
        )
    return "\n".join(lines)
