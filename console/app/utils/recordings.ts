/**
 * A session, its tracks, and how each of them is put into words.
 *
 * Separate from the components for the same reason the transport is: the
 * distinction this module exists to keep -- a measurement of zero against a
 * measurement that was never taken -- is a decision about honesty, and a
 * decision embedded in a template can only be checked by rendering one.
 */

/** Every id here is a string. A Discord snowflake exceeds JavaScript's safe
 *  integer range, where a JSON number silently loses its last digits and
 *  produces an id that looks right and names nobody. */
export interface SessionTrack {
  discord_user_id: string
  display_name: string | null
  /** Seconds of audio written for this speaker. `null` means never measured. */
  audio_seconds: number | null
  /** Seconds of it that were speech. `null` means never measured; `0` means
   *  measured, and this person did not say anything. */
  speech_seconds: number | null
  segment_count: number | null
}

export interface SessionParticipant {
  discord_user_id: string
  display_name: string
}

export interface RecordedSession {
  id: string
  started_at: string
  ended_at: string | null
  duration_seconds: number | null
  channel_id: string
  channel_name: string | null
  /** The Outline protocol, when one was written. */
  document_url: string | null
  /** Everybody else who was in the channel -- including the people who did
   *  not consent, who are therefore in this list and not in `tracks`. */
  other_participants: SessionParticipant[]
  tracks: SessionTrack[]
}

export interface SessionsResponse {
  sessions: RecordedSession[]
}

/**
 * What is shown where there is no answer.
 *
 * An em dash rather than "0", "n/a" or an empty cell: it reads as an
 * absence at a glance, lines up in a column of numbers, and cannot be
 * mistaken for a measurement.
 */
export const NOT_MEASURED = '—'

/** A length as a clock, with an hours field only when there are hours. */
export function formatSeconds(total: number): string {
  // Down, never to the nearest: a track of 59.9 seconds has not reached a
  // minute, and saying it has makes two numbers that should agree differ.
  const whole = Math.max(0, Math.floor(total))
  const hours = Math.floor(whole / 3600)
  const minutes = Math.floor((whole % 3600) / 60)
  const seconds = whole % 60
  const padded = `${String(seconds).padStart(2, '0')}`
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${padded}`
  return `${minutes}:${padded}`
}

/** A measurement, or the fact that none was taken. */
export function formatMeasurement(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return NOT_MEASURED
  return formatSeconds(seconds)
}

/** A count, or the fact that none was taken. */
export function formatCount(count: number | null | undefined): string {
  if (count === null || count === undefined) return NOT_MEASURED
  return String(count)
}

/**
 * How much of a track was speech, between 0 and 1, or `null` for unknown.
 *
 * Unknown covers three cases that all mean the same thing to a reader:
 * either measurement missing, and a track with no audio at all -- a share
 * of nothing is not a small share, it is no answer.
 */
export function speechShare(track: SessionTrack): number | null {
  const { audio_seconds: audio, speech_seconds: speech } = track
  if (audio === null || speech === null) return null
  if (audio <= 0) return null
  // The two numbers come from different stages of the pipeline -- one from
  // the padded track, one from what was actually transcribed -- so a bug
  // upstream could put speech above audio. A bar drawn past its own end is
  // a worse way to find that out than a bar at its end.
  return Math.min(1, Math.max(0, speech / audio))
}

export function formatShare(share: number | null): string {
  if (share === null) return NOT_MEASURED
  return `${Math.round(share * 100)}%`
}

/**
 * What to call a speaker.
 *
 * Somebody who has left the guild has no display name left to look up, and
 * a row with an empty name is a track nobody can attribute -- so the id,
 * which is ugly but true.
 */
export function trackLabel(track: SessionTrack): string {
  const name = track.display_name?.trim()
  return name ? name : track.discord_user_id
}

/** What to call the channel it happened in. */
export function channelLabel(session: RecordedSession): string {
  const name = session.channel_name?.trim()
  return name ? `#${name}` : `Channel ${session.channel_id}`
}

function instantOf(value: string | null): number | null {
  if (!value) return null
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? null : parsed
}

/**
 * How long the session ran, in seconds, or `null` while that is unknown.
 *
 * The API's own measurement first; the two timestamps as a fallback, since
 * an older row may predate the column. Written as an explicit null check
 * rather than `||`, because a meeting that ended the second it started has
 * a duration of zero and deserves to keep it.
 */
export function sessionLength(session: RecordedSession): number | null {
  if (session.duration_seconds !== null && session.duration_seconds !== undefined) {
    return session.duration_seconds
  }
  const started = instantOf(session.started_at)
  const ended = instantOf(session.ended_at)
  if (started === null || ended === null) return null
  return Math.max(0, Math.floor((ended - started) / 1000))
}

/** A session with no end is one that is still being recorded. */
export function isInProgress(session: RecordedSession): boolean {
  return session.ended_at === null
}

/** Whether a protocol was written. An empty link is not a document. */
export function hasProtocol(session: RecordedSession): boolean {
  return Boolean(session.document_url?.trim())
}

/**
 * An instant, written in a named zone.
 *
 * The zone is a parameter and not an ambient default on purpose. The
 * console renders on the server, where the process's zone has nothing to
 * do with the reader's; formatting with whatever `Intl` happens to resolve
 * would put one time in the server-rendered HTML and a different one in
 * the hydrated page, which Vue reports as a mismatch and a reader reports
 * as the console being wrong about when their meeting was. So the page
 * renders UTC first and switches to the viewer's zone after mounting.
 *
 * The shape is `YYYY-MM-DD HH:MM`, assembled from parts rather than taken
 * from a locale format: unambiguous in every country, sorts the way it
 * reads, and does not change with the ICU data the container happens to
 * ship.
 */
export function formatTimestamp(iso: string, timeZone: string): string {
  const instant = instantOf(iso)
  if (instant === null) return NOT_MEASURED
  const date = new Date(instant)
  try {
    return assemble(date, timeZone)
  } catch {
    // A zone the runtime will not accept. `resolvedOptions().timeZone` has
    // returned surprises before, and a page that throws while formatting a
    // date shows nothing at all.
    return assemble(date, 'UTC')
  }
}

function assemble(date: Date, timeZone: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date)
  const at = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? ''
  return `${at('year')}-${at('month')}-${at('day')} ${at('hour')}:${at('minute')}`
}

/**
 * Where one speaker's track is streamed from.
 *
 * The base is passed in rather than read from the runtime config here, and
 * it must be the *public* one: an `<audio>` element loads in a browser, so
 * the internal cluster address a server-side render would use addresses
 * nothing the listener can reach. Both ids are escaped -- they are strings
 * from an API, and a string allowed to contain a slash is a string allowed
 * to address a different endpoint.
 */
export function audioUrl(base: string, sessionId: string, discordUserId: string): string {
  const root = base.replace(/\/+$/, '')
  return `${root}/sessions/${encodeURIComponent(sessionId)}/tracks/${encodeURIComponent(discordUserId)}/audio`
}

/**
 * Where one speaker's spectrogram is fetched from.
 *
 * Same shape and same escaping as `audioUrl`, and behind the same
 * authorisation: a spectrogram is a rendering of somebody's voice and
 * shows when they spoke, so it is not public just because it is not
 * audible.
 */
export function spectrogramUrl(sessionId: string, discordUserId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/tracks/${encodeURIComponent(discordUserId)}/spectrogram`
}

/**
 * A track drawn as a picture: `bins` rows of `columns` cells, one byte
 * each, row 0 the lowest frequency.
 *
 * `hz_per_bin` is what labels the frequency axis. It is a number the API
 * derives rather than one this file assumes -- assuming an audio format
 * is what made every recording in this console play back at six times
 * speed, and the fix was to stop assuming anywhere.
 */
export interface SpectrogramResponse {
  columns: number
  bins: number
  sample_rate: number
  hz_per_bin: number
  duration_seconds: number
  /** Base64 of `bins * columns` bytes, row-major. */
  magnitudes: string
}

/** The matrix as bytes, or `null` if the payload is not the promised size. */
export function decodeMagnitudes(picture: SpectrogramResponse): Uint8Array | null {
  let binary: string
  try {
    binary = atob(picture.magnitudes)
  } catch {
    return null
  }
  const expected = picture.bins * picture.columns
  if (binary.length !== expected) return null
  const bytes = new Uint8Array(expected)
  for (let i = 0; i < expected; i += 1) bytes[i] = binary.charCodeAt(i)
  return bytes
}

/**
 * The canonical address of one recording.
 *
 * One recording, one URL — so a link in a protocol, a chat message or a
 * bookmark lands on the recording itself rather than on a list somebody
 * then has to search. Everything about that session hangs off this page.
 */
export function recordingPath(sessionId: string): string {
  return `/recordings/${encodeURIComponent(sessionId)}`
}

/**
 * Where a track's audio sits within the session's own timeline.
 *
 * A speaker's file begins at their first packet, not at the start of the
 * meeting, so two tracks of the same length can describe different
 * stretches of it. Returns `null` when there is nothing to place it
 * against, which is honest rather than a zero that reads as "from the
 * beginning".
 */
export function trackCoverage(
  session: RecordedSession,
  track: SessionTrack,
): { share: number } | null {
  const total = sessionLength(session)
  if (total === null || total <= 0) return null
  if (track.audio_seconds === null) return null
  return { share: Math.min(1, Math.max(0, track.audio_seconds / total)) }
}

/**
 * Where a session's transcription has got to, and whether it may be redone.
 *
 * Only an administrator of the session's guild ever sees this: re-running
 * a transcription spends worker time, clears transcripts and replaces a
 * document a team has already read. Everyone else's request answers 404,
 * which is also what "no such session" answers — the console does not
 * confirm the existence of meetings somebody has no business knowing
 * about.
 */
export interface QueueSpeaker {
  discord_user_id: string
  display_name: string | null
  /** `pending`, `running`, `done` or `dead`. */
  status: string
  attempts: number
  /** The last failure, already shortened, or `null` if nothing failed. */
  error: string | null
}

export interface QueueSnapshot {
  session_status: string
  document_url: string | null
  speakers: QueueSpeaker[]
  can_requeue: boolean
  /** Why not, when `can_requeue` is false. A button that greys out without
   *  saying why is a bug report waiting to be filed. */
  refusal: string | null
}

export interface RequeueOutcome {
  accepted: boolean
  requeued: string[]
  /** Speakers left alone because their audio is erased. Never folded into
   *  `requeued`: their old transcript is carried into the new document
   *  unchanged, and somebody told only the first number would reasonably
   *  assume the whole document had been regenerated. */
  skipped_erased: string[]
  refusal: string | null
}

export function queueStatusPath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/queue`
}

export function requeuePath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/queue/requeue`
}

/** Job statuses that mean a worker may still act on this session. */
const IN_FLIGHT = new Set(['pending', 'running'])

/**
 * Whether the queue is still moving, which is what decides whether the
 * progress view keeps polling.
 *
 * Read from the jobs rather than from `session_status`, because the
 * session flips to `documented` only after the document is written — so
 * polling that stopped at the last `done` would stop one step early and
 * never show the finished document.
 */
export function isQueueBusy(snapshot: QueueSnapshot): boolean {
  return snapshot.speakers.some((speaker) => IN_FLIGHT.has(speaker.status))
}

/** How far a re-queue has got, between 0 and 1. `null` with no jobs. */
export function queueProgress(snapshot: QueueSnapshot): number | null {
  if (snapshot.speakers.length === 0) return null
  const finished = snapshot.speakers.filter((speaker) => !IN_FLIGHT.has(speaker.status)).length
  return finished / snapshot.speakers.length
}

/** What to call a speaker in the queue view. */
export function queueSpeakerLabel(speaker: QueueSpeaker): string {
  const name = speaker.display_name?.trim()
  return name ? name : speaker.discord_user_id
}
