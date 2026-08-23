/**
 * The year grid and the intensity scale behind the calendar heatmap.
 *
 * A module rather than logic inside the component, because both are
 * decisions -- where a sparse list of days lands in a grid of weeks, and
 * how much recording counts as "a lot" -- and a decision embedded in a
 * template can only be tested by rendering one.
 *
 * ## The days here are UTC days, and that is deliberate
 *
 * `GET /api/calendar?year=` groups by UTC day and returns per-day
 * *aggregates*: a count, a sum of seconds, a number of people. Aggregates
 * cannot be re-bucketed. To show these cells in the viewer's own zone the
 * console would have to fetch every session of every day of the year and
 * add them up again itself -- 365 requests to move a handful of meetings
 * across a midnight.
 *
 * So the grid stays in UTC, and every label says so. **The cost is real
 * and it is not hidden:** a meeting at 00:30 in Berlin falls in the *previous*
 * UTC day, so somebody who met after midnight will find it one cell to the
 * left of where they remember it. The alternative -- relabelling UTC
 * aggregates as if they were local days -- would put the same meeting in
 * the wrong cell *and* claim otherwise, which is worse. The timeline
 * (`~/utils/timeline`) is where local clock time comes back, because a
 * session carries an instant that can honestly be converted.
 */
import { durationMessage } from './duration'
import type { Message } from './message'

/** One day as `GET /api/calendar?year=` returns it. */
export interface CalendarDay {
  /** `YYYY-MM-DD`, a UTC day. */
  date: string
  sessions: number
  total_duration_seconds: number
  participants: number
}

/** One square of the grid. */
export interface HeatmapCell {
  /**
   * The UTC day this square stands for, or `null` when it stands for no
   * day at all -- the padding that makes the first and last weeks whole.
   */
  date: string | null
  sessions: number
  totalDurationSeconds: number
  participants: number
  /** An index into {@link INTENSITY_LABEL_KEYS}; 0 means nothing was
   *  recorded. */
  intensity: number
}

/** Seven cells, Monday first. One column of the grid. */
export type HeatmapWeek = HeatmapCell[]

/**
 * One instant per row of the grid, Monday first.
 *
 * Monday first because the console's readers run their meetings on weekdays
 * and a week that ends at the weekend keeps those five together.
 *
 * These are *instants*, not names. The row headings used to be seven
 * English words written out here, which is seven words no reader of any
 * other language could ever have been shown -- and the shortened form the
 * grid actually has room for was `"Monday".slice(0, 3)`, which is a
 * sentence in English about English. A date whose weekday is the one wanted
 * costs nothing and lets `Intl` answer in whichever language is asking:
 * "Monday"/"Mon" or "Montag"/"Mo". The week chosen is an arbitrary one that
 * begins on a Monday; nothing about it is on screen but the weekday.
 */
export const WEEKDAY_INSTANTS: readonly Date[] = Array.from({ length: 7 }, (_, index) =>
  new Date(Date.UTC(2024, 0, 1 + index)),
)

/**
 * Where one intensity step ends and the next begins, in seconds of total
 * recording on a day.
 *
 * Half an hour, two hours, four hours: a single meeting, a morning, a day
 * that was mostly meetings. Four thresholds would be defensible too; what
 * is not defensible is more steps than a lightness ramp can separate, so
 * the scale stops at five.
 */
export const INTENSITY_THRESHOLDS_SECONDS: readonly number[] = [1800, 7200, 14400]

/**
 * A word for every step.
 *
 * This exists because **colour must never be the only channel carrying the
 * information.** The scale is one hue at five lightnesses -- which someone
 * who cannot distinguish red from green reads perfectly well, and someone
 * on a dim laptop screen in sunlight may not read at all. Every cell's
 * accessible name and tooltip names its step in words, so the grid is
 * legible with the colour thrown away entirely.
 *
 * Words in whichever language the reader chose, which is why these are keys
 * rather than the five English adjectives they used to be. A colour channel
 * replaced by a word only nine tenths of the readership can read is not
 * much of a replacement.
 */
export const INTENSITY_LABEL_KEYS: readonly string[] = [
  'calendar.intensityNone',
  'calendar.intensityLight',
  'calendar.intensityModerate',
  'calendar.intensityHeavy',
  'calendar.intensityBusiest',
]

/** The step a day's total recording falls into. */
export function intensityFor(totalDurationSeconds: number): number {
  if (totalDurationSeconds <= 0) return 0
  // Any recording at all lifts the day off the floor: a five-minute session
  // is not a blank day and the grid must not say it was.
  let step = 1
  for (const threshold of INTENSITY_THRESHOLDS_SECONDS) {
    if (totalDurationSeconds >= threshold) step += 1
  }
  return step
}

function blankCell(): HeatmapCell {
  return { date: null, sessions: 0, totalDurationSeconds: 0, participants: 0, intensity: 0 }
}

function isoOf(instant: Date): string {
  return instant.toISOString().slice(0, 10)
}

const DAY_MS = 86_400_000

/**
 * The whole year as columns of seven, Monday first.
 *
 * Three things this has to get right, and each of them is a test:
 *
 * - **A year that starts mid-week** gets blanks in front of it, not the
 *   previous December's days shown under this year's heading.
 * - **Days the API omitted** still get a cell. The endpoint sends an entry
 *   only for days that had recordings; a grid that skipped the others
 *   would slide every later day onto the wrong weekday.
 * - **The last week is padded too**, so the grid is a rectangle and the
 *   weekday row labels line up all the way across.
 */
export function buildYearGrid(year: number, days: readonly CalendarDay[]): HeatmapWeek[] {
  const byDate = new Map(days.map((day) => [day.date, day]))

  const firstMs = Date.UTC(year, 0, 1)
  const lastMs = Date.UTC(year, 11, 31)

  const cells: HeatmapCell[] = []

  // `getUTCDay()` is Sunday-first; the grid is Monday-first.
  const leadingBlanks = (new Date(firstMs).getUTCDay() + 6) % 7
  for (let i = 0; i < leadingBlanks; i += 1) cells.push(blankCell())

  // Stepping by a fixed 86 400 000 ms is only safe because these are UTC
  // instants: a UTC day is always exactly that long, where a local day
  // across a daylight-saving change is not.
  for (let ms = firstMs; ms <= lastMs; ms += DAY_MS) {
    const date = isoOf(new Date(ms))
    const day = byDate.get(date)
    const totalDurationSeconds = day?.total_duration_seconds ?? 0
    cells.push({
      date,
      sessions: day?.sessions ?? 0,
      totalDurationSeconds,
      participants: day?.participants ?? 0,
      intensity: intensityFor(totalDurationSeconds),
    })
  }

  while (cells.length % 7 !== 0) cells.push(blankCell())

  const weeks: HeatmapWeek[] = []
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7))
  return weeks
}

/** A month heading and how many week-columns it sits above. */
export interface MonthColumn {
  /** The month itself, as its first UTC instant, for the `shortMonth`
   *  format to name. It used to be the three English letters this module
   *  kept a table of. */
  at: Date
  /** Number of consecutive columns, so the heading can be a `colspan`. */
  span: number
}

/**
 * The headings along the top of the grid.
 *
 * A week that straddles a month boundary belongs to the month it started
 * in. The alternative -- switching at the first day of the new month --
 * would put "Feb" above a column that is six days of January.
 */
export function monthColumns(weeks: readonly HeatmapWeek[]): MonthColumn[] {
  const columns: { month: number; at: Date; span: number }[] = []

  for (const week of weeks) {
    const firstDated = week.find((cell) => cell.date !== null)
    if (!firstDated?.date) continue
    const year = Number(firstDated.date.slice(0, 4))
    const month = Number(firstDated.date.slice(5, 7))
    const previous = columns[columns.length - 1]
    if (previous && previous.month === month) {
      previous.span += 1
    } else {
      columns.push({ month, at: new Date(Date.UTC(year, month - 1, 1)), span: 1 })
    }
  }

  return columns.map(({ at, span }) => ({ at, span }))
}

/**
 * The day `deltaDays` away, never leaving the year on screen.
 *
 * This is what the arrow keys do. Clamping rather than wrapping or
 * spilling into the neighbouring year is the point: the grid only holds
 * this year, so a key press that walked off its edge would move focus to a
 * cell that is not there, and the browser would drop focus to the document
 * -- which is how a keyboard user loses their place entirely.
 */
export function shiftWithinYear(date: string, deltaDays: number, year: number): string {
  const moved = Date.parse(`${date}T00:00:00Z`) + deltaDays * DAY_MS
  const first = Date.UTC(year, 0, 1)
  const last = Date.UTC(year, 11, 31)
  return isoOf(new Date(Math.min(Math.max(moved, first), last)))
}

/**
 * `2026-08-21` as the UTC instant that day begins at.
 *
 * Built with `Date.UTC` from the parts rather than by parsing and reading
 * back with local getters, which turns `2026-08-21` into 20 August for
 * anybody west of Greenwich -- exactly the class of bug this whole module
 * is careful about. Every datetime format that renders one of these is
 * pinned to UTC for the second half of the same reason.
 *
 * This replaces `formatIsoDate` and `weekdayOf`, which wrote out `21 August
 * 2026` and `Friday` from tables of English words kept in this file. The
 * words are `Intl`'s now; what is left here is the only part that was ever
 * a decision -- which instant a UTC day is.
 */
export function dayInstant(date: string): Date {
  const [year, month, day] = date.split('-')
  return new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)))
}

/**
 * The sentence a cell says -- as its accessible name, and as its tooltip.
 *
 * One message for both, on purpose. A tooltip a screen reader never reaches
 * and a label a sighted viewer never sees would drift apart within a
 * release, and then the grid would be telling two different stories.
 *
 * `null` for a padding square, which stands for no day at all. It has no
 * name because there is nothing to name; nothing renders one either, and a
 * key saying "no day" would be a sentence nobody can ever read.
 */
export function describeCell(cell: HeatmapCell): Message | null {
  if (!cell.date) return null

  const date = { at: dayInstant(cell.date), format: 'fullDate' }
  if (cell.sessions === 0) return { key: 'calendar.cellNothing', params: { date } }

  return {
    key: 'calendar.cellRecorded',
    params: {
      date,
      sessions: { key: 'calendar.sessionCount', params: { count: cell.sessions } },
      duration: durationMessage(cell.totalDurationSeconds),
      people: { key: 'calendar.personCount', params: { count: cell.participants } },
      activity: {
        key: 'calendar.activityStep',
        params: {
          step: { key: INTENSITY_LABEL_KEYS[cell.intensity]! },
          index: cell.intensity,
          max: INTENSITY_LABEL_KEYS.length - 1,
        },
      },
    },
  }
}
