/**
 * How the dashboard's figures are rendered, and what it says when it has
 * no figure.
 *
 * A module rather than a handful of expressions in the template, because
 * every function here is a decision -- what an unmeasured total looks
 * like, which clock a timestamp is shown in, whether a zero is a fact or
 * an absence -- and a decision embedded in a template can only be tested
 * by rendering one.
 *
 * Nothing here writes a sentence any more. Every label and every caveat is
 * a {@link Message}: a key and, where the sentence counts something, the
 * numbers it counts. `i18n/README.md` has the rule and `utils/message.ts`
 * has the shape.
 */
import { NOT_MEASURED, type Instant, type Message } from './message'

/**
 * A session the dashboard points at.
 *
 * Every id is a string, and stays one. A Discord snowflake exceeds
 * JavaScript's safe integer range, where a JSON number silently loses its
 * last digits and produces an id that looks right and names nothing.
 */
export interface SessionPointer {
  id: string
  started_at: string
  ended_at: string | null
  duration_seconds: number | null
  channel_id: string
  channel_name: string | null
}

/** What `GET /api/dashboard` answers for the signed-in participant. */
export interface DashboardSummary {
  /** Null is "never measured", not "spoke for no time". The distinction is
   *  the whole reason this field is nullable: jobs that predate the
   *  measurement columns have no figure to contribute, and a sum that
   *  silently counted them as zero would understate a heavy speaker as
   *  precisely as it describes a silent one. */
  total_speech_seconds: number | null
  /** How many tracks the sum above had to skip. */
  unmeasured_tracks: number
  sessions_attended: number
  sessions_with_protocol: number
  people_spoken_with: number
  words_transcribed: number
  longest_session: SessionPointer | null
  most_recent_session: SessionPointer | null
  first_session: SessionPointer | null
}

/**
 * One figure on the dashboard: a label, what it says, and any caveat.
 *
 * `value` is a bare number where the figure is a count -- so that the
 * locale writes it, and a German reader gets `48.213` rather than a figure
 * that reads to them as forty-eight point two. It is a message where the
 * figure is a length, and `null` where there is no figure at all: a
 * distinction that used to be an em dash written into a string here, where
 * nothing downstream could tell it from a value.
 */
export interface Figure {
  key: string
  /** A translation key, resolved by whoever renders this. Named `…Key` so
   *  that nothing puts it on screen by mistake. */
  labelKey: string
  value: Message | number | null
  /** The line under the figure, or null when it needs no caveat. */
  note: Message | null
}

/** One session, already decided on. */
export interface SessionDescription {
  id: string
  /** The channel's own name needs no translating; a channel with no name
   *  left does. */
  channel: string | Message
  when: Instant | null
  duration: Message | null
}

/** A named session worth pointing at. */
export interface SessionHighlight {
  key: string
  labelKey: string
  session: SessionDescription
}

/** A figure that cannot be true is treated as no figure at all. Negative
 *  seconds and NaN both come from a defect upstream, and rendering them
 *  would put the defect in front of the reader as if it were their data. */
function usable(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

/**
 * A duration a person can compare to another one.
 *
 * Hours and minutes, never a bare count of seconds: "8040 seconds" is a
 * figure the reader has to divide twice before it means anything. Units
 * that would read as zero are dropped, and hours keep counting past
 * twenty-four rather than rolling into days -- "1 d 6 h" makes somebody do
 * arithmetic before they can compare it to last month's thirty.
 *
 * `null` for a figure that cannot be true, so that the absence travels as
 * an absence rather than as an em dash inside a string.
 *
 * Named `figureDuration` rather than `formatDuration`: that name existed in
 * two modules at once, Nuxt's auto-import picked one by file order, and the
 * warning it printed said which one it had dropped and nothing about what
 * that would look like on screen.
 */
export function figureDuration(seconds: number | null | undefined): Message | null {
  if (!usable(seconds)) return null

  const total = Math.round(seconds)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60

  if (hours > 0) {
    return minutes > 0
      ? { key: 'common.durationHoursMinutes', params: { hours, minutes } }
      : { key: 'common.durationHours', params: { count: hours } }
  }
  if (minutes > 0) {
    return rest > 0
      ? { key: 'common.durationMinutesSeconds', params: { minutes, seconds: rest } }
      : { key: 'common.durationMinutes', params: { count: minutes } }
  }
  return { key: 'common.durationSeconds', params: { count: rest } }
}

/**
 * A count the locale will write.
 *
 * This used to group the digits by hand, with a comment explaining that
 * `Intl.NumberFormat` formats for the runtime's locale and would render
 * `48,213` on the server and `48.213` in a browser set to German. The
 * mismatch was real; the conclusion -- "the console's own text is English;
 * its numbers should be too" -- has stopped being true. The locale is now
 * chosen rather than resolved, and it is the same on both sides, so the
 * grouping can be the reader's. It happens in `useSay`, and this returns
 * the number itself so that nothing between here and the screen has to
 * agree with it about separators.
 */
export function figureCount(value: number | null | undefined): number | null {
  if (!usable(value)) return null
  return Math.round(value)
}

/* -------------------------------------------------------------------- */
/* The English renderers the untranslated pages still read                */
/* -------------------------------------------------------------------- */

/**
 * A count, grouped so it can be read without counting digits.
 *
 * **English, and knowingly so.** `~/utils/queue` and `~/utils/consents`
 * still build English sentences by hand, because the three pages they serve
 * -- Queue, User Settings and Bot Settings -- are being rewritten in three
 * other pull requests and translating them here would collide with all
 * three. This is what those two modules call, and it keeps the behaviour
 * they were written against: grouped with commas, and an em dash rather
 * than a zero for a figure that cannot be true. It goes when the last of
 * those pages is translated; `figureCount` is what everything else uses.
 */
export function formatCount(value: number | null | undefined): string {
  if (!usable(value)) return NOT_MEASURED
  return Math.round(value).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

/** Month names for {@link formatMoment}, and for nothing else. Every other
 *  month on screen is now `Intl`'s, through a named datetime format. */
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** A moment in UTC, in English. The other half of what the untranslated
 *  admin pages read; see {@link formatCount}. */
export function formatMoment(iso: string | null | undefined): string {
  if (!iso) return NOT_MEASURED
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return NOT_MEASURED

  const day = at.getUTCDate()
  const month = MONTHS[at.getUTCMonth()]
  const year = at.getUTCFullYear()
  const hour = String(at.getUTCHours()).padStart(2, '0')
  const minute = String(at.getUTCMinutes()).padStart(2, '0')
  return `${day} ${month} ${year}, ${hour}:${minute} UTC`
}

/**
 * A moment, in UTC, saying so.
 *
 * The viewer's own zone would be friendlier and is not available: the
 * server render has no idea what it is, so the two renders would disagree
 * and hydration would rewrite every timestamp on the page. Naming the zone
 * is the honest half of that trade -- an unlabelled time in a zone that is
 * not yours is worse than a labelled one, and the `utcMoment` format says
 * so in whichever language is reading it.
 */
export function figureMoment(iso: string | null | undefined): Instant | null {
  if (!iso) return null
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return null
  return { at, format: 'utcMoment' }
}

/**
 * What to call the channel a session happened in.
 *
 * The id in full when there is no name -- a channel deleted since the
 * meeting has none to fetch, and a shortened snowflake identifies nothing
 * and cannot be searched for. Wide on a phone; the template wraps it.
 *
 * Not exported. `~/utils/recordings` exports a `channelLabel` of its own,
 * and for as long as both were exported Nuxt auto-imported one of them into
 * every component and dropped the other by file order.
 */
function channelOf(pointer: SessionPointer): string | Message {
  return pointer.channel_name
    ? `#${pointer.channel_name}`
    : { key: 'recordings.channelById', params: { id: pointer.channel_id } }
}

/** One session, decided on. Null in, null out: a highlight the endpoint had
 *  no session for is one the page should leave out entirely. */
export function describeSession(
  pointer: SessionPointer | null | undefined,
): SessionDescription | null {
  if (!pointer) return null
  return {
    id: pointer.id,
    channel: channelOf(pointer),
    when: figureMoment(pointer.started_at),
    duration: figureDuration(pointer.duration_seconds),
  }
}

/**
 * What the speech total does not cover.
 *
 * A total that silently omits half the data is worse than no total: the
 * reader trusts it, and it is wrong in a direction they cannot guess. So
 * an omission is always stated, and an absent total is explained rather
 * than left as a bare dash somebody has to interpret.
 */
export function speechNote(
  totalSeconds: number | null | undefined,
  unmeasuredTracks: number,
): Message | null {
  const skipped = usable(unmeasuredTracks) ? Math.round(unmeasuredTracks) : 0

  if (!usable(totalSeconds)) {
    if (skipped > 0) return { key: 'dashboard.speechNothingMeasured', params: { count: skipped } }
    return { key: 'dashboard.speechNothingYet' }
  }

  if (skipped === 0) return null
  return { key: 'dashboard.speechSkipped', params: { count: skipped } }
}

/**
 * The figures, in the order they are read.
 *
 * Speech time first because it is the question the page exists to answer;
 * the rest give it context -- a total means one thing across three
 * meetings and another across sixty.
 */
export function summaryFigures(summary: DashboardSummary): Figure[] {
  const attended = usable(summary.sessions_attended) ? Math.round(summary.sessions_attended) : 0

  return [
    {
      key: 'speech',
      labelKey: 'dashboard.speechLabel',
      value: figureDuration(summary.total_speech_seconds),
      note: speechNote(summary.total_speech_seconds, summary.unmeasured_tracks),
    },
    {
      key: 'sessions',
      labelKey: 'dashboard.sessionsLabel',
      value: figureCount(summary.sessions_attended),
      note: null,
    },
    {
      key: 'protocols',
      labelKey: 'dashboard.protocolsLabel',
      value: figureCount(summary.sessions_with_protocol),
      // The denominator belongs next to the numerator. Nine protocols is
      // most of a busy month or a third of a busier one.
      note: attended > 0 ? { key: 'dashboard.protocolsNote', params: { count: attended } } : null,
    },
    {
      key: 'people',
      labelKey: 'dashboard.peopleLabel',
      value: figureCount(summary.people_spoken_with),
      note: null,
    },
    {
      key: 'words',
      labelKey: 'dashboard.wordsLabel',
      value: figureCount(summary.words_transcribed),
      note: null,
    },
  ]
}

/**
 * The sessions worth pointing at, and only the ones that exist.
 *
 * A card labelled "Longest" with an em dash in it reads as still loading
 * rather than as absent, so a missing pointer produces no card at all.
 */
export function sessionHighlights(summary: DashboardSummary): SessionHighlight[] {
  const candidates: { key: string, labelKey: string, pointer: SessionPointer | null }[] = [
    {
      key: 'most_recent',
      labelKey: 'dashboard.highlightMostRecent',
      pointer: summary.most_recent_session,
    },
    { key: 'longest', labelKey: 'dashboard.highlightLongest', pointer: summary.longest_session },
    { key: 'first', labelKey: 'dashboard.highlightFirst', pointer: summary.first_session },
  ]

  return candidates.flatMap(({ key, labelKey, pointer }) => {
    const session = describeSession(pointer)
    return session ? [{ key, labelKey, session }] : []
  })
}

/**
 * Whether this person has ever been recorded at all.
 *
 * Deliberately stricter than `sessions_attended === 0`. Measured silence
 * is a result and gets a grid of zeros; an attendance count of zero
 * standing next to four hundred transcribed words is a defect upstream,
 * and showing the figures makes it visible where the empty state would
 * hide it behind an invitation to do something they have already done.
 */
export function hasNothingRecorded(summary: DashboardSummary): boolean {
  const counts = [
    summary.sessions_attended,
    summary.sessions_with_protocol,
    summary.people_spoken_with,
    summary.words_transcribed,
  ]
  const pointers = [summary.most_recent_session, summary.longest_session, summary.first_session]

  return (
    counts.every((count) => !usable(count) || Math.round(count) === 0)
    && pointers.every((pointer) => !pointer)
    && !usable(summary.total_speech_seconds)
  )
}

/**
 * The status a failed call came back with, if it came back at all.
 *
 * `$fetch` names it `statusCode`; a response object names it `status`; a
 * connection that never completed has neither, and null says so rather
 * than standing in a number that would read as an answer from the API.
 */
export function failureStatus(failure: unknown): number | null {
  const raw = (failure as { statusCode?: unknown, status?: unknown } | null)?.statusCode
    ?? (failure as { status?: unknown } | null)?.status
  return typeof raw === 'number' && Number.isFinite(raw) ? raw : null
}

/**
 * Why the dashboard could not be loaded, in words the reader can act on.
 *
 * Deliberately built from the status alone and never from the error's own
 * message. `$fetch` puts the URL it called into what it throws, and during
 * a server render that URL is the API's in-cluster address -- echoing it
 * into the page would publish an internal hostname to anybody who happened
 * to load a failing dashboard. The status is the part that tells somebody
 * what to do next anyway.
 *
 * The status travels into the sentence as a string. It is a number without
 * being a quantity, and `useSay` writes quantities in the locale's grouping
 * -- which would turn a hypothetical 1000 into `1,000` and a German
 * reader's `1.000`.
 */
export function describeFailure(failure: unknown): Message {
  const code = failureStatus(failure)

  if (code === 401 || code === 403) return { key: 'dashboard.failureSession' }
  // The console and the API ship as two images and can be deployed apart.
  // A bare 404 would send somebody looking for a fault in their own
  // account rather than in a version skew.
  if (code === 404) return { key: 'dashboard.failureNoDashboard' }
  if (code === null) return { key: 'dashboard.failureUnreachable' }
  return { key: 'dashboard.failureStatus', params: { code: String(code) } }
}

/**
 * Whether the failure was the session being refused.
 *
 * The two failures need different offers. A restarting API is worth one
 * button press; a session the API will not accept is a loop that cannot
 * succeed, however many times somebody presses it.
 */
export function isSessionFailure(failure: unknown): boolean {
  const code = failureStatus(failure)
  return code === 401 || code === 403
}
