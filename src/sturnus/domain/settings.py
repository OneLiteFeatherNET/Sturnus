"""Default values for runtime configuration (Spec 11)."""

from __future__ import annotations

from collections.abc import Mapping

#: The channels Sturnus may record in, as a comma-separated list of ids.
#: A guild names every room a meeting might happen in rather than the one
#: room somebody thought of first.
#:
#: **What a list does and does not mean.** A Discord bot holds exactly one
#: voice connection per guild -- a platform limit, enforced by discord.py
#: and by `infrastructure.discord.voice`, not a choice this project made.
#: So the list means "Sturnus may record in any of these, and follows the
#: one that is meeting", never "Sturnus records all of them at once".
#: Recording two rooms of one guild simultaneously needs a second bot
#: identity, which is a deployment decision nobody has taken.
VOICE_CHANNEL_IDS = "voice_channel_ids"

#: The singular spelling `VOICE_CHANNEL_IDS` replaced, kept readable
#: **indefinitely**. A guild configured before the list existed must keep
#: recording, and rewriting its row would be a database migration bought
#: for a rename -- `guild_config` is keyed `(guild_id, key)`, so the old
#: name is a row, not a column, and nothing about it is in the way.
#:
#: It is therefore a fallback and never a second source of truth:
#: `recording_channel_ids` reads the plural key and only drops back to
#: this one when the plural is unset. `/setup` and `/config` write the
#: plural; `/config show` tells a guild still on this key to move.
VOICE_CHANNEL_ID = "voice_channel_id"
CONSENT_ROLE_ID = "consent_role_id"
EMPTY_GRACE_SECONDS = "empty_grace_seconds"
IDLE_TIMEOUT_MINUTES = "idle_timeout_minutes"
MAX_SESSION_HOURS = "max_session_hours"
PUBLISH_POLL_SECONDS = "publish_poll_seconds"
DOCUMENT_PROVIDER = "document_provider"
DOCUMENT_TARGET = "document_target"
AUDIO_RETENTION_DAYS = "audio_retention_days"
POLICY_VERSION = "policy_version"
POLICY_URL = "policy_url"
ADMIN_ROLE_ID = "admin_role_id"
MERGE_GAP_SECONDS = "merge_gap_seconds"
TIMEZONE = "timezone"
TRANSCRIPTION_LANGUAGE = "transcription_language"
TRANSCRIPTION_PROMPT = "transcription_prompt"

#: The one value of `TRANSCRIPTION_LANGUAGE` that is not a language: it
#: asks the engine to detect one per speaker and pin what it found for the
#: rest of the session, which is what the worker did unconditionally before
#: this key existed. It exists because `DEFAULTS` below names a language,
#: and `/config clear` restores a default rather than removing it -- without
#: a spelling for "detect", a guild that really does meet in several
#: languages would have no way back to detection at all.
DETECT_LANGUAGE = "auto"

DEFAULTS: dict[str, str] = {
    EMPTY_GRACE_SECONDS: "60",
    IDLE_TIMEOUT_MINUTES: "15",
    MAX_SESSION_HOURS: "4",
    PUBLISH_POLL_SECONDS: "30",
    DOCUMENT_PROVIDER: "outline",
    AUDIO_RETENTION_DAYS: "30",
    MERGE_GAP_SECONDS: "15",
    # Protocols are read by the people who were in the room, so the
    # times in them are theirs, not the cluster's. A wrong offset is
    # not obviously wrong to a reader -- 15:08 looks like a plausible
    # meeting time whether or not it is the right one.
    TIMEZONE: "Europe/Berlin",
    # Naming the language is worth far more than it looks. The
    # alternative is detection, which runs on one speaker's track after
    # the silence has been cut out of it, and a speaker whose first
    # contribution is "ja, genau" gives it almost nothing to go on --
    # German, Dutch and Danish are all plausible readings of three
    # seconds of that. Whatever comes back is then pinned for that
    # speaker for the rest of the session, so a single unlucky guess is
    # not one bad job, it is every job for that person from then on.
    # German is what this deployment's guilds actually meet in; `auto`
    # (see `DETECT_LANGUAGE` above) is how one that does not says so.
    TRANSCRIPTION_LANGUAGE: "de",
    # Whisper's `initial_prompt` biases the decoder towards the
    # vocabulary and the style of this text. Proper nouns are where a
    # general model fails and where a protocol is judged: a model that
    # has never seen "Ducula" will confidently write the nearest word it
    # has seen, and a decision recorded about the wrong project is worse
    # than no minutes at all. The default is OneLiteFeather's own
    # vocabulary -- the bird-named projects and the stack they are built
    # on -- written as an ordinary German sentence so the style it biases
    # towards is punctuated prose in the language above rather than a
    # bare word list.
    TRANSCRIPTION_PROMPT: (
        "Protokoll eines OneLiteFeather-Meetings über die Projekte Falco, Otis, "
        "Ducula, Pica, Guira, Aves, Sturnus, Cygnus, Apus und Coris sowie über "
        "Minestom, Paper, Outline, Harbor, Flux, Kubernetes, Renovate und Gradle."
    ),
}

# No default value, so these must be set before going live.
REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        VOICE_CHANNEL_IDS,
        CONSENT_ROLE_ID,
        DOCUMENT_TARGET,
        POLICY_VERSION,
        POLICY_URL,
        ADMIN_ROLE_ID,
    }
)

#: Keys that are still read and still writable, but that nothing requires
#: and nothing new should be set to. Kept out of `REQUIRED_KEYS` so a guild
#: on the current spelling is not told it is missing a key it deliberately
#: does not use, and kept in `KNOWN_KEYS` so `/config clear` can still
#: remove one an administrator no longer wants.
LEGACY_KEYS: frozenset[str] = frozenset({VOICE_CHANNEL_ID})

#: Every key anything in this system reads, and the only keys
#: `ConfigStore.set` accepts. Computed once here rather than restated by
#: each caller: `/config`, the console's settings view and the store itself
#: all gate on this, and three hand-maintained unions are three lists that
#: disagree the day a fourth key class appears.
KNOWN_KEYS: frozenset[str] = frozenset(DEFAULTS) | REQUIRED_KEYS | LEGACY_KEYS

#: Both spellings of "which channels may be recorded", newest first. The
#: pair is one setting wearing two names, and every caller that has to
#: decide "is this key about the recording channels?" asks here.
VOICE_CHANNEL_KEYS: tuple[str, ...] = (VOICE_CHANNEL_IDS, VOICE_CHANNEL_ID)

# Keys whose stored value must parse as a positive integer. Checked by
# ConfigStore.set at write time so a bad value never reaches the read path.
INTEGER_KEYS: frozenset[str] = frozenset(
    {
        EMPTY_GRACE_SECONDS,
        IDLE_TIMEOUT_MINUTES,
        MAX_SESSION_HOURS,
        PUBLISH_POLL_SECONDS,
        AUDIO_RETENTION_DAYS,
        MERGE_GAP_SECONDS,
        ADMIN_ROLE_ID,
    }
)


class InvalidChannelList(ValueError):
    """`voice_channel_ids` held something that is not a list of channel ids.

    A named error rather than a bare `ValueError` so a caller can report
    *this* problem specifically -- `ConfigStore.set` refuses the write with
    it, `/config set` prints it, and the bot's reconcile pass keeps the
    configuration already in force rather than un-configuring a working
    guild over somebody's typo in a shell.
    """


def parse_channel_ids(value: str) -> tuple[int, ...]:
    """Reads `"12, 34"` as `(12, 34)`, or says why it cannot.

    Whitespace around each entry is ignored, because the natural way to
    type a list is with spaces after the commas. Everything else is
    refused rather than repaired:

    * a non-integer entry, because guessing which channel `"genral"` meant
      is how a guild ends up recording the wrong room;
    * a duplicate, because it is always a mistake and silently collapsing
      it hides the mistake from the person who made it;
    * an empty list or an empty entry (`"12,,34"`, `""`), because "allowed
      to record nowhere" is what `/config clear` is for and a trailing
      comma should not mean it.

    The result is **sorted**. Order carries no meaning -- which channel is
    served is decided by who is actually sitting in them
    (`sturnus.application.channel_choice`) -- so normalising it here is
    what stops a re-ordered list from reading as a configuration change
    and retargeting a guild for nothing.
    """
    seen: set[int] = set()
    ids: list[int] = []
    for entry in value.split(","):
        text = entry.strip()
        if not text:
            raise InvalidChannelList(
                f"{VOICE_CHANNEL_IDS!r} must be a comma-separated list of channel ids, "
                f"and {value!r} has an empty entry"
            )
        try:
            channel_id = int(text)
        except ValueError as exc:
            raise InvalidChannelList(
                f"{VOICE_CHANNEL_IDS!r} must be a comma-separated list of channel ids, "
                f"and {text!r} is not one"
            ) from exc
        if channel_id <= 0:
            raise InvalidChannelList(
                f"{VOICE_CHANNEL_IDS!r} must hold positive channel ids, got {channel_id}"
            )
        if channel_id in seen:
            raise InvalidChannelList(
                f"{VOICE_CHANNEL_IDS!r} names channel {channel_id} more than once"
            )
        seen.add(channel_id)
        ids.append(channel_id)
    if not ids:
        raise InvalidChannelList(f"{VOICE_CHANNEL_IDS!r} must name at least one channel")
    return tuple(sorted(ids))


def render_channel_ids(channel_ids: tuple[int, ...]) -> str:
    """The stored spelling of a list of channel ids.

    Beside the parser rather than at either call site, so the one thing
    `/setup` writes and the one thing `parse_channel_ids` reads cannot
    drift into disagreeing about the separator.
    """
    return ",".join(str(channel_id) for channel_id in sorted(channel_ids))


def recording_channel_ids(snapshot: Mapping[str, str | None]) -> tuple[int, ...]:
    """Which channels this guild allows Sturnus to record in.

    The plural key wins outright when it is set; the singular one
    (`VOICE_CHANNEL_ID`) is read only when it is not. That ordering is the
    whole deprecation: a guild that has never touched the setting since
    the rename keeps recording, and one that has set the new key is not
    quietly overruled by a stale row nobody remembered to delete.

    Returns an empty tuple when neither key is set -- a genuine answer
    ("this guild is not configured to record anywhere"), which is why it
    is not an error. An unparseable value *is* an error and raises
    `InvalidChannelList`, because a guild whose list cannot be read is not
    a guild allowed to record nowhere.
    """
    for key in VOICE_CHANNEL_KEYS:
        value = snapshot.get(key)
        if value is not None:
            return parse_channel_ids(value)
    return ()


def missing_required(snapshot: Mapping[str, str | None]) -> frozenset[str]:
    """The required keys this guild still has neither a value nor a default for.

    The recording channels are one requirement with two spellings, so the
    pair is satisfied by *either* key being set -- and reported under the
    plural one, because that is the key an administrator should now set.
    Reporting the singular one would tell somebody to configure a
    deprecated setting.
    """
    missing = {key for key in REQUIRED_KEYS if snapshot.get(key) is None}
    if VOICE_CHANNEL_IDS in missing and snapshot.get(VOICE_CHANNEL_ID) is not None:
        missing.discard(VOICE_CHANNEL_IDS)
    return frozenset(missing)


def canonical_key(key: str) -> str:
    """The current spelling of a key, for callers comparing two of them.

    `voice_channel_id` and `voice_channel_ids` are one setting, so a reply
    about a write to the old name has to recognise itself in a reconcile
    result that names the new one. Without this, `/config set
    voice_channel_id ...` mid-session would be told the change is in force
    while the bot is in fact still recording the old channel -- exactly
    the lie `render_write_result` exists to prevent.
    """
    return VOICE_CHANNEL_IDS if key == VOICE_CHANNEL_ID else key
