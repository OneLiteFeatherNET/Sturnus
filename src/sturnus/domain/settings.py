"""Default values for runtime configuration (Spec 11)."""

from __future__ import annotations

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
        VOICE_CHANNEL_ID,
        CONSENT_ROLE_ID,
        DOCUMENT_TARGET,
        POLICY_VERSION,
        POLICY_URL,
        ADMIN_ROLE_ID,
    }
)

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
