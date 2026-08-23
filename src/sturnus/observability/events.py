"""The event vocabulary, and the one sanctioned way to emit a log line.

An operator's questions are about a *session*, not about a module. The
names below are chosen so that `| json | session_id="4711"` in LogQL
returns one readable narrative that crosses all three processes:

    guild.configured -> voice.joined -> session.opened
      -> session.speaker_first_packet -> session.closing
      -> session.speaker_finalized -> session.closed
      -> job.claimed -> job.transcribed -> session.document_created
      -> announce.posted

`session_id` is the join key for the whole story and is line content, never
a Loki label -- it is unbounded, and promoting it would multiply the cluster's
stream count without limit. `docs/operations.md` section 7 carries the label
policy and the queries this vocabulary was designed to answer.

Levels are part of the design, not decoration:

- `DEBUG` -- counts, sizes and housekeeping. Sturnus's own DEBUG lines are
  held to ids, counts, sizes and durations by the same registry that governs
  INFO; they are never payload.
- `INFO` -- the narrative above. One line per event, never per packet.
- `WARNING` -- retried, and expected to self-heal.
- `ERROR` -- **a human must act.** Reserved for permanent loss, for capture
  that has silently stopped, and for a guild that has stopped being able to
  record. `job.dead`, `session.unrecoverable`, `session.document_rejected`,
  `voice.join_failed`, `voice.reader_stopped`, `voice.decode_failed`,
  `voice.packet_handler_failed`, `voice.left_failed`,
  `voice.rejoin_blocked`, `guild.tick_failed` and `session.close_failed`
  are the ones that earn it.

`voice.join_failed`, `voice.reader_stopped` and `voice.decode_failed` are
one family and are deliberately not one name. They are the three ways this
process can end up in a voice channel hearing nothing while everyone in it
has been told they are recorded: capture never started
(`voice.join_failed`), capture started and then died
(`voice.reader_stopped`), or capture is running and no stream decodes any
more (`voice.decode_failed`). All three end the session with an
`end_reason` that says "we could not hear" rather than "nobody spoke", and
telling them apart in Loki is the difference between suspecting libopus,
suspecting the gateway, and suspecting the channel.

The last four are the bot's *operational* failures, and they were bare
`log.exception("... %d ...", guild_id)` calls until this vocabulary reached
them. A `%d`-formatted id is invisible to `| json | guild_id="..."`, which
is the one query this whole package exists to make possible -- and
`scrub_event` forwards `LogRecord.msg` to Sentry, so an id interpolated
into the message is also the half of the line that leaves the pod. They
carry fields now, and the message stayed a literal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Final


class Event(StrEnum):
    """The closed set of event names. New lines pick a name from here."""

    # -- bot: the session story -------------------------------------------
    GUILD_CONFIGURED = "guild.configured"
    GUILD_UNCONFIGURED = "guild.unconfigured"
    BOT_CONNECTED = "bot.connected"
    #: One gateway connection finished its IDENTIFY and handed this process
    #: its share of the guilds. Fires once per shard, and on every
    #: re-IDENTIFY of that shard afterwards -- which is the *only* signal
    #: that a shard came back, because `on_ready` does not fire again for
    #: a single shard once the whole set has been ready once. See
    #: `SturnusClient.on_shard_ready`.
    BOT_SHARD_READY = "bot.shard_ready"
    #: One gateway connection resumed after a blip, replaying the events it
    #: missed. INFO rather than WARNING: a RESUME is the gateway working,
    #: and no guild cache was lost, so nothing is re-reconciled.
    BOT_SHARD_RESUMED = "bot.shard_resumed"
    #: One gateway connection dropped. **WARNING**, not ERROR: discord.py
    #: reconnects on its own and the overwhelming majority of these are
    #: followed by `bot.shard_resumed` within seconds. It is ERROR-shaped
    #: only if it stays -- which is what `/readyz` is for, and why
    #: readiness is scoped to shards rather than to a single boolean.
    BOT_SHARD_DISCONNECTED = "bot.shard_disconnected"
    VOICE_JOINED = "voice.joined"
    VOICE_LEFT = "voice.left"
    VOICE_JOIN_FAILED = "voice.join_failed"
    VOICE_READER_STOPPED = "voice.reader_stopped"
    VOICE_DECODE_FAILED = "voice.decode_failed"
    VOICE_PACKET_REJECTED = "voice.packet_rejected"
    VOICE_PACKET_HANDLER_FAILED = "voice.packet_handler_failed"
    VOICE_LEFT_FAILED = "voice.left_failed"
    VOICE_REJOIN_BLOCKED = "voice.rejoin_blocked"
    GUILD_TICK_FAILED = "guild.tick_failed"
    SESSION_CLOSE_FAILED = "session.close_failed"
    SESSION_OPENED = "session.opened"
    SESSION_SPEAKER_FIRST_PACKET = "session.speaker_first_packet"
    #: A speaker whose packets arrive, decode, and carry no audible level --
    #: what a microphone muted at system level produces. Three lines, because
    #: the durable record and the message into the room can each fail on
    #: their own and neither may take the capture path down with it.
    SPEAKER_AUDIO_SILENT = "speaker.audio_silent"
    SPEAKER_SILENT_WARNING_FAILED = "speaker.silent_warning_failed"
    SPEAKER_SILENT_RECORD_FAILED = "speaker.silent_record_failed"
    SESSION_CLOSING = "session.closing"
    SESSION_SPEAKER_FINALIZED = "session.speaker_finalized"
    SESSION_CLOSED = "session.closed"
    SESSION_RECOVERED = "session.recovered"
    SESSION_UNRECOVERABLE = "session.unrecoverable"
    #: `/queue requeue`'s confirmation buttons could not be greyed out. The
    #: answer the administrator is waiting for is not lost with them -- the
    #: edit is swallowed on purpose -- so this line is the only trace.
    QUEUE_VIEW_DISABLE_FAILED = "queue.view_disable_failed"
    ANNOUNCE_POSTED = "announce.posted"
    ANNOUNCE_FAILED = "announce.failed"
    AUDIO_ERASED = "audio.erased"

    # -- worker ------------------------------------------------------------
    WORKER_STARTED = "worker.started"
    JOB_CLAIMED = "job.claimed"
    JOB_TRANSCRIBED = "job.transcribed"
    TRANSCRIPTION_SKIPPED = "transcription.skipped"
    TRANSCRIPTION_DECODED = "transcription.decoded"
    JOB_FAILED = "job.failed"
    JOB_DEAD = "job.dead"
    #: A worker finished a job it no longer holds: its lease expired
    #: mid-transcription and a second worker took the job over. The work
    #: is discarded rather than applied -- see
    #: `sturnus.infrastructure.db.queue._report_lost_claim`.
    JOB_CLAIM_LOST = "job.claim_lost"
    KEY_ID_MISMATCH = "key.id_mismatch"
    SESSION_DOCUMENT_CREATED = "session.document_created"
    SESSION_DOCUMENT_REJECTED = "session.document_rejected"
    SESSION_DOCUMENT_RETRY_FAILED = "session.document_retry_failed"
    RETENTION_SWEPT = "retention.swept"
    RETENTION_FAILED = "retention.failed"

    # -- link ---------------------------------------------------------------
    LINK_STARTED = "link.started"
    LINK_CALLBACK_REJECTED = "link.callback_rejected"
    LINK_EXCHANGE_FAILED = "link.exchange_failed"
    LINK_ESTABLISHED = "link.established"
    LINK_STATES_PURGED = "link.states_purged"

    # -- console ------------------------------------------------------------
    CONSOLE_STARTED = "console.started"
    CONSOLE_SIGNED_IN = "console.signed_in"
    CONSOLE_SIGN_IN_REJECTED = "console.sign_in_rejected"
    CONSOLE_STATES_PURGED = "console.states_purged"
    #: The access log for the most consequential thing the console does:
    #: one person played another person's voice back. `session_id`,
    #: `discord_user_id` (whose voice) and `requested_by` (who listened)
    #: are the three that make the line answer the question anyone would
    #: ask of it afterwards.
    CONSOLE_TRACK_SERVED = "console.track_served"
    #: A copy of somebody's voice left the console as a file. **WARNING,
    #: and deliberately so:** a download outlives every control this
    #: system has -- it is in a Downloads folder, and nothing here can
    #: expire it, sweep it or take it back, so the line saying it happened
    #: is the only record there will ever be.
    #:
    #: A different event from `CONSOLE_TRACK_SERVED` because it is a
    #: different act, and `by_participant` separates the two acts *this*
    #: event covers: a participant keeping a copy of their own meeting,
    #: and an administrator obtaining a recording of a meeting they were
    #: not in. The second is the one read in this system that reaches
    #: another person's voice without the reader having been in the room
    #: with them, and it is the reason the field exists.
    CONSOLE_TRACK_DOWNLOADED = "console.track_downloaded"
    #: A recording that was asked for and not handed over -- because the
    #: asker was not in the session, or because the retention sweep has
    #: already erased the audio. `reason` says which.
    CONSOLE_TRACK_REFUSED = "console.track_refused"
    #: An administrator asked, from the console, for a session to be
    #: transcribed again. **WARNING, and deliberately so:** it clears
    #: transcripts and will replace a document a team has already read, so
    #: the line that says who asked is the only record of why that
    #: document changed under them.
    CONSOLE_REQUEUE_APPLIED = "console.requeue_applied"
    #: A re-queue that was asked for and refused, because the session was
    #: not in a state a redo is safe from. INFO: this is the feature
    #: working, not failing.
    CONSOLE_REQUEUE_REFUSED = "console.requeue_refused"
    #: A stored recording that this reader cannot make sense of. **A human
    #: must act:** the object is there, the person is entitled to it, and
    #: it will not decrypt -- which is either a truncated upload or a
    #: format that has drifted from its reader.
    CONSOLE_TRACK_UNREADABLE = "console.track_unreadable"

    #: An administrator changed a guild's runtime configuration from the
    #: web console. The only trace such a change leaves: a slash command
    #: at least leaves the administrator holding its reply, while a form
    #: submission leaves nothing behind but this line. It names who, which
    #: guild and which key -- never the value, since `transcription_prompt`
    #: is free text somebody typed.
    CONSOLE_SETTING_WRITTEN = "console.setting_written"
    #: `ConfigStore.set` refused the value. Ordinary in the sense that a
    #: typo in a number field produces it, and worth a line anyway: it is
    #: the one place where the console and the store could ever disagree
    #: about what a valid value is.
    CONSOLE_SETTING_REJECTED = "console.setting_rejected"

    #: An administrator withdrew somebody else's consent to be recorded.
    #: **WARNING, and for a stronger reason than `console.requeue_applied`
    #: has:** this is a third party acting on a person's own consent, and
    #: `consent.revoked_at` records only that it happened, never who did
    #: it. This line is the entire answer to "who withdrew whose consent,
    #: and when" -- `guild_id`, `discord_user_id` (whose consent) and
    #: `requested_by` (who withdrew it).
    CONSOLE_CONSENT_REVOKED = "console.consent_revoked"
    #: A revocation that changed nothing, because there was no consent on
    #: record or it had already been withdrawn. INFO: two administrators
    #: reaching for the same name is this feature working. `reason` says
    #: which of the two it was.
    CONSOLE_CONSENT_REVOKE_REFUSED = "console.consent_revoke_refused"
    #: A person withdrew their **own** consent from the console. INFO
    #: rather than WARNING, and the asymmetry with the line above it is
    #: deliberate: nobody is acting on anybody else here, which is the
    #: ordinary case the feature exists for. It carries `requested_by`
    #: equal to `discord_user_id` anyway, so a query over both events
    #: answers "who withdrew whose consent" without a row falling out of
    #: it for lack of the field.
    CONSOLE_CONSENT_SELF_REVOKED = "console.consent_self_revoked"
    #: A person changed what their own consent covers -- `scope` names
    #: what it covers now. A widening is a new `consent` row and a
    #: narrowing modifies the existing one, so the table itself does not
    #: record the two as the same kind of event; this line does.
    CONSOLE_CONSENT_SCOPE_CHANGED = "console.consent_scope_changed"
    #: A scope change that changed nothing. `reason` says why -- most
    #: often `video_consent_offered` being false, which is the guild
    #: saying its policy document does not name video.
    CONSOLE_CONSENT_SCOPE_REFUSED = "console.consent_scope_refused"

    # -- cross-cutting ------------------------------------------------------
    PROCESS_STARTING = "process.starting"
    SHUTDOWN_BEGIN = "shutdown.begin"
    SHUTDOWN_COMPLETE = "shutdown.complete"
    SCHEMA_WAITING = "schema.waiting"
    #: The startup line that says a requested third-party log level was
    #: raised to `setup.THIRD_PARTY_FLOOR`. An operator who turned the knob
    #: up and sees nothing new needs to be told why, in the same place they
    #: are already looking.
    LOG_LEVEL_CLAMPED = "log.level_clamped"
    SWEEP_FAILED = "sweep.failed"
    TELEMETRY_ENABLED = "telemetry.enabled"
    UNHANDLED_EXCEPTION = "unhandled.exception"


#: Filled in by `sturnus.infrastructure.telemetry.install_trace_context`
#: once an OpenTelemetry provider exists. A module-level hook rather than an
#: import because this package is standard-library only (see the package
#: docstring) and must never reach for the OTel API; with no telemetry
#: installed it stays `None` and every log line simply has no `trace_id`.
#:
#: This is the Loki -> Tempo link: a `trace_id` in the JSON line is what a
#: Grafana derived field turns into a click through to the waterfall for the
#: same job.
_trace_context_provider: Callable[[], dict[str, str]] | None = None


def set_trace_context_provider(provider: Callable[[], dict[str, str]] | None) -> None:
    """Installs (or clears) the hook that supplies `trace_id`/`span_id`."""
    global _trace_context_provider
    _trace_context_provider = provider


def current_trace_context() -> dict[str, str]:
    """`{"trace_id": ..., "span_id": ...}` when a span is active, else `{}`.

    Never raises: a broken telemetry provider must not be able to stop a log
    line being written, because the log line is the fallback for telemetry
    being broken.
    """
    provider = _trace_context_provider
    if provider is None:
        return {}
    try:
        return provider()
    except Exception:  # pragma: no cover - defensive; see docstring
        return {}


def log_event(
    logger: logging.Logger,
    level: int,
    event: Event,
    message: str,
    /,
    **fields: object,
) -> None:
    """Emits one structured event. The only sanctioned log call shape.

    `message` must be a plain string literal -- no f-string, no `%`
    interpolation, no concatenation. Everything that varies goes in
    `**fields`, where `redaction.scrub_fields` rebuilds it from the
    registry. This is what makes the human-readable half of a line
    reviewable source text rather than a place data can hide, and it is the
    same guarantee `sturnus.infrastructure.observability.scrub_event`
    already relies on when it forwards `logentry.message` and nothing else
    to Sentry.

    `tests/test_logging_discipline.py` enforces both halves: the literal
    message, and every field name being registered.
    """
    logger.log(
        level,
        message,
        extra={"sturnus_event": str(event), "sturnus_fields": dict(fields)},
    )


def log_exception(
    logger: logging.Logger,
    level: int,
    event: Event,
    message: str,
    exc: BaseException,
    /,
    **fields: object,
) -> None:
    """`log_event` plus a stack trace, with `error_type` filled in.

    Note what is *not* here: the exception is never passed as a `%`
    argument. `log.warning("failed: %s", exc)` -- twelve of which existed
    before this package -- prints `str(exc)` verbatim, and a
    `jinja2.UndefinedError` raised while rendering a transcript through the
    Outline template carries template context in exactly that string. The
    type is a registered field, the traceback is rendered by
    `setup.SafeFormatterMixin` from static program text, and the message
    itself travels only if `redaction.SAFE_MESSAGE_TYPES` vouches for its
    class.
    """
    from sturnus.observability.redaction import error_type

    logger.log(
        level,
        message,
        exc_info=exc,
        extra={
            "sturnus_event": str(event),
            "sturnus_fields": {"error_type": error_type(exc), **fields},
        },
    )


class RateLimiter:
    """Lets the first occurrence through, then one in every `every`.

    For events that are per-packet in origin but must not be per-packet in
    Loki. `voice.packet_handler_failed` used to be a `log.error` on every
    failed packet: during a systematic failure -- which is the only time it
    matters -- that is its own flood, at ~50 lines per second per speaker.
    One line carrying `count` says strictly more and costs four orders of
    magnitude less.

    Not thread-safe by construction, and it does not need to be: the
    counter is a plain `int` increment, the GIL makes that atomic enough for
    a rate limiter, and the worst outcome of a lost increment is one line
    logged early.
    """

    def __init__(self, every: int = 1000) -> None:
        self._every = every
        self._count = 0

    def should_log(self) -> bool:
        self._count += 1
        return self._count == 1 or self._count % self._every == 0

    @property
    def count(self) -> int:
        return self._count

    def reset(self) -> None:
        self._count = 0


#: Level constants re-exported so a call site needs one import, not two.
DEBUG: Final = logging.DEBUG
INFO: Final = logging.INFO
WARNING: Final = logging.WARNING
ERROR: Final = logging.ERROR
