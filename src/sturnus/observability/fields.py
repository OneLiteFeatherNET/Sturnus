"""The closed registry of what Sturnus is prepared to put in a retained store.

Read this file before adding a name to it. Every entry below is copied into
Loki and, unless it is in `LOG_ONLY_FIELDS`, into Tempo -- both of which
index it, retain it, and show it to anyone with Grafana access. The gate in
`docs/verification/end-to-end-checklist.md` states the standard this list is
held to: "A log line naming a user id, a job id, or a status code is fine; a
log line containing what someone said, or the bytes of what they said, or a
credential, is not."

**Allowlist, not denylist.** `redaction.scrub_fields` rebuilds its output
from `ALLOWED_FIELDS` rather than deleting known-bad keys from its input,
the same inversion `sturnus.infrastructure.observability.scrub_event` makes
for Sentry events and for the same reason: a denylist is correct about the
code it was written against and silently wrong about the next call site
somebody adds. Rebuilding inverts the failure mode -- a field nobody
registered is dropped, so a mistake costs a missing panel in Grafana rather
than a transcript in Loki.

**One registry, three spellings, derived not restated.** Logs want flat
snake_case (`job_id`), OpenTelemetry wants dotted attributes
(`sturnus.job_id`), and a handful of concepts already have names in the
OpenTelemetry semantic conventions (`error.type`) that would be perverse to
reinvent. `span_attribute()` derives the span spelling from the log
spelling, so there is no second list to fall out of step with this one.
`tests/observability/test_redaction.py` pins the derivation, and
`tests/infrastructure/test_telemetry.py` checks the semantic-convention
literals below against the constants the installed
`opentelemetry-semantic-conventions` actually exports.
"""

from __future__ import annotations

from typing import Final

#: The Python processes built from the one image. The same literal names
#: `sturnus.infrastructure.observability.init_sentry` takes as its
#: `component` tag and `[project.scripts]` uses, so an operator reading a
#: Sentry issue, a Loki stream and a Tempo service graph sees one word for
#: one process rather than four near-synonyms.
#:
#: `api` arrived without being added here, which is exactly what the note
#: on `service_name` below warned about -- nothing enforces this tuple, so
#: it drifted silently for the length of one change. The console is
#: deliberately absent: it is a Node process that calls none of this.
COMPONENTS: Final = ("bot", "worker", "link", "api")


#: OpenTelemetry `service.name` per component. Derived, not tabulated, so a
#: fourth component cannot arrive with a name that agrees with nothing.
def service_name(component: str) -> str:
    """`service.name` for one component -- `bot` -> `sturnus-bot`."""
    return f"sturnus-{component}"


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

#: Opaque identifiers. Database primary keys and platform-issued ids: each
#: names a row, a server, a room or a document, and none of them is content.
#: `guild_id`/`channel_id` are already logged today
#: (`infrastructure/discord/client.py`), and `document_id` at
#: `infrastructure/documents/outline.py`.
_IDENTIFIERS = frozenset(
    {
        "session_id",
        "job_id",
        "guild_id",
        "channel_id",
        "document_id",
        "collection_id",
        "key_id",
        "configured_key_id",
        "provider",
        "ssrc",
        #: Which of this process's gateway connections a guild's events
        #: arrive on -- Discord's `(guild_id >> 22) % shard_count`. An
        #: identifier of a connection rather than of a row, and bounded by
        #: the shard count, which is why it sits here rather than among
        #: the measurements.
        #:
        #: **Emitted only when this process holds more than one shard.**
        #: It is a pure function of `guild_id`, so on a single-shard
        #: process it is `0` on every line for ever -- a key in every Loki
        #: stream that answers nothing. `sturnus.application.sharding.
        #: shard_fields` is the one place that decision is made; see its
        #: docstring for why LogQL cannot recover it from `guild_id`
        #: instead.
        #:
        #: Deliberately **not** in `METRIC_LABEL_FIELDS`. `guild_id`
        #: already is, and a shard id adds no dimension a guild id does
        #: not already carry -- it would only multiply series.
        "shard_id",
    }
)

#: Identifiers for a *person*. Pseudonymous rather than identifying -- a
#: Discord snowflake and an Outline UUID -- and an operator cannot answer
#: "did this person's `/audio delete` actually erase their recordings"
#: without them, which is itself a compliance question. `audio_cog.py`
#: already logs the Discord one.
#:
#: They are nonetheless **log-only**: see `LOG_ONLY_FIELDS`.
#:
#: `requested_by` is the second person in a line that has two: the console
#: serves one participant's voice to another, and an access log for that
#: which records only whose voice it was answers half the question anyone
#: would ever ask of it.
_SUBJECT_IDENTIFIERS = frozenset({"discord_user_id", "external_user_id", "requested_by"})

#: Fixed literals from this repository's own source: enum members, stage
#: names, outcome words. Bounded by construction, which is what makes them
#: safe as metric labels as well as safe to log.
#:
#: `close_code` and `http_status` are the two exceptions to "from this
#: repository's own source", and they are bounded by the same argument from
#: somewhere else: both are protocol constants with a documented, finite
#: value set (RFC 6455 plus Discord's own 4xxx voice codes; RFC 9110). See
#: `infrastructure.discord.voice.voice_close_code` for why `close_code`
#: exists at all -- it is the diagnosis a withheld exception message takes
#: with it.
_LITERALS = frozenset(
    {
        "close_code",
        "component",
        "stage",
        "outcome",
        "reason",
        "end_reason",
        "status",
        "language",
        "model",
        "device",
        "compute_type",
        "version",
        "error_type",
        "http_method",
        "http_status",
        "server_address",
        "url_path",
        "permanent",
        "is_last",
        "listening",
        "missing",
        #: What a consent covers -- `sturnus.domain.consent.ConsentScope`,
        #: two literals from this repository's own source. Never the
        #: policy document, never a URL: the scope is `audio` or
        #: `audio_video` and nothing else can be written here.
        "scope",
        #: Whether a revocation named its own effective instant or took
        #: the default of now. A boolean, and the one thing about a
        #: console revocation that `consent.revoked_at` cannot recover:
        #: an administrator back-dating a withdrawal to last March and
        #: one clicking "withdraw" both leave a perfectly ordinary date
        #: in the column, and only this field says which act it was.
        "effective_at_given",
        #: A key of `sturnus.domain.settings` -- one of the literals in
        #: `KNOWN_KEYS`, from this repository's own source, and checked
        #: against the registry before anything logs it. The *value* is
        #: not here and must not be: `transcription_prompt` is free text
        #: an administrator typed, and `policy_url` is a URL.
        "config_key",
        #: Whether the person who downloaded a recording was in the
        #: session it belongs to. A boolean, and the whole difference
        #: between two acts that leave otherwise identical lines: somebody
        #: keeping a copy of their own meeting, and an administrator
        #: obtaining a recording of a meeting they were not part of.
        #: Nothing else can recover it afterwards -- `session_participant`
        #: is not frozen at the moment of the download, and no other row
        #: records that a copy was made at all.
        "by_participant",
    }
)

#: Counts, sizes and durations. Numbers about content, never content.
_MEASUREMENTS = frozenset(
    {
        "attempt",
        "attempts",
        "max_attempts",
        "lease_seconds",
        "count",
        "deleted",
        "failed",
        "speakers",
        "skipped",
        "participants",
        "blocks",
        "packets",
        "segments",
        "segment_count",
        "char_count",
        "title_chars",
        "body_bytes",
        "bytes",
        "object_bytes",
        "plaintext_bytes",
        "audio_seconds",
        #: How much of a speaker's recording `speech_gate.speech_clips`
        #: found above the silence floor, and in how many clips. Against
        #: `audio_seconds` this is the gate's own signature and the one
        #: number that names the failure mode that cost this project two
        #: days: "one second of speech in two minutes of recording" is not
        #: a plausible meeting. Nothing else can report it --
        #: `job.transcribed` is emitted from `sturnus.application.worker`,
        #: which never sees the gate.
        "speech_seconds",
        "clips",
        "wall_seconds",
        "realtime_factor",
        "duration_seconds",
        "seconds_since_last_packet",
        "consented_present",
        "jobs_enqueued",
        #: How many people a bulk consent withdrawal actually withdrew,
        #: and how many it changed nothing for. On
        #: `console.consent_bulk_revoked` only, where they are the shape
        #: of the outcome: "nine named, one withdrawn, eight already
        #: gone" is a different act from "nine named, nine withdrawn",
        #: and the per-person lines beside it can only be counted by
        #: somebody who already knows which batch to count.
        "revoked",
        "refused",
        #: How many gateway connections this one process holds. On the
        #: shard lifecycle lines only -- `bot.connected`,
        #: `bot.shard_ready`, `bot.shard_disconnected` -- where it is the
        #: denominator that makes "shard 3 is down" mean something. Not on
        #: per-guild lines: there it would be the same number on every one
        #: of them, which is what `shard_id` is conditional to avoid.
        "shard_count",
    }
)

#: Correlation ids the OpenTelemetry SDK produces. 128- and 64-bit random
#: numbers carrying no data; they are what turns a Loki line into a click
#: through to the Tempo trace.
_CORRELATION = frozenset({"trace_id", "span_id"})

ALLOWED_FIELDS: Final[frozenset[str]] = (
    _IDENTIFIERS | _SUBJECT_IDENTIFIERS | _LITERALS | _MEASUREMENTS | _CORRELATION
)

#: Fields whose `None` means "this does not apply here" rather than "we
#: looked, and there was none" -- dropped by `redaction.scrub_fields`
#: instead of being written as `null`.
#:
#: **The contrast that defines the set is `close_code`.** A
#: `voice.reader_stopped` line with `close_code: null` is a *finding*: the
#: adapter looked at the exception, it was an `OSError` rather than a
#: `discord.ConnectionClosed`, and there genuinely is no close code. That
#: null is worth a key, and `voice_close_code`'s docstring argues the case.
#:
#: `shard_id` is the other kind. On a process holding one shard it is not
#: "no shard"; the concept simply has nothing to say, and writing
#: `shard_id: null` into every guild line for ever is the noise the
#: registry exists to keep out. The distinction is registered here rather
#: than decided by `scrub_fields` for every field at once, precisely so
#: that adding a field to it is a deliberate act with a reason attached.
#:
#: This is also what lets a call site emit a field conditionally at all:
#: rule R3 in `tests/test_logging_discipline.py` forbids `**kwargs` into a
#: log event, so the field name must be written in the source and the
#: *value* is the only place the condition can live.
OMITTED_WHEN_NONE: Final[frozenset[str]] = frozenset({"shard_id"})

#: Registered, logged, and deliberately kept out of spans and metrics.
#:
#: The checklist blesses a user id in a pod log, and this codebase already
#: writes one. A user id joined to a session id and precise timestamps in a
#: *searchable, retained, trace-indexed* store is a different artifact: a
#: record of who was in which voice channel when, reachable by a wider
#: Grafana audience than `kubectl logs`. Nothing is lost by the exclusion --
#: `sturnus.session_id` on the span joins to the row that has the user id,
#: for anyone with the access to look -- and it keeps metric cardinality
#: bounded for free.
#:
#: `SAFE_SPAN_ATTRIBUTES` and `METRIC_LABEL_FIELDS` are both computed with
#: this set subtracted, so the exclusion is a control rather than a habit.
LOG_ONLY_FIELDS: Final[frozenset[str]] = _SUBJECT_IDENTIFIERS

#: Fields that may become a *metric* attribute. Metrics multiply: one new
#: value of one attribute is a whole new time series forever. Only fixed
#: source literals and `guild_id` qualify -- see `docs/operations.md`
#: section 7 for the cardinality argument, and note that `guild_id` is the
#: first thing to review if Sturnus is ever deployed multi-tenant at scale.
METRIC_LABEL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "component",
        #: The Whisper model name, and the only label the transcription
        #: progress metrics carry beyond it. Bounded by deployment rather
        #: than by source -- `STURNUS_WHISPER_MODEL` is a Helm value -- but
        #: bounded all the same: one process loads exactly one model, so a
        #: cluster produces one series per distinct model ever deployed.
        #: It is also the dimension the numbers are meaningless without,
        #: since a real-time factor compares `large-v3` against `tiny`
        #: otherwise.
        "model",
        "stage",
        "outcome",
        "reason",
        "end_reason",
        "status",
        "language",
        "error_type",
        "http_status",
        "permanent",
        "guild_id",
    }
)

# ---------------------------------------------------------------------------
# Names that must never appear, and are named so the test can say so
# ---------------------------------------------------------------------------

#: Argument, attribute and `extra`-key names that carry payload. Not the
#: mechanism that keeps them out -- `ALLOWED_FIELDS` is, by rebuilding --
#: but the list `tests/test_logging_discipline.py` fails the build on, so
#: `log_event(Event.JOB_DONE, transcript=result.text)` is caught in CI
#: rather than dropped silently at runtime and wondered about later.
#:
#: `s3_key` is here rather than in the registry on purpose. The key format
#: is `sessions/{session_id}/speakers/{discord_user_id}.enc`
#: (`sturnus.application.recording.audio_key`), so it *embeds* a user id --
#: and both halves are already registered separately, which makes the key
#: itself pure duplication with a wider blast radius.
#: The payload half: what someone said, or who said it.
_PAYLOAD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "transcript",
        "transcripts",
        "text",
        "body",
        "pcm",
        "opus",
        "packet_data",
        "payload",
        "plaintext",
        "display_name",
        "discord_display_name",
        "participant_names",
        "s3_key",
    }
)

#: The credential half: what authorises or decrypts.
#:
#: Split out from the payload half rather than kept as one flat set,
#: because `redaction.PATTERNS` needs exactly this half and nothing else.
#: A string of the shape `<one of these>: <value>` in *any* record --
#: including a message a third-party library composed, which no allowlist
#: over Sturnus's own field names can see -- is scrubbed on its way to the
#: formatter. Applying the payload half there too would redact the word
#: after every "text:" and "body:" an English sentence contains, which is
#: how a control earns itself a suppression.
#:
#: Derived, not restated: `DENIED_NAMES` below is the union, so the static
#: rule in `tests/test_logging_discipline.py` still sees one list and the
#: two halves cannot drift apart into disagreement.
CREDENTIAL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "token",
        "access_token",
        "api_token",
        "api_key",
        "secret",
        "client_secret",
        "secret_key",
        "access_key",
        "master_key",
        "data_key",
        "wrapped",
        "wrapped_data_key",
        "password",
        "authorization",
        "database_url",
        "dsn",
        "get_secret_value",
    }
)

DENIED_NAMES: Final[frozenset[str]] = _PAYLOAD_NAMES | CREDENTIAL_NAMES

# ---------------------------------------------------------------------------
# Deriving the span spelling
# ---------------------------------------------------------------------------

#: The prefix for attributes this project invents. Everything under it is
#: ours; everything outside it in `SAFE_SPAN_ATTRIBUTES` is a name
#: OpenTelemetry already standardised.
SPAN_ATTRIBUTE_NAMESPACE: Final = "sturnus."

#: Fields whose span spelling is an OpenTelemetry semantic convention
#: rather than `sturnus.<field>`. Written as literals because this module is
#: standard-library only (see the package docstring);
#: `tests/infrastructure/test_telemetry.py` asserts each one equals the
#: constant `opentelemetry-semantic-conventions` exports, so the literal
#: cannot drift from the convention without a red test.
SEMCONV_SPAN_ATTRIBUTES: Final[dict[str, str]] = {
    "error_type": "error.type",
    "http_method": "http.request.method",
    "http_status": "http.response.status_code",
    "server_address": "server.address",
    "url_path": "url.path",
}


def span_attribute(field: str) -> str:
    """The span-attribute spelling of a registered field name.

    The single conversion between the two vocabularies. Nothing else in the
    codebase writes an attribute key as a string literal, which is what
    keeps `SAFE_SPAN_ATTRIBUTES` and the emitting call sites in step.
    """
    return SEMCONV_SPAN_ATTRIBUTES.get(field, f"{SPAN_ATTRIBUTE_NAMESPACE}{field}")


#: Every attribute key an exported span may carry, derived from the registry
#: above. `sturnus.infrastructure.telemetry.AllowlistingSpanExporter`
#: rebuilds each span from exactly this set on the way out -- the second,
#: independent lock behind `scrub_fields`.
SAFE_SPAN_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    span_attribute(field) for field in ALLOWED_FIELDS - LOG_ONLY_FIELDS
)
