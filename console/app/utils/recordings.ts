/**
 * A session, its tracks, and how each of them is put into words.
 *
 * Separate from the components for the same reason the transport is: the
 * distinction this module exists to keep -- a measurement of zero against a
 * measurement that was never taken -- is a decision about honesty, and a
 * decision embedded in a template can only be checked by rendering one.
 *
 * Where something here is a sentence rather than a measurement it comes
 * back as a {@link Message} -- a key and its values -- and the template
 * turns it into words. `i18n/README.md` has the rule. The clock-shaped
 * numbers below are *not* sentences and stay strings: `12:34` is the same
 * in every language this console speaks, and assembling it by hand is what
 * keeps a track's length agreeing with the position under the playhead.
 */
import { NOT_MEASURED, type Message } from './message'

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
  /** The labels *this reader* put on the session, alphabetical. Never
   *  anybody else's: `session_tag` is keyed by its owner and the query
   *  that fills it names the signed-in person, so a meeting two people
   *  both tagged carries only the reader's own words. See
   *  `~/utils/tagging`. */
  tags: string[]
}

/**
 * One page of the recordings list.
 *
 * `total` is how many recordings this person has in all, not how many are
 * in `sessions` -- it is what lets the list say "1-20 of 47", and a list
 * that cannot say how much it is not showing is one people scroll to the
 * bottom of to find out.
 *
 * `limit` and `offset` are echoed back by the API so a slow response
 * arriving after a second click can be recognised as the wrong page
 * rather than rendered under the right page number.
 */
export interface SessionsResponse {
  sessions: RecordedSession[]
  total: number
  limit: number
  offset: number
}

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

/*
 * A count used to be rendered here too, as `String(count)` or an em dash.
 * Both halves have moved: `useSay` writes a bare number in the locale's
 * grouping and an absent one as the em dash, so a segment count reaches the
 * screen through the same path as every other figure. The function also
 * shared a name with `~/utils/format`'s, which Nuxt resolved by file order
 * and a build warning nobody was reading.
 */

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

/*
 * The share used to be rendered here as `45%`. It is a number, and German
 * writes it `45 %` -- with the space -- so it is now handed to the
 * `percent` number format in `i18n/i18n.config.ts` rather than glued to a
 * sign here. `speechShare` above already says everything this module has to
 * decide about it: how much, or that nobody can say.
 */

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

/**
 * What to call the channel it happened in.
 *
 * A name is somebody's own word for their channel and is not translated --
 * it comes back as the string it is. A channel with no name left has to be
 * called something, and "Channel" is a word: that half is a key.
 */
export function channelLabel(session: RecordedSession): string | Message {
  const name = session.channel_name?.trim()
  return name
    ? `#${name}`
    : { key: 'recordings.channelById', params: { id: session.channel_id } }
}

/**
 * The same question, answered for a row that has to be *scanned*.
 *
 * `channelLabel` answers "Channel 1240377558927872021" when the name has
 * gone, and that string is the complaint this exists for: eighteen digits
 * set as the heading of a row reads as the meeting's name. Nobody has a
 * meeting called that. A column of them is unscannable — the digits are
 * all the same shape, they are long enough to crowd out the date, and a
 * reader's eye stops at each one to check whether it means anything.
 *
 * So an unresolved channel is presented as **an absence, not a name**: the
 * heading says there is no name, in the muted role and in the same weight
 * as the rest of the row, and the id goes underneath as a subordinate
 * line. That is the treatment `UiSelect` already gives a snowflake through
 * its `detail` slot, for the same reason.
 *
 * The id is kept rather than dropped. It is the only handle anybody
 * debugging a channel that vanished from the guild actually has, and
 * hiding it would trade one person's confusion for another's.
 */
export interface ChannelNaming {
  /** Whether the guild's own word for the channel is still known. */
  named: boolean
  /** The row's heading. A name is somebody's own word and is not
   *  translated; the absence of one is a sentence and so is a key. */
  heading: string | Message
  /** The Discord id, to be set underneath — never as the heading, and
   *  `null` whenever the name says everything. */
  id: string | null
}

export function channelNaming(session: RecordedSession): ChannelNaming {
  const name = session.channel_name?.trim()
  if (name) return { named: true, heading: `#${name}`, id: null }
  return { named: false, heading: { key: 'recordings.channelUnnamed' }, id: session.channel_id }
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
 * How many speakers a row names before it starts counting them.
 *
 * Three. Two meetings in the same channel on the same afternoon are told
 * apart by who was in them, so the names have to be there; a row that
 * spelled out eleven of them would wrap to three lines and cost the list
 * the density this whole page is about. Three names is enough to
 * recognise a meeting and short enough to stay on one line at 360 px.
 */
export const NAMED_SPEAKERS = 3

/**
 * Who is on this recording, in as many words as a row can spare.
 *
 * Three shapes rather than one sentence with holes in it, because "0
 * speakers and 0 others" is not a sentence anybody wants read out — the
 * same reasoning `describeChips` gives.
 *
 * The names are the **recorded** speakers and not everybody who was in the
 * channel. This list indexes voices: the people with no audio are a fact
 * about one session rather than a way of telling two apart, and they are
 * on the recording's own page where there is room to say why they are not
 * here. The one case that stays is a session with no tracks at all, which
 * is a surprise worth meeting in the list rather than after a click.
 */
export function speakerSummary(
  session: RecordedSession,
  limit: number = NAMED_SPEAKERS,
): string | Message {
  const speakers = session.tracks.map(trackLabel)
  if (speakers.length === 0) return { key: 'recordings.rowNobody' }
  const named = speakers.slice(0, limit).join(', ')
  // Names are people's own, and a list of them is a list in both
  // languages -- so where they all fit, the answer is not a sentence and
  // has no key, exactly as `channelLabel` returns a channel's own name.
  if (speakers.length <= limit) return named
  return {
    key: 'recordings.rowSpeakersMore',
    params: { names: named, count: speakers.length - limit },
  }
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
  const { day, time } = stampParts(iso, timeZone)
  return time === '' ? day : `${day} ${time}`
}

/**
 * The same instant, with the day and the clock kept apart.
 *
 * A list sorted by date is scanned down its left edge, and what the eye is
 * looking for there is the *day*. Setting `2026-08-21 14:30` as one string
 * makes the reader parse eleven characters of it to find the two that
 * matter, in every row. Two lines — the day, and the time under it in the
 * muted role — puts a column of identically-shaped days under each other
 * and lets the clock recede to where it belongs.
 *
 * An unreadable instant is one em dash and no time at all, rather than an
 * em dash with a plausible clock beside it.
 */
export interface Stamp {
  day: string
  /** Empty when there is no instant to take one from. */
  time: string
}

export function stampParts(iso: string, timeZone: string): Stamp {
  const instant = instantOf(iso)
  if (instant === null) return { day: NOT_MEASURED, time: '' }
  const date = new Date(instant)
  try {
    return split(date, timeZone)
  } catch {
    // A zone the runtime will not accept. `resolvedOptions().timeZone` has
    // returned surprises before, and a page that throws while formatting a
    // date shows nothing at all.
    return split(date, 'UTC')
  }
}

function split(date: Date, timeZone: string): Stamp {
  const [day = NOT_MEASURED, time = ''] = assemble(date, timeZone).split(' ')
  return { day, time }
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

/** The same answer as `queueStatusPath`, sent again whenever it changes.
 *
 *  Derived from the polling path rather than written out beside it: a
 *  second literal is a second thing to forget when the first one moves.
 *  Named for the session rather than called `queueStreamPath`, because
 *  `~/utils/queue` has a guild-wide one of that name and both modules are
 *  auto-imported -- two exports sharing a name means one of them silently
 *  wins, and the loser is whichever file was added second. */
export function sessionQueueStreamPath(sessionId: string): string {
  return `${queueStatusPath(sessionId)}/stream`
}

/**
 * A snapshot that arrived over the live feed, or `null` if it did not.
 *
 * The polling path takes the endpoint's word for the shape, because a
 * failed parse there is an exception a caller can see. An event on a
 * stream has no caller: it arrives in a listener, and a payload that is
 * not what it claims to be would first be noticed as a render throwing
 * inside `speakers.length`. So the one field the panel dereferences is
 * checked, and anything else is discarded and the last good snapshot left
 * on screen.
 *
 * Deliberately not a full validation. Everything here was serialised by
 * the same function that serves the polling endpoint; the check exists to
 * keep a malformed frame from taking the page down, not to second-guess
 * the API.
 */
export function asQueueSnapshot(payload: unknown): QueueSnapshot | null {
  if (typeof payload !== 'object' || payload === null) return null
  const candidate = payload as Partial<QueueSnapshot>
  if (!Array.isArray(candidate.speakers)) return null
  return candidate as QueueSnapshot
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


/**
 * A job status in words rather than as the enum the table stores.
 *
 * A speaker's row reading "dead" is a database value shown to a human. It
 * means the job gave up after exhausting its retries, which is a sentence
 * somebody can act on; the enum is one they have to be told the meaning
 * of. Unknown values pass through unchanged rather than becoming
 * "unknown": a status this console has not been taught yet is still more
 * use to whoever is debugging than a word that hides it.
 */
const QUEUE_STATUS_WORDS: Record<string, string> = {
  pending: 'waiting',
  running: 'transcribing',
  done: 'finished',
  dead: 'gave up',
}

export function queueStatusWords(status: string): string {
  return QUEUE_STATUS_WORDS[status] ?? status
}

/** How far the arrow keys move a track's playhead, and how far the page
 *  keys move it. Five seconds is about one sentence, which is the unit
 *  somebody scrubbing a meeting is looking for; thirty is about one
 *  exchange. */
export const SEEK_NUDGE_SECONDS = 5
export const SEEK_STRIDE_SECONDS = 30

/**
 * Where a key press moves the playhead, or `null` for a key this is not
 * about.
 *
 * `null` rather than the unchanged position, because the caller has to
 * know whether to call `preventDefault` — swallowing Tab or a browser
 * shortcut to save writing out the list of handled keys is how a control
 * becomes a trap.
 *
 * The five keys every slider on the web answers to, so that nobody has to
 * discover this one. Clamped at both ends: a seek past the end of a track
 * is a player that stops, and a negative one throws in some browsers.
 */
export function seekTarget(key: string, at: number, duration: number): number | null {
  if (duration <= 0) return null
  if (key === 'Home') return 0
  if (key === 'End') return duration
  const moves: Record<string, number> = {
    ArrowLeft: -SEEK_NUDGE_SECONDS,
    ArrowRight: SEEK_NUDGE_SECONDS,
    PageDown: -SEEK_STRIDE_SECONDS,
    PageUp: SEEK_STRIDE_SECONDS,
  }
  const step = moves[key]
  if (step === undefined) return null
  return Math.min(duration, Math.max(0, at + step))
}
