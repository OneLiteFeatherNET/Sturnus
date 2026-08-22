/**
 * How the dashboard's figures are rendered, and what it says when it has
 * no figure.
 *
 * A module rather than a handful of expressions in the template, because
 * every function here is a decision -- what an unmeasured total looks
 * like, which clock a timestamp is shown in, whether a zero is a fact or
 * an absence -- and a decision embedded in a template can only be tested
 * by rendering one.
 */

/** The one thing that means "there is no figure here". Never "0". */
const NOTHING = '—'

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

/** One figure on the dashboard: a label, what it says, and any caveat. */
export interface Figure {
  key: string
  label: string
  value: string
  /** The line under the figure, or null when it needs no caveat. */
  note: string | null
}

/** One session, already rendered. */
export interface SessionDescription {
  id: string
  channel: string
  when: string
  duration: string
}

/** A named session worth pointing at. */
export interface SessionHighlight {
  key: string
  label: string
  session: SessionDescription
}

/** A figure that cannot be true is treated as no figure at all. Negative
 *  seconds and NaN both come from a defect upstream, and rendering them
 *  would put the defect in front of the reader as if it were their data. */
function usable(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

function plural(count: number, singular: string): string {
  return count === 1 ? singular : `${singular}s`
}

/**
 * A duration a person can compare to another one.
 *
 * Hours and minutes, never a bare count of seconds: "8040 seconds" is a
 * figure the reader has to divide twice before it means anything. Units
 * that would read as zero are dropped, and hours keep counting past
 * twenty-four rather than rolling into days -- "1 d 6 h" makes somebody do
 * arithmetic before they can compare it to last month's thirty.
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (!usable(seconds)) return NOTHING

  const total = Math.round(seconds)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60

  if (hours > 0) return minutes > 0 ? `${hours} h ${minutes} min` : `${hours} h`
  if (minutes > 0) return rest > 0 ? `${minutes} min ${rest} s` : `${minutes} min`
  return `${rest} s`
}

/**
 * A count, grouped so it can be read without counting digits.
 *
 * Grouped by hand rather than through `Intl.NumberFormat`, because that
 * formats for the runtime's locale: the same figure would render as
 * `48,213` during the server render and `48.213` in a browser set to
 * German, which Vue reports as a hydration mismatch and the reader sees as
 * a flicker. The console's own text is English; its numbers should be too.
 */
export function formatCount(value: number | null | undefined): string {
  if (!usable(value)) return NOTHING
  return Math.round(value).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

/**
 * A moment, in UTC, saying so.
 *
 * The viewer's own zone would be friendlier and is not available: the
 * server render has no idea what it is, so the two renders would disagree
 * and hydration would rewrite every timestamp on the page. Naming the zone
 * is the honest half of that trade -- an unlabelled time in a zone that is
 * not yours is worse than a labelled one.
 */
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

export function formatMoment(iso: string | null | undefined): string {
  if (!iso) return NOTHING
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return NOTHING

  const day = at.getUTCDate()
  const month = MONTHS[at.getUTCMonth()]
  const year = at.getUTCFullYear()
  const hour = String(at.getUTCHours()).padStart(2, '0')
  const minute = String(at.getUTCMinutes()).padStart(2, '0')
  return `${day} ${month} ${year}, ${hour}:${minute} UTC`
}

/**
 * What to call the channel a session happened in.
 *
 * The id in full when there is no name -- a channel deleted since the
 * meeting has none to fetch, and a shortened snowflake identifies nothing
 * and cannot be searched for. Wide on a phone; the template wraps it.
 */
export function channelLabel(pointer: SessionPointer): string {
  return pointer.channel_name ? `#${pointer.channel_name}` : `Channel ${pointer.channel_id}`
}

/** One session, rendered. Null in, null out: a highlight the endpoint had
 *  no session for is one the page should leave out entirely. */
export function describeSession(
  pointer: SessionPointer | null | undefined,
): SessionDescription | null {
  if (!pointer) return null
  return {
    id: pointer.id,
    channel: channelLabel(pointer),
    when: formatMoment(pointer.started_at),
    duration: formatDuration(pointer.duration_seconds),
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
): string | null {
  const skipped = usable(unmeasuredTracks) ? Math.round(unmeasuredTracks) : 0

  if (!usable(totalSeconds)) {
    if (skipped > 0) {
      return `None of your ${formatCount(skipped)} ${plural(skipped, 'track')} were measured; they predate the measurement.`
    }
    return 'Nothing has been measured yet.'
  }

  if (skipped === 0) return null
  const verb = skipped === 1 ? 'is' : 'are'
  return `${formatCount(skipped)} ${plural(skipped, 'track')} recorded before Sturnus measured speech ${verb} not counted.`
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
      label: 'Time you have spoken',
      value: formatDuration(summary.total_speech_seconds),
      note: speechNote(summary.total_speech_seconds, summary.unmeasured_tracks),
    },
    {
      key: 'sessions',
      label: 'Sessions attended',
      value: formatCount(summary.sessions_attended),
      note: null,
    },
    {
      key: 'protocols',
      label: 'Sessions with a protocol',
      value: formatCount(summary.sessions_with_protocol),
      // The denominator belongs next to the numerator. Nine protocols is
      // most of a busy month or a third of a busier one.
      note: attended > 0 ? `of ${formatCount(attended)} ${plural(attended, 'session')} attended` : null,
    },
    {
      key: 'people',
      label: 'People you have spoken with',
      value: formatCount(summary.people_spoken_with),
      note: null,
    },
    {
      key: 'words',
      label: 'Words transcribed',
      value: formatCount(summary.words_transcribed),
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
  const candidates: { key: string, label: string, pointer: SessionPointer | null }[] = [
    { key: 'most_recent', label: 'Most recent', pointer: summary.most_recent_session },
    { key: 'longest', label: 'Longest', pointer: summary.longest_session },
    { key: 'first', label: 'First', pointer: summary.first_session },
  ]

  return candidates.flatMap(({ key, label, pointer }) => {
    const session = describeSession(pointer)
    return session ? [{ key, label, session }] : []
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
 */
export function describeFailure(failure: unknown): string {
  const code = failureStatus(failure)

  if (code === 401 || code === 403) return 'Your session is no longer valid.'
  // The console and the API ship as two images and can be deployed apart.
  // A bare 404 would send somebody looking for a fault in their own
  // account rather than in a version skew.
  if (code === 404) return 'This API has no dashboard yet; it is older than this console.'
  if (code === null) return 'The API could not be reached.'
  return `The API answered ${code} and could not produce your figures.`
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
