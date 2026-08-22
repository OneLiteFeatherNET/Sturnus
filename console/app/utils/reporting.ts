/**
 * How one Discord server uses Sturnus, said in figures that do not
 * overstate themselves.
 *
 * A module rather than expressions in the page, for the same reason
 * `~/utils/queue` is one: every function here is a *decision* -- what an
 * absent average reads like, which month comes first, whether a gap in a
 * bar row is drawn or skipped, what the speech total is actually a total
 * of, whether an empty report is a fault -- and a decision embedded in a
 * template can only be tested by rendering one.
 *
 * Five facts govern the wording below, and none of them are softened
 * anywhere in this file. Each is a way this page could quietly mislead
 * somebody who trusted it:
 *
 * - **This report is about a server, never about the people in it.** The
 *   payload carries no ids and no names, and nothing here invents a
 *   per-person figure or hints that one is coming. How long one named
 *   person sat in meetings, or spoke in them, is a measure of that
 *   person's conduct at work -- a matter for a works council, not a
 *   console feature. Counts of people are counts and stop there.
 * - **Null is not zero.** `average_duration_seconds`, `longest_duration_
 *   seconds`, `average_participants` and `largest_meeting` are null for a
 *   server whose meetings have not finished. Printing `0` would state that
 *   its meetings are instantaneous and attended by nobody, which is a
 *   claim rather than an absence, so an absence renders as one and carries
 *   the sentence that says why.
 * - **`unmeasured_tracks` is the size of a hole in `speech_seconds`.** The
 *   per-track speech column is nullable: null means nobody ever measured,
 *   zero means somebody measured and it was silence. `SUM` skips nulls
 *   without comment, so a server with most of its tracks unmeasured gets a
 *   speech total that is short by an unknown amount. Every rendering of
 *   that total says so, in the words "not that this server was quiet" --
 *   because a small number under a large one is read as quiet by anybody
 *   not told otherwise.
 * - **`recorded_seconds` excludes the meetings still open.** A session
 *   with no `ended_at` has no length yet, and a session that has been
 *   "recording" for three days is a fault rather than a long meeting. So
 *   `open_sessions` is shown as a figure of its own instead of being
 *   allowed to vanish into a total it is not part of.
 * - **The months were cut in the server's own calendar.** A meeting that
 *   starts at 00:30 belongs to the month the people in it think it does,
 *   which is why the API buckets by `timezone` rather than by UTC -- and
 *   why a reader who is not told the zone will assume it is theirs. The
 *   instants on this page are still written in UTC, since a server render
 *   has no idea what zone the reader is in, so the page says both.
 *
 * Nothing here decides what to do about a backlog. That question belongs
 * to the Queue page, which reads the pipeline rather than the calendar; a
 * second answer to "is something stuck" would be a second definition of
 * stuck, and the two would drift.
 */
import { formatCount, formatDuration, formatMoment } from '~/utils/format'

/* -------------------------------------------------------------------- */
/* What the API describes                                                */
/* -------------------------------------------------------------------- */

/**
 * One calendar month of recording, as the API bucketed it.
 *
 * Months with no sessions are **absent** from the payload rather than sent
 * as zeros -- a server that met in March and again in November sends two
 * entries, not thirteen. What this module does about that gap is decided
 * in `reportMonthRows`, deliberately and in one place.
 */
export interface ReportMonth {
  /** `YYYY-MM`, in the server's own zone. Malformed months never reach
   *  here; see `parseGuildReport`. */
  month: string
  sessions: number
  /** Null when the API had no total to give, which is not the same as a
   *  month in which nothing ran for any time at all. */
  recorded_seconds: number | null
  documented: number
}

/**
 * One server's whole history with Sturnus.
 *
 * The four nullable figures are nullable on purpose and are kept nullable
 * all the way to the screen. Collapsing them to zero at the edge of this
 * module would put the lie somewhere no test could see it.
 */
export interface GuildReport {
  /** Null only when the payload named no server, which the page uses to
   *  refuse to show one server's figures under another's heading. */
  guild_id: string | null
  sessions: number
  documented: number
  /** Sessions with no `ended_at`. Counted in `sessions`, counted in
   *  nothing else. */
  open_sessions: number
  recorded_seconds: number | null
  /** The sum of a nullable column, so null means "nothing was ever
   *  measured" rather than "nothing was said". */
  speech_seconds: number | null
  /** How many tracks the sum above had to skip. */
  unmeasured_tracks: number
  tracks: number
  /** How many different people this server has recorded. Not a list, and
   *  never on its way to becoming one. */
  distinct_participants: number
  /** Null until a meeting has finished. */
  average_participants: number | null
  /** The most people in any one meeting. Null until a meeting has
   *  finished. */
  largest_meeting: number | null
  average_duration_seconds: number | null
  longest_duration_seconds: number | null
  first_session_at: string | null
  last_session_at: string | null
  /** The IANA zone the months were cut in. Empty when the API did not say,
   *  which the timezone note reports as the uncertainty it is. */
  timezone: string
  months: ReportMonth[]
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
 * rendering it as "-3 meetings" would put that defect in front of the
 * reader as though it were a fact about their server. Zero is the honest
 * floor: it says "none", which is as far as this console can vouch.
 */
function asCount(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 0
  return Math.round(value)
}

/**
 * A count that is allowed to be missing.
 *
 * The distinction this keeps is the whole point of the field being
 * nullable: `largest_meeting` is null for a server whose meetings have not
 * finished, and rounding that to zero would claim it holds meetings nobody
 * attends. Nonsense collapses to null rather than to zero for the same
 * reason -- "we do not know" is true of a broken figure and "none" is not.
 */
function asOptionalCount(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null
  return Math.round(value)
}

/** A quantity that is allowed to be missing and is not a whole number --
 *  seconds, and an average of people. Kept unrounded; how many digits it
 *  is worth showing is a rendering decision and is made where it shows. */
function asOptionalNumber(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null
  return value
}

/** `YYYY-MM`, and nothing looser. A month this module cannot place on a
 *  calendar cannot be ordered, cannot be labelled and -- worst -- would
 *  anchor the gap filling in `reportMonthRows` at an arbitrary point in
 *  history, turning one bad string into a thousand rows of zeros. */
const MONTH_KEY = /^\d{4}-(0[1-9]|1[0-2])$/

function asMonths(value: unknown): ReportMonth[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((entry) => {
    if (!isRecord(entry)) return []
    const month = asText(entry.month)
    if (!month || !MONTH_KEY.test(month)) return []
    return [
      {
        month,
        sessions: asCount(entry.sessions),
        recorded_seconds: asOptionalNumber(entry.recorded_seconds),
        documented: asCount(entry.documented),
      },
    ]
  })
}

/**
 * The report in a payload.
 *
 * Always yields a well-formed value, never null. A page that has to
 * distinguish "the API refused" from "the API answered something odd"
 * already has the thrown `ApiError` for the first; a parser that returned
 * null for the second would turn a strange payload into a blank page with
 * no error anywhere, which is the failure mode hardest to report.
 */
export function parseGuildReport(payload: unknown): GuildReport {
  const raw = isRecord(payload) ? payload : {}
  return {
    guild_id: asText(raw.guild_id),
    sessions: asCount(raw.sessions),
    documented: asCount(raw.documented),
    open_sessions: asCount(raw.open_sessions),
    recorded_seconds: asOptionalNumber(raw.recorded_seconds),
    speech_seconds: asOptionalNumber(raw.speech_seconds),
    unmeasured_tracks: asCount(raw.unmeasured_tracks),
    tracks: asCount(raw.tracks),
    distinct_participants: asCount(raw.distinct_participants),
    average_participants: asOptionalNumber(raw.average_participants),
    largest_meeting: asOptionalCount(raw.largest_meeting),
    average_duration_seconds: asOptionalNumber(raw.average_duration_seconds),
    longest_duration_seconds: asOptionalNumber(raw.longest_duration_seconds),
    first_session_at: asText(raw.first_session_at),
    last_session_at: asText(raw.last_session_at),
    timezone: asText(raw.timezone) ?? '',
    months: asMonths(raw.months),
  }
}

/** Where a server's report is read from. The id is escaped: it is a string
 *  from an API, and a string allowed to contain a slash is a string
 *  allowed to address a different endpoint. */
export function reportPath(guildId: string): string {
  return `/guilds/${encodeURIComponent(guildId)}/report`
}

/* -------------------------------------------------------------------- */
/* Writing a figure down                                                 */
/* -------------------------------------------------------------------- */

/** The one thing that means "there is no figure here". Never "0", and the
 *  same glyph the dashboard uses, so an absence looks the same in both
 *  places a reader might meet one. */
const NO_FIGURE = '—'

/**
 * Full month names, written out here rather than taken from `Intl`.
 *
 * `Intl.DateTimeFormat` formats for the runtime's locale, so the same
 * month would render as "August" during the server render and "August"
 * only by luck in a browser set to German -- which Vue reports as a
 * hydration mismatch and the reader sees as a flicker across every row of
 * the table. The console's own text is English; its months should be too.
 */
const MONTH_NAMES = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]

/** `2026-08` → `August 2026`. A month key this function cannot read comes
 *  back unchanged rather than as a blank: a raw key is at least something
 *  the reader can match against the payload. */
export function reportMonthLabel(month: string): string {
  if (!MONTH_KEY.test(month)) return month
  const year = Number(month.slice(0, 4))
  const index = Number(month.slice(5, 7)) - 1
  return `${MONTH_NAMES[index]} ${year}`
}

/** An average of people, to one decimal, without the trailing `.0` that
 *  makes a whole number look like a measurement it is not. */
function averageInWords(value: number): string {
  const rounded = Math.round(value * 10) / 10
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1)
}

/** One or the other, chosen by the count. Written out at each call rather
 *  than derived by adding an `s`, because half the pairs this page needs
 *  are `is`/`are` and `was`/`were`. */
function plural(count: number, one: string, many: string): string {
  return count === 1 ? one : many
}

function meetings(count: number): string {
  return `${formatCount(count)} ${plural(count, 'meeting', 'meetings')}`
}

/* -------------------------------------------------------------------- */
/* The figures themselves                                                */
/* -------------------------------------------------------------------- */

/**
 * Three tones, and they are about the reader rather than about the data.
 *
 * `plain` is a figure that says what it says. `absent` is the deliberate
 * lack of one, which must not be coloured like a number that happens to be
 * small. `watch` is a figure worth a second look -- today that is only the
 * count of meetings still open, which is either a meeting happening right
 * now or a session that never closed.
 */
export type ReportTone = 'plain' | 'absent' | 'watch'

/**
 * One figure on the page: a label, what it says, and the sentence under
 * it.
 *
 * Deliberately not `Figure` from `~/utils/format`. That type is the
 * dashboard's, where every figure is a number somebody has and none of
 * them can be missing; widening it with a tone for this page's sake would
 * change a shape three other things depend on in order to serve one that
 * does not exist yet.
 */
export interface ReportFigure {
  key: string
  label: string
  value: string
  /** The line under the figure. Never null for a missing value: an em dash
   *  with nothing beside it reads as "still loading", which is the one
   *  thing this page is not doing. */
  note: string | null
  tone: ReportTone
}

/**
 * What share of this server's meetings reached a protocol, as a percentage
 * -- or null when it has held none.
 *
 * Rounding is clamped away from both ends on purpose. A server with 999 of
 * 1000 meetings written up rounds to 100 %, and "100 %" beside a figure
 * that is not all of them is the page telling somebody every meeting is
 * covered when one is not. The same holds at the bottom: one success out
 * of a thousand is not "0 %" of them.
 */
export function reportDocumentedShare(report: GuildReport): number | null {
  if (report.sessions <= 0) return null
  if (report.documented >= report.sessions) return 100
  const raw = Math.round((report.documented / report.sessions) * 100)
  if (report.documented > 0 && raw <= 0) return 1
  return Math.min(99, Math.max(0, raw))
}

/**
 * How the pipeline has done, as this server experienced it.
 *
 * Written as "n of m", not as a bare rate: a rate on its own is read as a
 * property of the software, and this is a property of what happened here.
 * Where meetings are still recording that is said too -- a meeting that
 * has not ended cannot have been written up, and counting it as a failure
 * of the pipeline would be blaming the pipeline for the clock.
 */
export function reportDocumentedLine(report: GuildReport): string {
  if (report.sessions <= 0) {
    return 'Nothing has been recorded in this server yet, so there is nothing to write up.'
  }
  const open
    = report.open_sessions > 0
      ? ` ${meetings(report.open_sessions)} ${plural(report.open_sessions, 'is', 'are')} still `
        + 'recording and cannot have been written up yet; they are counted in the total all the '
        + 'same.'
      : ''
  if (report.documented >= report.sessions) {
    return (
      `Every one of the ${meetings(report.sessions)} recorded in this server reached a protocol.`
      + open
    )
  }
  const missing = report.sessions - report.documented
  const share = reportDocumentedShare(report)
  return (
    `${formatCount(report.documented)} of the ${meetings(report.sessions)} recorded in this server `
    + `reached a protocol — ${share} %. The other ${formatCount(missing)} `
    + `${plural(missing, 'was', 'were')} recorded and never written up.${open}`
  )
}

/**
 * What the recorded total covers, and what it leaves out.
 *
 * Said whether or not anything is open. A total whose exclusions are
 * mentioned only when they bite is a total whose scope the reader has to
 * infer from its own silence.
 */
export function reportRecordedLine(report: GuildReport): string {
  if (report.open_sessions > 0) {
    return (
      'Adds up the length of every meeting in this server that has ended. The '
      + `${meetings(report.open_sessions)} still recording ${plural(report.open_sessions, 'is', 'are')} `
      + 'not in it — a meeting has no length until it stops.'
    )
  }
  return 'Adds up the length of every meeting this server has recorded, all of which have ended.'
}

/**
 * What is happening in this server at this moment, or the fact that
 * nothing is.
 *
 * Shown whether or not it is zero. A figure that appears only when it is
 * bad news is a figure whose absence has to be interpreted, and "nothing
 * is stuck open" and "this page does not report sessions left open" look
 * identical on screen.
 *
 * The sentence names the second reading on purpose. One meeting open for
 * ten minutes is a meeting; one open since Tuesday is a session that never
 * closed, and the number alone cannot tell them apart.
 */
export function reportOpenSessionsLine(report: GuildReport): string {
  if (report.open_sessions <= 0) {
    return (
      'Nothing is being recorded in this server right now — every meeting it has recorded has '
      + 'ended and has a length.'
    )
  }
  const count = report.open_sessions
  return (
    `${meetings(count)} in this server ${plural(count, 'has', 'have')} no end time yet. That is `
    + 'either a meeting happening at this moment or a session that never closed, and this figure '
    + 'cannot tell the two apart — a session open for days is the second. Neither its length nor '
    + 'its speech is counted anywhere else on this page.'
  )
}

/** The reason a figure is absent, said as the absence it is rather than
 *  left as a dash the reader has to account for. */
function noFinishedMeetings(what: string): string {
  return (
    `No meeting in this server has finished, so there is no ${what} to give. This is the absence `
    + 'of a figure and not a figure of zero.'
  )
}

/**
 * The headline band: how much this server has used Sturnus at all.
 *
 * Meetings first, because it is the question the page is opened with, and
 * every figure after it is context -- four hours of recording means one
 * thing across three meetings and another across sixty.
 */
export function reportHeadlineFigures(report: GuildReport): ReportFigure[] {
  return [
    {
      key: 'sessions',
      label: 'Meetings recorded',
      value: formatCount(report.sessions),
      note: reportSpanLine(report),
      tone: 'plain',
    },
    {
      key: 'documented',
      label: 'Meetings written up',
      value: formatCount(report.documented),
      note: reportDocumentedLine(report),
      tone: 'plain',
    },
    {
      key: 'recorded',
      label: 'Time recorded',
      value: formatDuration(report.recorded_seconds),
      note: reportRecordedLine(report),
      tone: report.recorded_seconds === null ? 'absent' : 'plain',
    },
    {
      key: 'speech',
      label: 'Time spoken',
      value: formatDuration(report.speech_seconds),
      note: reportSpeechCaveat(report),
      tone: report.speech_seconds === null ? 'absent' : 'plain',
    },
  ]
}

/**
 * What a meeting in this server looks like: how long, and how many people.
 *
 * Kept apart from the headline band because these are a different kind of
 * number. Those describe how much has happened; these describe the shape
 * of one meeting, and four of the six can legitimately be missing.
 *
 * Nothing in here names anybody. `distinct_participants` is a count of
 * people and is the closest this page comes to the individuals in a
 * server; it goes no further, and the note beside it says so rather than
 * leaving the reader to wonder whether a list is one click away.
 */
export function reportShapeFigures(report: GuildReport): ReportFigure[] {
  const average = report.average_duration_seconds
  const longest = report.longest_duration_seconds
  const perMeeting = report.average_participants
  const largest = report.largest_meeting

  return [
    {
      key: 'average-duration',
      label: 'Typical meeting',
      value: average === null ? NO_FIGURE : formatDuration(average),
      note:
        average === null
          ? noFinishedMeetings('length to average')
          : 'The mean length of the meetings that have ended here.',
      tone: average === null ? 'absent' : 'plain',
    },
    {
      key: 'longest-duration',
      label: 'Longest meeting',
      value: longest === null ? NO_FIGURE : formatDuration(longest),
      note:
        longest === null
          ? noFinishedMeetings('length to compare')
          : 'The longest single meeting this server has recorded from start to finish.',
      tone: longest === null ? 'absent' : 'plain',
    },
    {
      key: 'average-participants',
      label: 'People per meeting',
      value: perMeeting === null ? NO_FIGURE : averageInWords(perMeeting),
      note:
        perMeeting === null
          ? noFinishedMeetings('attendance to average')
          : 'The mean number of people recorded in a meeting that has ended here.',
      tone: perMeeting === null ? 'absent' : 'plain',
    },
    {
      key: 'largest-meeting',
      label: 'Largest meeting',
      value: largest === null ? NO_FIGURE : formatCount(largest),
      note:
        largest === null
          ? noFinishedMeetings('attendance to compare')
          : 'The most people recorded in any one meeting in this server.',
      tone: largest === null ? 'absent' : 'plain',
    },
    {
      key: 'participants',
      label: 'People recorded',
      value: formatCount(report.distinct_participants),
      note:
        'How many different people this server has recorded at all. A count and nothing else — '
        + 'Sturnus does not send this page their names, and this page does not ask.',
      tone: 'plain',
    },
    {
      key: 'open',
      label: 'Still recording',
      value: formatCount(report.open_sessions),
      note: reportOpenSessionsLine(report),
      tone: report.open_sessions > 0 ? 'watch' : 'plain',
    },
  ]
}

/* -------------------------------------------------------------------- */
/* The caveats that travel with the figures                              */
/* -------------------------------------------------------------------- */

/**
 * What the speech total is a total *of*.
 *
 * The single most misreadable number on this page, and the reason this
 * paragraph exists. Speaking time is measured per track and the column is
 * nullable: null means nobody ever measured that track -- jobs that
 * predate the measurement columns -- and zero means somebody measured and
 * heard nothing. `SUM` skips the nulls without saying so, so the total is
 * short by however much those tracks held.
 *
 * The failure this wording exists to prevent is specific: a server with
 * most of its tracks unmeasured shows a small speech figure under a large
 * recorded figure, and the obvious reading is "these meetings were quiet".
 * The obvious reading is wrong, so the sentence rules it out in words
 * rather than leaving it to be inferred from a ratio.
 */
export function reportSpeechCaveat(report: GuildReport): string {
  const { tracks, unmeasured_tracks: unmeasured } = report

  if (tracks <= 0) {
    return (
      'No audio has been recorded in this server, so there is nothing to have measured. The '
      + 'speaking time above is missing rather than zero.'
    )
  }

  if (unmeasured <= 0) {
    return (
      `Every one of the ${formatCount(tracks)} recorded ${plural(tracks, 'track', 'tracks')} in `
      + 'this server carries a measured speaking time, so the figure above covers all of what was '
      + 'recorded.'
    )
  }

  const measured = Math.max(0, tracks - unmeasured)
  if (measured === 0) {
    return (
      `None of the ${formatCount(tracks)} recorded ${plural(tracks, 'track', 'tracks')} in this `
      + 'server was ever measured for speaking time — they all predate the columns that hold it — '
      + 'so there is no speaking time here to report. Read the figure above as a measurement that '
      + 'was never taken, not as a server that was quiet.'
    )
  }

  return (
    `${formatCount(unmeasured)} of the ${formatCount(tracks)} recorded `
    + `${plural(tracks, 'track', 'tracks')} in this server ${plural(unmeasured, 'was', 'were')} `
    + 'never measured for speaking time — they predate the columns that hold it — and a sum skips '
    + `them in silence. The speaking time above is therefore the total for the other `
    + `${formatCount(measured)} ${plural(measured, 'track', 'tracks')} only: it describes part of `
    + 'what was recorded, and the larger this number grows the further short the figure falls. It '
    + 'does not mean this server was quiet.'
  )
}

/**
 * Which calendar the months were cut in.
 *
 * A reader who is not told a zone assumes their own, and month boundaries
 * are exactly where that assumption costs something: a meeting at 00:30 on
 * the first falls into either month depending on who is asking. Sturnus
 * buckets by the server's own zone on purpose -- a meeting belongs to the
 * month the people in it think it does -- and that choice is only worth
 * anything if the page names the zone it made.
 *
 * The second half is the seam this page carries and cannot remove: the
 * instants are written in UTC, because a server-side render has no idea
 * what zone the reader is in and a second rendering in the browser would
 * disagree with the first. So two clocks appear on one page, and saying
 * which is which is cheaper than a reader discovering it from a date that
 * does not match a month.
 */
export function reportTimezoneNote(report: GuildReport): string {
  if (!report.timezone) {
    return (
      'Sturnus did not say which calendar the months below were cut in, so do not assume it is '
      + 'yours: a meeting near midnight falls on either side of a month boundary depending on the '
      + 'zone. Every instant on this page is written in UTC, which may not be that calendar '
      + 'either.'
    )
  }
  return (
    `The months below are cut in ${report.timezone}, this server's own calendar — not in UTC and `
    + 'not in yours. A meeting that begins at 00:30 belongs to the month the people in it think it '
    + 'does, which is why Sturnus does not bucket by UTC. The instants elsewhere on this page are '
    + 'written in UTC all the same, because a page rendered on a server cannot know your zone, so '
    + 'a meeting near a month boundary can carry a UTC date that reads as the neighbouring month.'
  )
}

export interface ReportCaveat {
  key: string
  label: string
  text: string
}

/**
 * The two things a reader has to know before the figures above mean what
 * they appear to mean.
 *
 * In a panel of their own rather than as footnotes. A footnote is read
 * once, by the person who was already being careful; these two are the
 * difference between a figure and a wrong figure.
 */
export function reportCaveats(report: GuildReport): ReportCaveat[] {
  return [
    {
      key: 'speech',
      label: 'What the speaking time covers',
      text: reportSpeechCaveat(report),
    },
    {
      key: 'timezone',
      label: 'Which calendar the months use',
      text: reportTimezoneNote(report),
    },
  ]
}

/* -------------------------------------------------------------------- */
/* The span this report covers                                           */
/* -------------------------------------------------------------------- */

/**
 * From when to when, in UTC and saying so.
 *
 * Both ends can be missing independently: a server whose only session is
 * still open has a first session and, depending on how the API dates the
 * last one, may have nothing to close the span with. Each case gets its
 * own sentence rather than an em dash standing in for half a range.
 */
export function reportSpanLine(report: GuildReport): string {
  const first = report.first_session_at
  const last = report.last_session_at

  if (!first && !last) {
    return 'No meeting has been recorded in this server yet, so this report covers no time at all.'
  }
  if (first && last) {
    if (first === last) {
      return `One meeting, recorded ${formatMoment(first)}.`
    }
    return `Everything recorded in this server between ${formatMoment(first)} and ${formatMoment(last)}.`
  }
  const known = first ?? last
  return (
    `Everything recorded in this server. Only one end of the span is known: ${formatMoment(known)}.`
  )
}

/* -------------------------------------------------------------------- */
/* The months, and the gaps between them                                 */
/* -------------------------------------------------------------------- */

/**
 * The shortest bar a month with any recording is allowed to draw.
 *
 * A busy server makes its quiet months round to nothing, and a month with
 * one meeting rendered as an empty row is indistinguishable from a month
 * with none -- which is the one distinction the filled gaps below exist to
 * draw. Two per cent is enough to be a mark and little enough not to read
 * as a quantity.
 */
export const REPORT_MIN_BAR_EXTENT = 0.02

/**
 * How long a span this page will fill in with silent months.
 *
 * Ten years. Beyond that the filling stops being a service and becomes a
 * wall: a single stray month in the payload would otherwise produce
 * hundreds of rows of zeros and bury the months that carry something. The
 * limit is stated on the page when it bites, because a list of months with
 * gaps silently left out is exactly what the filling exists to prevent.
 */
export const REPORT_MONTH_FILL_LIMIT = 120

export interface ReportMonthRow {
  /** `YYYY-MM`, unique across the rows, so it keys a `v-for` safely. */
  month: string
  label: string
  sessions: number
  documented: number
  recorded: string
  /** True for a month this module added because the payload skipped it. */
  silent: boolean
  /** 0 to 1, against the busiest month in the list. The bar's width, and
   *  nothing else -- it is deliberately not a percentage anybody reads. */
  extent: number
  /** The row said as a sentence, for the reader who is listening to the
   *  page rather than looking at it. A bar with no text is a bar only its
   *  author can read. */
  detail: string
}

/** A month key as a count of months since year zero, so two of them can be
 *  compared and stepped between without ever becoming a `Date` -- which
 *  would drag a timezone into a calculation that has no instant in it. */
function monthIndex(month: string): number {
  return Number(month.slice(0, 4)) * 12 + (Number(month.slice(5, 7)) - 1)
}

function monthKey(index: number): string {
  const year = Math.floor(index / 12)
  const month = (index % 12) + 1
  return `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}`
}

/**
 * The months, oldest first, with the empty ones put back.
 *
 * **The gaps are filled, deliberately.** The API sends only the months in
 * which something happened, and a bar row that puts March next to November
 * draws them as neighbours -- a server that went quiet for eight months
 * would read as one that recorded steadily. The silence is the finding, so
 * it gets a row: the same width, a zero, and a sentence saying nothing was
 * recorded. Filling happens only *between* the first and last month that
 * carry something; a server is not silent in the months before it existed,
 * and inventing rows there would be inventing history rather than showing
 * a gap in it.
 *
 * Oldest first because the row is read as a timeline, and a timeline that
 * runs backwards has to be re-read before it can be understood. That is
 * the opposite of the Queue page's newest-first list, and for the opposite
 * reason: nobody scans this for one particular month, they look at its
 * shape.
 *
 * Bars are scaled against the busiest month's meeting count rather than
 * against its recorded time. Meetings are the figure the rest of the page
 * leads with, and two rows scaled by different quantities cannot be
 * compared to each other at a glance, which is all a bar is for.
 */
export function reportMonthRows(report: GuildReport): ReportMonthRow[] {
  const present = [...report.months].sort((a, b) => monthIndex(a.month) - monthIndex(b.month))
  if (present.length === 0) return []

  const byMonth = new Map<string, ReportMonth>()
  // Last one wins for a duplicated month. A duplicate is a defect
  // upstream; two rows with the same heading and different numbers would
  // put that defect on screen as though the server had lived the month
  // twice.
  for (const entry of present) byMonth.set(entry.month, entry)

  const firstIndex = monthIndex(present[0]!.month)
  const lastIndex = monthIndex(present[present.length - 1]!.month)
  const span = lastIndex - firstIndex + 1

  const keys
    = span <= REPORT_MONTH_FILL_LIMIT
      ? Array.from({ length: span }, (_, offset) => monthKey(firstIndex + offset))
      : [...byMonth.keys()].sort()

  const busiest = present.reduce((most, entry) => Math.max(most, entry.sessions), 0)

  return keys.map((key) => {
    const entry = byMonth.get(key)
    const sessions = entry?.sessions ?? 0
    const documented = entry?.documented ?? 0
    const recorded = entry ? formatDuration(entry.recorded_seconds) : NO_FIGURE
    const silent = entry === undefined
    const label = reportMonthLabel(key)

    // A month present in the payload with no sessions in it is not the
    // same as one the payload skipped, and it does not get the silent
    // row's wording -- the API said something about it, and this page
    // should not overwrite that with an assumption.
    const detail = silent
      ? `${label}: nothing was recorded in this server.`
      : `${label}: ${meetings(sessions)}, ${recorded} recorded, `
        + `${formatCount(documented)} written up.`

    const scaled = busiest > 0 ? sessions / busiest : 0
    return {
      month: key,
      label,
      sessions,
      documented,
      recorded,
      silent,
      extent: sessions > 0 ? Math.max(scaled, REPORT_MIN_BAR_EXTENT) : 0,
      detail,
    }
  })
}

/**
 * What the month rows are, and what was done about the gaps.
 *
 * Said above the rows rather than left to be worked out. A reader who
 * assumes the API sent every month will read a filled zero as a fact from
 * the database, and a reader who assumes it sent only the busy ones will
 * read an unfilled list as a steady run of months. Both are wrong in a way
 * the page can settle in one sentence.
 */
export function reportMonthsNote(report: GuildReport): string {
  const rows = reportMonthRows(report)
  if (rows.length === 0) {
    return 'No month in this server has any recording in it yet.'
  }
  // The two questions are asked in this order because a list too long to
  // fill has no silent rows in it at all, and would otherwise answer the
  // "nothing was skipped" branch by saying nothing was skipped -- which is
  // the exact opposite of what happened.
  const span = monthIndex(rows[rows.length - 1]!.month) - monthIndex(rows[0]!.month) + 1
  if (rows.length < span) {
    return (
      'Only the months in which something was recorded are listed, oldest first. This server\'s '
      + `history spans more than ${REPORT_MONTH_FILL_LIMIT / 12} years, which is too long to list `
      + 'month by month, so the quiet months between these are not shown as rows — two rows next '
      + 'to each other are not necessarily neighbouring months.'
    )
  }
  const silent = rows.filter((row) => row.silent).length
  if (silent === 0) {
    return (
      'Every month from the first recording in this server to the most recent one, oldest first. '
      + 'Something was recorded in each of them.'
    )
  }
  return (
    'Every month from the first recording in this server to the most recent one, oldest first. '
    + `The ${formatCount(silent)} ${plural(silent, 'month', 'months')} in which nothing was `
    + `recorded ${plural(silent, 'is', 'are')} listed with a zero rather than left out, so a quiet `
    + 'stretch reads as a gap instead of closing up.'
  )
}

/* -------------------------------------------------------------------- */
/* A server with nothing to report                                       */
/* -------------------------------------------------------------------- */

/**
 * Whether this server has ever been recorded at all.
 *
 * Deliberately stricter than `sessions === 0`. A server with no sessions
 * standing next to a hundred and sixty tracks is a defect upstream, and
 * showing the figures makes it visible where an empty state would hide it
 * behind an invitation to do something that has already been done. The
 * same reasoning `hasNothingRecorded` uses on the dashboard, applied to a
 * server rather than to a person.
 */
export function isReportEmpty(report: GuildReport): boolean {
  return (
    report.sessions === 0
    && report.tracks === 0
    && report.distinct_participants === 0
    && report.months.length === 0
    && report.first_session_at === null
    && report.last_session_at === null
  )
}

/** The empty state, as one sentence and a second saying what would fill
 *  it. A page of dashes and zeros for a server that has recorded nothing
 *  reads as a page that failed to load, and is also a claim -- that this
 *  server holds meetings of no length attended by nobody. */
export const REPORT_EMPTY_HEADING = 'Sturnus has not recorded anything in this server yet'

export const REPORT_EMPTY_NOTE =
  'There are no meetings here to report on, so this page shows no figures rather than a grid of '
  + 'zeros — a zero would be a measurement, and nothing has been measured. Once a meeting happens '
  + 'in a channel Sturnus watches and the people in it have consented, this page fills in: how '
  + 'many meetings, how long they ran, how many of them were written up, and how that changed '
  + 'month by month.'

/**
 * What this report is about, and what it is deliberately not about.
 *
 * On the page rather than only in this file. The payload holds no names
 * and no ids, and that is a decision rather than an oversight: a
 * per-person readout of who attended which meetings and who spoke for how
 * long is a means of monitoring conduct and performance at work, which is
 * a matter for a works council and not a console feature. Saying so is
 * also the honest answer to the reader who was about to ask where the
 * breakdown is.
 */
export const REPORT_SCOPE_NOTE =
  'Every figure here is about the server as a whole and never about the people in it. Sturnus '
  + 'sends this page no names and no ids, and there is no per-person breakdown behind it — how '
  + 'long one named person sat in meetings, or spoke in them, is a measure of that person rather '
  + 'than of this server, and that is not something a console should hand out. Counts of people '
  + 'are counts, and stop there.'

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
 * Named `describeReportError` rather than `describeError` for the same
 * reason `describeQueueError` is: everything under `app/utils` is
 * auto-imported into every component, and two exports sharing a name is a
 * build warning and a coin toss over which one a page actually gets.
 */
export function describeReportError(error: unknown): string {
  const status = statusOf(error)
  switch (status) {
    case 401:
      return 'Your session has ended. Sign in again to see this server’s figures.'
    case 403:
      return (
        'You do not administer this server. Administrators are the members holding the role named '
        + 'by that guild’s `admin_role_id`.'
      )
    case 404:
      // The API answers 404 both for a server that does not exist and for
      // one the caller does not administer, on purpose: it will not
      // confirm the existence of a server to somebody with no business
      // there. So this sentence has to cover both without guessing which.
      return (
        'Sturnus does not know this server, or you no longer administer it — it answers the same '
        + 'way to both. Reload the page; the list of servers is rebuilt from Discord.'
      )
    case null:
      return (
        'Could not reach the API. Nothing here is out of date on purpose; check the connection and '
        + 'retry.'
      )
    default:
      return `Sturnus answered ${status} and could not produce this server’s figures. Nothing is known about why.`
  }
}
