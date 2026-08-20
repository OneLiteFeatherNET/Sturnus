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

DEFAULTS: dict[str, str] = {
    EMPTY_GRACE_SECONDS: "60",
    IDLE_TIMEOUT_MINUTES: "15",
    MAX_SESSION_HOURS: "4",
    PUBLISH_POLL_SECONDS: "30",
    DOCUMENT_PROVIDER: "outline",
    AUDIO_RETENTION_DAYS: "30",
    MERGE_GAP_SECONDS: "15",
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
    }
)
