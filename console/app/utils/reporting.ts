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
import { figureDuration, figureMoment } from '~/utils/format'
import { NOT_MEASURED, type Instant, type Message } from '~/utils/message'

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

/**
 * `2026-08` as the UTC instant that month begins at.
 *
 * This replaces `reportMonthLabel`, which wrote `August 2026` out of a
 * table of English month names kept in this file. The comment defending
 * that table said `Intl` formats for the runtime's locale and would
 * disagree between a server render and a browser -- true of an *ambient*
 * locale, and no longer true of a chosen one that travels in a cookie. The
 * month names are `Intl`'s now, through the `monthYear` format, which is
 * pinned to UTC so that the instant below cannot slide into July on the way
 * to the screen.
 *
 * A month this cannot read has already been dropped: `parseGuildReport`
 * refuses anything that is not `YYYY-MM`, and every other key here is one
 * `monthKey` produced.
 */
function monthInstant(month: string): Date {
  return new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)) - 1, 1))
}

/**
 * Nothing here counts by hand any more.
 *
 * This module used to keep a `plural(count, one, many)` and a
 * `meetings(count)` beside it, and every sentence below was assembled by
 * calling them -- which is an English decision about English words, made in
 * a module German cannot reach. The counting is now the locale file's: a
 * message carries a `count`, and each language says what that does to the
 * sentence. Where a sentence has two counts in it, the one that governs the
 * verb is the `count`; the other is a value like any other.
 */

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
  /** A translation key, resolved by whoever renders this. Named `…Key` so
   *  that nothing puts it on screen by mistake. */
  labelKey: string
  /** A quantity, a length, or the absence of one. `null` is what an em
   *  dash used to be: a distinction that lived inside a string, where
   *  nothing downstream could tell it from a value. */
  value: Message | number | null
  /**
   * The lines under the figure, one message per sentence. Never empty for a
   * missing value: an em dash with nothing beside it reads as "still
   * loading", which is the one thing this page is not doing.
   *
   * A list rather than one string, because several of these notes are two
   * or three sentences and were built by adding one to another. A sentence
   * glued to the end of a sentence carries the order it was glued in, and
   * whether German wants the caveat first is not this module's to decide.
   */
  note: Message[]
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
export function reportDocumentedLine(report: GuildReport): Message[] {
  if (report.sessions <= 0) return [{ key: 'admin.reporting.documentedNothing' }]

  const said: Message[] = []
  if (report.documented >= report.sessions) {
    said.push({ key: 'admin.reporting.documentedAll', params: { count: report.sessions } })
  }
  else {
    const missing = report.sessions - report.documented
    said.push({
      key: 'admin.reporting.documentedPartial',
      params: {
        // The count that governs the verb, and therefore the sentence's
        // plural form: "the other one was" against "the other three were".
        count: missing,
        documented: report.documented,
        sessions: report.sessions,
        share: reportDocumentedShare(report) ?? 0,
      },
    })
  }
  if (report.open_sessions > 0) {
    said.push({ key: 'admin.reporting.documentedOpen', params: { count: report.open_sessions } })
  }
  return said
}

/**
 * What the recorded total covers, and what it leaves out.
 *
 * Said whether or not anything is open. A total whose exclusions are
 * mentioned only when they bite is a total whose scope the reader has to
 * infer from its own silence.
 */
export function reportRecordedLine(report: GuildReport): Message[] {
  if (report.open_sessions > 0) {
    return [
      { key: 'admin.reporting.recordedScope' },
      { key: 'admin.reporting.recordedOpenExcluded', params: { count: report.open_sessions } },
    ]
  }
  return [{ key: 'admin.reporting.recordedAllEnded' }]
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
export function reportOpenSessionsLine(report: GuildReport): Message[] {
  if (report.open_sessions <= 0) return [{ key: 'admin.reporting.openNone' }]
  return [
    { key: 'admin.reporting.openSome', params: { count: report.open_sessions } },
    { key: 'admin.reporting.openAmbiguous' },
  ]
}

/** The reason a figure is absent, said as the absence it is rather than
 *  left as a dash the reader has to account for. The second sentence is the
 *  same for all four, which is why it is a key of its own rather than four
 *  copies of one sentence that would drift apart one edit at a time. */
function noFinishedMeetings(reasonKey: string): Message[] {
  return [{ key: reasonKey }, { key: 'admin.reporting.absentNotZero' }]
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
      labelKey: 'admin.reporting.sessionsLabel',
      value: report.sessions,
      note: [reportSpanLine(report)],
      tone: 'plain',
    },
    {
      key: 'documented',
      labelKey: 'admin.reporting.documentedLabel',
      value: report.documented,
      note: reportDocumentedLine(report),
      tone: 'plain',
    },
    {
      key: 'recorded',
      labelKey: 'admin.reporting.recordedLabel',
      value: figureDuration(report.recorded_seconds),
      note: reportRecordedLine(report),
      tone: report.recorded_seconds === null ? 'absent' : 'plain',
    },
    {
      key: 'speech',
      labelKey: 'admin.reporting.speechLabel',
      value: figureDuration(report.speech_seconds),
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
      labelKey: 'admin.reporting.averageDurationLabel',
      value: figureDuration(average),
      note:
        average === null
          ? noFinishedMeetings('admin.reporting.absentAverageDuration')
          : [{ key: 'admin.reporting.averageDurationNote' }],
      tone: average === null ? 'absent' : 'plain',
    },
    {
      key: 'longest-duration',
      labelKey: 'admin.reporting.longestDurationLabel',
      value: figureDuration(longest),
      note:
        longest === null
          ? noFinishedMeetings('admin.reporting.absentLongestDuration')
          : [{ key: 'admin.reporting.longestDurationNote' }],
      tone: longest === null ? 'absent' : 'plain',
    },
    {
      key: 'average-participants',
      // To one decimal, and rounded here rather than on screen: a mean of
      // 4.25 people is a precision the payload does not have. The trailing
      // `.0` that used to be trimmed by hand goes on its own now -- the
      // locale's number format does not write digits nobody asked for --
      // and German gets its comma with it.
      labelKey: 'admin.reporting.averageParticipantsLabel',
      value: perMeeting === null ? null : Math.round(perMeeting * 10) / 10,
      note:
        perMeeting === null
          ? noFinishedMeetings('admin.reporting.absentAverageParticipants')
          : [{ key: 'admin.reporting.averageParticipantsNote' }],
      tone: perMeeting === null ? 'absent' : 'plain',
    },
    {
      key: 'largest-meeting',
      labelKey: 'admin.reporting.largestMeetingLabel',
      value: largest,
      note:
        largest === null
          ? noFinishedMeetings('admin.reporting.absentLargestMeeting')
          : [{ key: 'admin.reporting.largestMeetingNote' }],
      tone: largest === null ? 'absent' : 'plain',
    },
    {
      key: 'participants',
      labelKey: 'admin.reporting.participantsLabel',
      value: report.distinct_participants,
      note: [{ key: 'admin.reporting.participantsNote' }],
      tone: 'plain',
    },
    {
      key: 'open',
      labelKey: 'admin.reporting.openLabel',
      value: report.open_sessions,
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
export function reportSpeechCaveat(report: GuildReport): Message[] {
  const { tracks, unmeasured_tracks: unmeasured } = report

  if (tracks <= 0) return [{ key: 'admin.reporting.speechNoTracks' }]

  if (unmeasured <= 0) {
    return [{ key: 'admin.reporting.speechAllMeasured', params: { count: tracks } }]
  }

  const measured = Math.max(0, tracks - unmeasured)
  if (measured === 0) {
    return [{ key: 'admin.reporting.speechNoneMeasured', params: { count: tracks } }]
  }

  return [
    // The count that governs the verb is the unmeasured one -- "one was
    // never measured" against "forty were" -- and the total is a value
    // alongside it.
    {
      key: 'admin.reporting.speechPartlyMeasured',
      params: { count: unmeasured, tracks },
    },
    { key: 'admin.reporting.speechPartlyMeasuredTotal', params: { count: measured } },
    { key: 'admin.reporting.speechNotQuiet' },
  ]
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
export function reportTimezoneNote(report: GuildReport): Message {
  if (!report.timezone) return { key: 'admin.reporting.timezoneUnknown' }
  // An IANA zone name is not a word in any language -- `Europe/Berlin` is
  // `Europe/Berlin` -- so it travels as the string it is.
  return { key: 'admin.reporting.timezoneKnown', params: { zone: report.timezone } }
}

export interface ReportCaveat {
  key: string
  labelKey: string
  text: Message[]
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
      labelKey: 'admin.reporting.caveatSpeechLabel',
      text: reportSpeechCaveat(report),
    },
    {
      key: 'timezone',
      labelKey: 'admin.reporting.caveatTimezoneLabel',
      text: [reportTimezoneNote(report)],
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
export function reportSpanLine(report: GuildReport): Message {
  const first = report.first_session_at
  const last = report.last_session_at

  if (!first && !last) return { key: 'admin.reporting.spanNothing' }
  if (first && last) {
    if (first === last) {
      return { key: 'admin.reporting.spanOne', params: { at: moment(first) } }
    }
    return {
      key: 'admin.reporting.spanBetween',
      params: { from: moment(first), to: moment(last) },
    }
  }
  return { key: 'admin.reporting.spanPartial', params: { at: moment((first ?? last)!) } }
}

/** An instant, or the em dash that stands for one the API sent in a shape
 *  nothing can read. `figureMoment` already makes that judgement for the
 *  dashboard; making it again here would be a second answer to one
 *  question. */
function moment(iso: string): Instant | string {
  return figureMoment(iso) ?? NOT_MEASURED
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
  /** The month as its first UTC instant, for the `monthYear` format to
   *  name in whichever language is reading. */
  at: Date
  sessions: number
  documented: number
  /** How long was recorded, or `null` where the API had no total to give. */
  recorded: Message | null
  /** True for a month this module added because the payload skipped it. */
  silent: boolean
  /** 0 to 1, against the busiest month in the list. The bar's width, and
   *  nothing else -- it is deliberately not a percentage anybody reads. */
  extent: number
  /** The row said as a sentence, for the reader who is listening to the
   *  page rather than looking at it. A bar with no text is a bar only its
   *  author can read. */
  detail: Message
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
    const recorded = entry ? figureDuration(entry.recorded_seconds) : null
    const silent = entry === undefined
    const at = monthInstant(key)
    const month = { at, format: 'monthYear' }

    // A month present in the payload with no sessions in it is not the
    // same as one the payload skipped, and it does not get the silent
    // row's wording -- the API said something about it, and this page
    // should not overwrite that with an assumption.
    const detail: Message = silent
      ? { key: 'admin.reporting.monthSilent', params: { month } }
      : {
          key: 'admin.reporting.monthDetail',
          params: { month, count: sessions, recorded: recorded ?? NOT_MEASURED, documented },
        }

    const scaled = busiest > 0 ? sessions / busiest : 0
    return {
      month: key,
      at,
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
export function reportMonthsNote(report: GuildReport): Message[] {
  const rows = reportMonthRows(report)
  if (rows.length === 0) return [{ key: 'admin.reporting.monthsNothing' }]

  // The two questions are asked in this order because a list too long to
  // fill has no silent rows in it at all, and would otherwise answer the
  // "nothing was skipped" branch by saying nothing was skipped -- which is
  // the exact opposite of what happened.
  const span = monthIndex(rows[rows.length - 1]!.month) - monthIndex(rows[0]!.month) + 1
  if (rows.length < span) {
    return [
      {
        key: 'admin.reporting.monthsTooLong',
        params: { years: REPORT_MONTH_FILL_LIMIT / 12 },
      },
    ]
  }
  const silent = rows.filter((row) => row.silent).length
  return [
    { key: 'admin.reporting.monthsOldestFirst' },
    silent === 0
      ? { key: 'admin.reporting.monthsAllBusy' }
      : { key: 'admin.reporting.monthsSomeSilent', params: { count: silent } },
  ]
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
export const REPORT_EMPTY_HEADING_KEY = 'admin.reporting.emptyHeading'

export const REPORT_EMPTY_NOTE_KEY = 'admin.reporting.emptyNote'

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
export const REPORT_SCOPE_NOTE_KEY = 'admin.reporting.scopeNote'

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
export function describeReportError(error: unknown): Message {
  const status = statusOf(error)
  switch (status) {
    case 401:
      return { key: 'admin.reporting.errorSession' }
    case 403:
      return { key: 'admin.reporting.errorNotAdmin' }
    case 404:
      // The API answers 404 both for a server that does not exist and for
      // one the caller does not administer, on purpose: it will not
      // confirm the existence of a server to somebody with no business
      // there. So this sentence has to cover both without guessing which.
      return { key: 'admin.reporting.errorUnknownGuild' }
    case null:
      return { key: 'admin.reporting.errorUnreachable' }
    default:
      // A status is a number without being a quantity, so it travels as a
      // string: `useSay` would otherwise group it, and there is no such
      // status as 1,000.
      return { key: 'admin.reporting.errorStatus', params: { status: String(status) } }
  }
}
