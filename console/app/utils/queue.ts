/**
 * Where a guild's transcription work stands, and which of it a person has
 * to do something about.
 *
 * A module rather than expressions in the page, for the same reason
 * `~/utils/consents` is one: every function here is a *decision* -- which
 * row comes first, whether a row is waiting or stuck, what a caveat reads
 * like as a sentence, whether an empty page is good news -- and a decision
 * embedded in a template can only be tested by rendering one.
 *
 * Four facts govern the wording of everything below, and none of them are
 * softened anywhere in this file. Each of them is a way this page could
 * quietly lie to somebody who trusted it:
 *
 * - **The four lifecycle counts are guild-wide and cover all time.** They
 *   are not the sum of the sessions listed underneath them, and a reader
 *   who adds the rows up and gets a different number must find the reason
 *   on the page rather than conclude the page is broken.
 * - **`running_past_lease` is measured against a lease this process cannot
 *   see.** The API assumes `lease_seconds`; the lease that actually
 *   applies is the *worker's* `job_lease_seconds`, in the worker's own
 *   environment. A job running under a raised lease is perfectly healthy
 *   and is still counted here, so the figure is always presented with the
 *   number it was measured against -- never as a fact about dead workers.
 * - **The oldest pending figure is dated by a session's end, not by a
 *   job.** `transcription_job` has no enqueue timestamp at all. A session
 *   ends within seconds of its jobs being created, which is close enough
 *   to answer "has something been sitting here for hours?", and it is not
 *   close at all after a re-queue: a reset job keeps its session's
 *   original end and therefore reads older than it is.
 * - **The list is cut.** A page showing twenty sessions reads as "there
 *   are twenty" unless it says otherwise, and the one thing an
 *   administrator wants from a backlog page is its size.
 *
 * Nothing here decides whether a re-queue is allowed. That decision lives
 * on the recording page, once, in `~/utils/recordings` and the panel that
 * uses it -- a second implementation of "when is a redo safe" is a second
 * answer to it, and the two would drift.
 */
import { formatDuration } from '~/utils/duration'
import { formatCount, formatMoment } from '~/utils/format'

/* -------------------------------------------------------------------- */
/* What the API describes                                                */
/* -------------------------------------------------------------------- */

/** The transcription lifecycle, in the order a job moves through it. Kept
 *  as one shape rather than four sibling fields because that order is the
 *  point: it is how a reader finds where work is piling up. */
export interface QueueCounts {
  pending: number
  running: number
  done: number
  dead: number
}

/**
 * One session the pipeline has not finished with.
 *
 * "Unfinished" is the API's definition and is wider than it sounds: a
 * session counts as unfinished when it is not `documented` **or** when it
 * carries a `dead` job. A session can reach `documented` with a
 * permanently failed speaker inside it, and that speaker is exactly who
 * somebody needs to notice.
 */
export interface QueuedSession {
  /** A string, always. Session ids are database integers today and follow
   *  the snowflake rule anyway: two id shapes in one payload is how the
   *  one that matters gets parsed with the wrong one. */
  id: string
  channel_id: string
  /** Null when Sturnus has no name for the channel -- usually one deleted
   *  since the meeting. */
  channel_name: string | null
  started_at: string
  /** Null while the session is still being recorded. */
  ended_at: string | null
  /** `open`, `closed` or `documented`. Deliberately a plain string: a
   *  fourth value added to the API must render as itself rather than
   *  silently as the friendliest of the three. */
  status: string
  document_url: string | null
  counts: QueueCounts
}

export interface GuildQueue {
  /** Null only when the payload named no guild, which the page uses to
   *  refuse to show one server's queue under another's heading. */
  guild_id: string | null
  /** Guild-wide, across all time. See the module comment. */
  counts: QueueCounts
  running_past_lease: number
  /** ISO instant, or null when nothing is pending at all. */
  oldest_pending_session_ended_at: string | null
  closed_undocumented: number
  /** The lease `running_past_lease` was measured against, in seconds. */
  lease_seconds: number
  truncated: boolean
  sessions: QueuedSession[]
}

/* -------------------------------------------------------------------- */
/* Reading what the API sent                                             */
/* -------------------------------------------------------------------- */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** `null` stays `null`; anything else becomes the string it prints as. Ids
 *  are strings on the wire, and a number that arrived instead has already
 *  lost whatever precision it was going to lose. */
function asText(value: unknown): string | null {
  if (value === null || value === undefined) return null
  const text = typeof value === 'string' ? value : String(value)
  return text.trim() === '' ? null : text
}

/**
 * A count that can be printed.
 *
 * Anything absent, negative or not a number is a defect upstream, and
 * rendering it as "-3 pending" would put the defect in front of the reader
 * as though it were a fact about their server. Zero is the honest floor:
 * it says "none", which is as far as this console can vouch.
 */
function asCount(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 0
  return Math.round(value)
}

function asCounts(value: unknown): QueueCounts {
  const raw = isRecord(value) ? value : {}
  return {
    pending: asCount(raw.pending),
    running: asCount(raw.running),
    done: asCount(raw.done),
    dead: asCount(raw.dead),
  }
}

/**
 * The lease the past-lease figure was measured against.
 *
 * Zero and nonsense both become `null` rather than `0`, because the whole
 * job of this number on screen is to name what the count means. "past the
 * 0-second lease" would name nothing and would read as though every
 * running job were overdue, which is the opposite of what it says.
 */
function asLease(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null
  return value
}

/**
 * The queue in a payload.
 *
 * Always yields a well-formed value, never null. A page that has to
 * distinguish "the API refused" from "the API answered something odd"
 * already has the thrown `ApiError` for the first; a parser that returned
 * null for the second would turn a strange payload into a blank page with
 * no error anywhere, which is the failure mode hardest to report.
 */
export function parseGuildQueue(payload: unknown): GuildQueue {
  const raw = isRecord(payload) ? payload : {}
  const sessions = Array.isArray(raw.sessions) ? raw.sessions : []
  return {
    guild_id: asText(raw.guild_id),
    counts: asCounts(raw.counts),
    running_past_lease: asCount(raw.running_past_lease),
    oldest_pending_session_ended_at: asText(raw.oldest_pending_session_ended_at),
    closed_undocumented: asCount(raw.closed_undocumented),
    lease_seconds: asLease(raw.lease_seconds) ?? 0,
    // `=== true` rather than truthiness: a missing flag must read as "the
    // list is whole". Erring the other way would put a warning about a
    // hidden backlog on every complete page, and a warning that is always
    // there is one nobody reads on the day it is true.
    truncated: raw.truncated === true,
    sessions: sessions.flatMap((entry) => {
      if (!isRecord(entry)) return []
      const id = asText(entry.id)
      // A session with no id has no recording page to link to, and the
      // link is the only thing this page offers per row.
      if (!id) return []
      return [
        {
          id,
          channel_id: asText(entry.channel_id) ?? '',
          channel_name: asText(entry.channel_name),
          started_at: asText(entry.started_at) ?? '',
          ended_at: asText(entry.ended_at),
          status: asText(entry.status) ?? '',
          document_url: asText(entry.document_url),
          counts: asCounts(entry.counts),
        },
      ]
    }),
  }
}

/** Where a guild's queue is read from. The id is escaped: it is a string
 *  from an API, and a string allowed to contain a slash is a string
 *  allowed to address a different endpoint. */
export function queuePath(guildId: string): string {
  return `/guilds/${encodeURIComponent(guildId)}/queue`
}

/* -------------------------------------------------------------------- */
/* Naming a session                                                      */
/* -------------------------------------------------------------------- */

/** What to call the channel a session happened in. The whole id when there
 *  is no name -- never a shortened one, since snowflakes minted in the
 *  same era share their leading digits, and a truncated id is something
 *  nobody can search a page for either. */
export function queueChannelLabel(session: QueuedSession): string {
  const name = session.channel_name?.trim()
  if (name) return `#${name}`
  return session.channel_id ? `Channel ${session.channel_id}` : 'An unnamed channel'
}

/**
 * The line under a row whose channel has no name, or `null` when it has
 * one.
 *
 * A bare snowflake where every other row carries a `#name` reads as a
 * fault in the console. It is not one: the name is looked up in Discord,
 * and a channel deleted since the meeting has none left to look up.
 * Saying so is the difference between "this row is broken" and "the
 * channel this meeting happened in is gone".
 */
export function queueChannelNote(session: QueuedSession): string | null {
  if (session.channel_name?.trim()) return null
  return (
    'Sturnus has no name for this channel. Channel names are read from Discord, so a channel '
    + 'deleted since the meeting leaves only its id behind — the recording itself is unaffected.'
  )
}

/** When the recording started, always in UTC and saying so: the server
 *  render cannot know the reader's zone, and a second rendering in the
 *  browser would disagree with the first. */
export function sessionStartLine(session: QueuedSession): string {
  if (!session.started_at) return 'When this session started was not recorded.'
  return `Started ${formatMoment(session.started_at)}.`
}

/* -------------------------------------------------------------------- */
/* What state a session is in                                            */
/* -------------------------------------------------------------------- */

/**
 * Three tones, and they are about the reader rather than about the data.
 *
 * `alarm` means nothing will change here until a person does something.
 * `watch` means the pipeline has it and the only useful act is to wait.
 * `clear` means there is nothing to do and no bad news either. Rendering
 * "a speaker failed for good" and "a worker is on it" in the same colour
 * would hide the one distinction this page exists to draw.
 */
export type QueueTone = 'clear' | 'watch' | 'alarm'

/**
 * What kind of row this is.
 *
 * - `needs-person` — nothing queued will move it on. A dead job, or a
 *   closed session with nothing running and no document.
 * - `moving` — a job is pending or running. A worker will get to it.
 * - `recording` — the meeting is happening right now. It has no jobs yet
 *   because they are created when the recording ends, so its row of zeros
 *   is not an absence of work but the absence of a reason for work.
 * - `finishing` — every job is done and a document exists; only the
 *   session's own status has not caught up.
 */
export type QueueAttention = 'needs-person' | 'moving' | 'recording' | 'finishing'

/**
 * The lifecycle, once, in the order a job moves through it.
 *
 * One list rather than four field names written out at each call site, so
 * a session row and the summary band can never end up ordering the same
 * four counts differently -- which is the one thing that would make the
 * two halves of this page disagree about what it is showing.
 */
const LIFECYCLE: readonly { key: keyof QueueCounts, label: string }[] = [
  { key: 'pending', label: 'Pending' },
  { key: 'running', label: 'Running' },
  { key: 'done', label: 'Done' },
  { key: 'dead', label: 'Dead' },
]

/** Jobs a worker may still act on. `done` and `dead` are both terminal;
 *  only one of them is good news, which is why they are never counted
 *  together anywhere in this file. */
function inFlight(counts: QueueCounts): number {
  return counts.pending + counts.running
}

function totalJobs(counts: QueueCounts): number {
  return counts.pending + counts.running + counts.done + counts.dead
}

/**
 * Which of the four kinds of row this is.
 *
 * The order of the tests is the point. A dead job outranks everything,
 * including a document: a session that reached `documented` with a
 * permanently failed speaker looks finished from every other angle, and
 * the only place anybody will find out is here.
 *
 * `ended_at` rather than `status === 'open'` decides whether a session is
 * live, because that is the fact rather than a label for it -- and a
 * status string this console does not recognise must not turn a running
 * meeting into an unexplained row of zeros.
 */
export function queueAttention(session: QueuedSession): QueueAttention {
  if (session.counts.dead > 0) return 'needs-person'
  if (inFlight(session.counts) > 0) return 'moving'
  if (!session.ended_at) return 'recording'
  if (!session.document_url) return 'needs-person'
  return 'finishing'
}

export interface SessionState {
  tone: QueueTone
  label: string
  /** The long form, said in full on the row rather than hidden behind a
   *  tooltip: which of these four a row is decides what to do next, and
   *  nobody hovers to find that out. */
  detail: string
}

function jobs(count: number): string {
  return count === 1 ? '1 job' : `${formatCount(count)} jobs`
}

/**
 * What this row's state is, in a badge and a sentence.
 *
 * Every sentence says what will happen next without anybody doing
 * anything, because that is the question a queue page is read to answer.
 * Where the answer is "nothing", it says so in those words.
 */
export function queueSessionState(session: QueuedSession): SessionState {
  const { counts } = session
  if (counts.dead > 0) {
    const failed
      = counts.dead === 1
        ? 'One speaker in this session failed for the last time'
        : `${formatCount(counts.dead)} speakers in this session failed for the last time`
    const document = session.document_url
      ? ' A protocol was written anyway, without them, so nothing about this session looks wrong '
        + 'until somebody reads it and finds a voice missing.'
      : ''
    return {
      tone: 'alarm',
      label:
        counts.dead === 1
          ? '1 speaker failed for good'
          : `${formatCount(counts.dead)} speakers failed for good`,
      detail:
        `${failed} and will not be retried on their own.${document} Open the recording to see `
        + 'which speaker and why; re-queueing it there is the only thing that starts them again.',
    }
  }

  if (inFlight(counts) > 0) {
    if (counts.running > 0) {
      const waiting
        = counts.pending > 0 ? ` ${jobs(counts.pending)} behind it are still waiting.` : ''
      return {
        tone: 'watch',
        label: 'Being transcribed',
        detail:
          `A worker has ${jobs(counts.running)} from this session in hand right now.${waiting} `
          + 'Nothing needs doing unless the figures stop changing.',
      }
    }
    return {
      tone: 'watch',
      label: 'Waiting for a worker',
      detail:
        `${jobs(counts.pending)} queued and none running. They start as soon as a worker is free; `
        + 'if this never changes, no worker is taking work at all.',
    }
  }

  if (!session.ended_at) {
    return {
      tone: 'clear',
      label: 'Recording now',
      detail:
        'This meeting is being recorded at this moment. It has no transcription jobs yet — they '
        + 'are created when the recording ends — so the zeros beside it mean there is nothing to '
        + 'do, not that something went missing.',
    }
  }

  if (!session.document_url) {
    if (totalJobs(counts) === 0) {
      return {
        tone: 'alarm',
        label: 'Nothing was ever queued',
        detail:
          'The recording finished and no transcription job was ever created for it: nobody in the '
          + 'channel had consented, or the recording captured no audio. No worker has anything to '
          + 'do here and no protocol will appear on its own.',
      }
    }
    return {
      tone: 'alarm',
      label: 'Closed with nothing queued',
      detail:
        `The recording finished, all ${jobs(totalJobs(counts))} came back done, and no protocol `
        + 'was written. Nothing is queued for this session, so nothing about it will change on its '
        + 'own — somebody has to re-queue it from the recording.',
    }
  }

  return {
    tone: 'clear',
    label: 'Waiting to be marked done',
    detail:
      'Every job finished and the protocol exists. Only the session\'s own status has not caught '
      + 'up yet, which it does on its own; there is nothing to do here.',
  }
}

/** The four counts of one row, in lifecycle order and already worded. The
 *  page loops this rather than naming the four fields itself, so a row and
 *  the summary band can never end up ordering them differently. */
export function sessionCounts(session: QueuedSession): { key: string, label: string, value: string }[] {
  return LIFECYCLE.map(({ key, label }) => ({
    key,
    label,
    value: formatCount(session.counts[key]),
  }))
}

/* -------------------------------------------------------------------- */
/* The order the sessions are listed in                                  */
/* -------------------------------------------------------------------- */

/**
 * Rank by what the reader can still do about a row.
 *
 * 0 -- needs a person: nothing queued will move it on, and no amount of
 *      waiting changes that. These are the only rows whose position on the
 *      page decides whether they are seen at all.
 * 1 -- moving: a worker has it or will. Second because this is what
 *      somebody watching a backlog drain came to watch.
 * 2 -- recording now: nothing to do, and worth seeing — it is the reason a
 *      channel that ought to be producing work is not.
 * 3 -- finishing: done in every way that matters, waiting on a flag.
 */
function attentionRank(session: QueuedSession): number {
  switch (queueAttention(session)) {
    case 'needs-person':
      return 0
    case 'moving':
      return 1
    case 'recording':
      return 2
    default:
      return 3
  }
}

/** An instant as a number, or `null` when there is nothing to compare. */
function instantOf(value: string | null): number | null {
  if (!value) return null
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? null : parsed
}

/**
 * Session ids in numeric order without ever becoming numbers.
 *
 * Shorter is smaller, and equal lengths compare digit by digit. Comparing
 * them as plain strings would put "1000" after "999", and comparing them
 * as numbers would round any id past the safe integer range into a
 * different id -- which session ids do not reach today and channel ids do,
 * so the same rule is used for both rather than two rules that differ by
 * which field they are applied to.
 */
function compareIds(a: string, b: string): number {
  if (a.length !== b.length) return a.length - b.length
  if (a === b) return 0
  return a < b ? -1 : 1
}

/**
 * The order the rows are listed in.
 *
 * Rows needing a person first (see `attentionRank`), then newest first
 * inside each rank, then by id.
 *
 * Newest first *within every rank*, including the stuck ones, and
 * deliberately not oldest-first for those. It is tempting to surface the
 * longest-rotting session at the very top, and it would be the wrong page
 * to do it on: somebody arrives here because a team has just said "our
 * meeting from this morning has no protocol", and they scan for the
 * meeting they were told about. A list that counts backwards in one rank
 * and forwards in another cannot be scanned at all. How long the backlog
 * has been there is a question the oldest-pending figure in the summary
 * band answers, in one line, without reordering anything.
 *
 * A session whose `started_at` could not be parsed sorts to the end of its
 * rank rather than to an arbitrary place in the middle -- it is still a
 * row worth seeing, and it has no claim to a position among the ones that
 * carry a real time.
 *
 * Every comparison ends at the id, which is unique, so the order is total:
 * two sessions recorded in parallel channels never swap places between
 * renders.
 */
export function orderQueueSessions(sessions: readonly QueuedSession[]): QueuedSession[] {
  return [...sessions].sort((a, b) => {
    const byRank = attentionRank(a) - attentionRank(b)
    if (byRank !== 0) return byRank

    const left = instantOf(a.started_at)
    const right = instantOf(b.started_at)
    if (left !== null && right !== null) {
      if (left !== right) return right - left
    } else if (left !== null || right !== null) {
      return left !== null ? -1 : 1
    }

    // Higher id last-created, so newest first here too, which keeps the
    // tiebreak pointing the same way as the comparison above it.
    return compareIds(b.id, a.id)
  })
}

/** How many rows nothing will move without a person. The headline figure
 *  for the list: twelve unfinished sessions where three are stuck says
 *  something a bare row count does not. */
export function needsPersonCount(sessions: readonly QueuedSession[]): number {
  return sessions.filter((session) => queueAttention(session) === 'needs-person').length
}

/** The line above the list, naming what it holds and how much of it is
 *  actually somebody's problem. */
export function sessionsSummaryLine(sessions: readonly QueuedSession[]): string {
  const total = sessions.length
  if (total === 0) return 'No unfinished sessions are listed for this server.'
  const stuck = needsPersonCount(sessions)
  const noun = total === 1 ? 'session' : 'sessions'
  if (stuck === 0) {
    return (
      `${formatCount(total)} unfinished ${noun} here, and none of them is waiting on a person — `
      + 'every one is either being worked on or still being recorded.'
    )
  }
  const verb = stuck === 1 ? 'needs' : 'need'
  return (
    `${formatCount(total)} unfinished ${noun} here; ${formatCount(stuck)} of them ${verb} `
    + 'somebody, because nothing queued will move them on. Those are listed first.'
  )
}

/* -------------------------------------------------------------------- */
/* The four lifecycle counts                                             */
/* -------------------------------------------------------------------- */

export interface LifecycleFigure {
  key: string
  label: string
  value: string
  /** What this stage means, in the reader's terms. */
  note: string
  tone: QueueTone
}

/**
 * What the four counts are counting.
 *
 * Stated once, above the figures, and not left to be inferred. These are
 * guild-wide totals over all time; the sessions below are a list of the
 * unfinished ones only. A reader who adds up the rows and gets a different
 * number has found the difference between the two, not a fault, and the
 * page has to be the thing that tells them so.
 */
export const LIFECYCLE_SCOPE_NOTE =
  'These four count every transcription job this server has ever had, in the order a job moves '
  + 'through them: pending, then running, then done — or dead, once it has failed for the last '
  + 'time. They are totals for the whole server across all time, not a sum of the sessions listed '
  + 'below, so the two will not add up and are not meant to.'

/**
 * The four counts, in lifecycle order, with what each stage means.
 *
 * `dead` is the only one whose note changes with its value, because it is
 * the only one where zero is worth saying out loud: "nothing has failed
 * for good" is news, and an unexplained 0 next to three other numbers is
 * not.
 */
export function lifecycleFigures(queue: GuildQueue): LifecycleFigure[] {
  const notes: Record<keyof QueueCounts, string> = {
    pending: 'Queued, waiting for a worker to pick them up.',
    running: 'A worker has these in hand right now.',
    done: 'Transcribed successfully, counted for as long as the job row exists.',
    dead:
      queue.counts.dead > 0
        ? 'Failed for the last time. These are not retried on their own; each one is a speaker '
          + 'missing from a protocol until somebody re-queues their session.'
        : 'Nothing in this server has failed for good.',
  }
  return LIFECYCLE.map(({ key, label }) => ({
    key,
    label,
    value: formatCount(queue.counts[key]),
    note: notes[key],
    tone: key === 'dead' && queue.counts.dead > 0 ? 'alarm' : 'clear',
  }))
}

/* -------------------------------------------------------------------- */
/* The three figures somebody has to act on                              */
/* -------------------------------------------------------------------- */

/** The assumed lease, written the way the Discord `/queue status` reply
 *  writes it, so the two readouts of the same number agree. */
function leaseInWords(queue: GuildQueue): string {
  if (queue.lease_seconds <= 0) return 'the lease the API assumed'
  return `the ${Math.round(queue.lease_seconds)}-second lease`
}

/**
 * How many running jobs have outlived their lease, and what that is worth.
 *
 * The caveat is in both branches, not only the alarming one. The count is
 * derived from an *assumed* lease: the API process cannot see the worker's
 * `job_lease_seconds`, so a raised lease makes a healthy job look overdue
 * and a lowered one hides an overdue job entirely. A zero reported without
 * the caveat would read as "no worker has died", which this figure cannot
 * establish.
 */
export function pastLeaseLine(queue: GuildQueue): string {
  const lease = leaseInWords(queue)
  const caveat =
    `The lease that actually applies is the worker's own job_lease_seconds, which the API process `
    + 'cannot see, so this is measured against the lease it assumed rather than the real one.'
  if (queue.running_past_lease === 0) {
    return (
      `No running job has been held longer than ${lease}. ${caveat} A worker running under a `
      + 'raised lease would still be counted here, so a zero is reassuring rather than conclusive.'
    )
  }
  const held
    = queue.running_past_lease === 1
      ? 'One running job has been held'
      : `${formatCount(queue.running_past_lease)} running jobs have been held`
  return (
    `${held} longer than ${lease}. ${caveat} If the worker's lease is not higher than that, the `
    + 'worker holding these died and another may already have reclaimed the job — no amount of '
    + 'waiting fixes that, which is why this is the figure to read first.'
  )
}

/**
 * Sessions that are closed, have nothing queued and still have no
 * document.
 *
 * Nothing is pending for them, nothing is running for them, and nothing
 * will start on its own. Kept separate from the lifecycle counts because
 * it is not a count of jobs at all -- it is a count of meetings whose
 * protocol nobody is going to get unless somebody asks for it.
 */
export function undocumentedLine(queue: GuildQueue): string {
  if (queue.closed_undocumented === 0) {
    return (
      'Every closed session in this server either has its protocol or still has work queued for '
      + 'it. Nothing is sitting finished and unwritten.'
    )
  }
  const sessions
    = queue.closed_undocumented === 1
      ? 'One closed session has'
      : `${formatCount(queue.closed_undocumented)} closed sessions have`
  return (
    `${sessions} no unfinished jobs left and still no protocol. Nothing is queued for them and `
    + 'nothing will start on its own, so each one waits for a person to open its recording and '
    + 'ask for the transcription again.'
  )
}

/** An age in words, or `null` when there is nothing to measure it
 *  against. `now` is a parameter rather than a call to `Date.now()` inside
 *  this module for two reasons: a pure function is testable, and the page
 *  only has a clock after it has mounted -- a server render and a browser
 *  render a second apart would otherwise disagree about the text of the
 *  same paragraph, which Vue reports as a hydration mismatch. */
function ageInWords(iso: string, now: number | null): string | null {
  if (now === null) return null
  const at = instantOf(iso)
  if (at === null) return null
  const seconds = Math.floor((now - at) / 1000)
  if (seconds < 0) return null
  return formatDuration(seconds)
}

/**
 * How long the oldest waiting job has been waiting -- with the two things
 * that figure is not.
 *
 * It is not a job timestamp: `transcription_job` records no enqueue time
 * at all, so this is dated by the *session's* end, which is within seconds
 * of when its jobs were created. And it is not the age of a re-queued job:
 * a reset job keeps its session's original end and therefore reads older
 * than it is. Both are said every time the figure is shown, because a
 * number labelled "oldest pending" that is quietly a different number is
 * worse than no number.
 */
export function oldestPendingLine(queue: GuildQueue, now: number | null): string {
  const ended = queue.oldest_pending_session_ended_at
  if (!ended) {
    return 'Nothing is waiting: this server has no job in pending at all.'
  }
  const age = ageInWords(ended, now)
  const since = age === null ? '' : ` — ${age} ago`
  return (
    `The oldest job still waiting belongs to a session that ended ${formatMoment(ended)}${since}. `
    + `This is dated by the session's end rather than by the job: transcription_job records no `
    + 'enqueue time at all, and a session ends within seconds of its jobs being created. A '
    + "re-queued job keeps its session's original end, so after a re-queue this reads older than "
    + 'the job itself.'
  )
}

export interface AttentionItem {
  key: string
  label: string
  /** The figure itself, short enough to be scanned in a band of three. */
  value: string
  /** The sentence, including the caveat that makes the figure honest. */
  detail: string
  tone: QueueTone
}

/**
 * The three figures that mean somebody has to do something, kept apart
 * from the four that merely describe the pipeline.
 *
 * They are shown whether or not they are zero. A row that appears only
 * when it is bad news is a row whose absence has to be interpreted, and
 * "there is no warning about dead workers" and "this page does not warn
 * about dead workers" look identical on screen.
 */
export function attentionItems(queue: GuildQueue, now: number | null): AttentionItem[] {
  return [
    {
      key: 'past-lease',
      label: 'Running past their lease',
      value: formatCount(queue.running_past_lease),
      detail: pastLeaseLine(queue),
      tone: queue.running_past_lease > 0 ? 'alarm' : 'clear',
    },
    {
      key: 'closed-undocumented',
      label: 'Closed with no protocol',
      value: formatCount(queue.closed_undocumented),
      detail: undocumentedLine(queue),
      tone: queue.closed_undocumented > 0 ? 'alarm' : 'clear',
    },
    {
      key: 'oldest-pending',
      label: 'Oldest job waiting',
      value: queue.oldest_pending_session_ended_at
        ? formatMoment(queue.oldest_pending_session_ended_at)
        : 'Nothing waiting',
      detail: oldestPendingLine(queue, now),
      tone: queue.oldest_pending_session_ended_at ? 'watch' : 'clear',
    },
  ]
}

/* -------------------------------------------------------------------- */
/* What the list does not show                                           */
/* -------------------------------------------------------------------- */

/**
 * The notice above a list that was cut short, or `null` when it was not.
 *
 * The number of rows is read off the list rather than written into the
 * sentence, because the server's limit is the server's to change and a
 * sentence naming a number the API no longer uses is a sentence that lies
 * without anybody editing it.
 */
export function truncationNotice(queue: GuildQueue): string | null {
  if (!queue.truncated) return null
  const shown = queue.sessions.length
  const listed
    = shown === 1
      ? 'Only one unfinished session is listed'
      : `Only the newest ${formatCount(shown)} unfinished sessions are listed`
  return (
    `${listed}; this server has more. Sturnus cuts the list rather than sending an unbounded one, `
    + 'so what is below is a window on the backlog and not its size. The four job counts above '
    + 'are guild-wide and do count all of it.'
  )
}

/* -------------------------------------------------------------------- */
/* Whether anything is happening at all                                  */
/* -------------------------------------------------------------------- */

/**
 * Whether work is actually moving, which is what decides whether the page
 * keeps polling.
 *
 * Read from the guild-wide `pending` and `running` rather than from the
 * listed sessions, because the list is cut and the counts are not: a guild
 * with a hundred pending jobs and twenty listed sessions is still moving
 * even if every listed row happens to be finished.
 *
 * `dead` deliberately does not count. A dead job never changes again on
 * its own, and polling for it would be a page that reloads for ever
 * waiting for news that cannot arrive.
 */
export function isQueueMoving(queue: GuildQueue): boolean {
  return inFlight(queue.counts) > 0
}

/**
 * Whether there is genuinely nothing outstanding in this server.
 *
 * Stricter than "the list is empty". A guild can have no unfinished
 * sessions listed and still have a dead worker holding a lease or a closed
 * meeting with no protocol, and reporting that as "all clear" is exactly
 * the reassurance nobody should be given. Historical `done` and `dead`
 * counts are not consulted: they describe what has happened, not what is
 * outstanding, and a server that once had a failure is not permanently
 * unwell.
 */
export function isQueueClear(queue: GuildQueue): boolean {
  return (
    queue.sessions.length === 0
    && !queue.truncated
    && inFlight(queue.counts) === 0
    && queue.running_past_lease === 0
    && queue.closed_undocumented === 0
    && queue.oldest_pending_session_ended_at === null
  )
}

/** The empty state, written as the good news it is. A queue page with
 *  nothing on it is the state everybody wants and the state that looks
 *  most like a broken page, so it says which of the two it is. */
export const CLEAR_QUEUE_HEADING = 'Nothing is outstanding in this server'

export const CLEAR_QUEUE_NOTE =
  'Every session has been transcribed and written up, no job is waiting or running, no worker is '
  + 'holding one past its lease, and no closed meeting is missing its protocol. There is nothing '
  + 'to do here — this page is worth coming back to when somebody says a protocol has not '
  + 'appeared.'

/* -------------------------------------------------------------------- */
/* When the API says no                                                  */
/* -------------------------------------------------------------------- */

/** `ApiError` names it `status`; a raw `$fetch` failure may name it
 *  `statusCode`; a request that never got a response has neither, and null
 *  says so rather than standing in a number that would read as an
 *  answer. */
function statusOf(error: unknown): number | null {
  if (!isRecord(error)) return null
  for (const candidate of [error.status, error.statusCode]) {
    if (typeof candidate === 'number' && Number.isFinite(candidate)) {
      // `ApiError` uses 0 for "never reached the API", which is
      // deliberately distinguishable from every real status.
      return candidate === 0 ? null : candidate
    }
  }
  return null
}

/**
 * A failed request, in a sentence somebody can act on.
 *
 * Built from the status alone. `useApi` throws `ApiError`, which carries
 * no body by design -- the API's own `{"error": "no such guild"}` never
 * reaches this console -- so every sentence below has to stand on its own
 * without it.
 *
 * Named `describeQueueError` rather than `describeError` for the same
 * reason `describeConsentError` is: everything under `app/utils` is
 * auto-imported into every component, and two exports sharing a name is a
 * build warning and a coin toss over which one a page actually gets.
 */
export function describeQueueError(error: unknown): string {
  const status = statusOf(error)
  switch (status) {
    case 401:
      return 'Your session has ended. Sign in again to see this server’s queue.'
    case 403:
      return (
        'You do not administer this server. Administrators are the members holding the role named '
        + 'by that guild’s `admin_role_id`.'
      )
    case 404:
      // The API answers 404 both for a guild that does not exist and for
      // one the caller does not administer, on purpose: it will not
      // confirm the existence of a server to somebody with no business
      // there. So this sentence has to cover both without guessing which.
      return (
        'Sturnus does not know this server, or you no longer administer it — it answers the same '
        + 'way to both. Reload the page; the list of servers is rebuilt from Discord.'
      )
    case null:
      return 'Could not reach the API. Nothing here is out of date on purpose; check the connection and retry.'
    default:
      return `Sturnus answered ${status} and could not report this server’s queue. Nothing is known about why.`
  }
}

/**
 * A poll that cannot outlive the thing it is polling for.
 *
 * Extracted from the page rather than written inline, and not for tidiness:
 * the property that matters here is one no build, type check or render can
 * show, and one that a page component cannot be asked about without a Nuxt
 * runtime around it. Here it is an ordinary function with fake timers
 * pointed at it.
 *
 * The defect it exists to make unrepresentable is the one `RequeuePanel`
 * shipped with. A chain of timeouts is the right shape -- an interval can
 * queue a second request behind a slow first -- but `clearTimeout` cannot
 * stop a timer that has **already fired**, and the continuation after the
 * `await` inside it installs a fresh timer that nothing is left to cancel.
 * Navigating away during the seconds a poll is in flight therefore leaves a
 * loop reading the database for the life of the tab, per page, invisibly.
 *
 * So `alive` is checked *after every await*, which is exactly where an
 * unmount happens without the resuming code being told, and `stop()` sets
 * it false as well as clearing the pending timer. One of those two alone
 * is the bug.
 *
 * `shouldContinue` is re-asked each round rather than captured once,
 * because whether there is anything left to watch is a fact about the data
 * that just came back.
 */
/** What a timer handle is, without committing to a runtime.
 *
 *  `setTimeout` returns a `Timeout` object under Node and a number in a
 *  browser, and this module is compiled with both libraries in scope
 *  because a page is rendered on the server and then polls in the client.
 *  `ReturnType<typeof setTimeout>` resolves to whichever overload the
 *  checker reaches first, which is not the same as what the call actually
 *  returns -- so the union is written out rather than inferred. The loop
 *  never inspects a handle; it stores what its own `setTimer` returned and
 *  hands it back to its own `clearTimer`. */
export type QueueTimer = ReturnType<typeof setTimeout> | number

export interface QueuePoll {
  /** Whether another round is worth making, asked afresh each time. */
  shouldContinue: () => boolean
  /** One re-read. Rejections are the caller's to handle; a rejected round
   *  ends the loop rather than retrying blind, because a poll that keeps
   *  hammering an endpoint that is failing is how a transient fault becomes
   *  a sustained one. */
  run: () => Promise<void>
  delayMs: number
  setTimer?: (callback: () => void, ms: number) => QueueTimer
  clearTimer?: (handle: QueueTimer) => void
}

export interface QueuePollHandle {
  /** Stops the loop for good. Safe to call more than once, and safe to
   *  call from inside the loop's own continuation. */
  stop: () => void
  /** Whether the loop is still able to schedule another round. Exposed for
   *  the tests, which is the whole reason this is a function and not four
   *  lines in a component. */
  readonly alive: boolean
}

export function startQueuePolling(poll: QueuePoll): QueuePollHandle {
  const setTimer = poll.setTimer ?? setTimeout
  const clearTimer = poll.clearTimer ?? clearTimeout

  let alive = true
  let timer: QueueTimer | null = null

  function stop() {
    alive = false
    if (timer !== null) {
      clearTimer(timer)
      timer = null
    }
  }

  function schedule() {
    if (!alive || !poll.shouldContinue()) return
    timer = setTimer(() => {
      // Cleared first: this handle has fired and can no longer be
      // cancelled, so leaving it in place would make `stop()` believe it
      // had cancelled something.
      timer = null
      if (!alive) return
      poll
        .run()
        .then(() => {
          // The check that the extracted version exists for. Between the
          // timer firing and this line the component may have gone, and
          // nothing tells the resuming code so.
          if (!alive) return
          schedule()
        })
        .catch(() => {
          // A failed round ends the loop. The page has an error to show
          // and a refresh control to try again with; a loop that retried
          // on its own would turn one bad second into a request every
          // five for as long as the tab is open.
          stop()
        })
    }, poll.delayMs)
  }

  schedule()
  return {
    stop,
    get alive() {
      return alive
    },
  }
}
