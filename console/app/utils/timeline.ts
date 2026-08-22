/**
 * Where a day's sessions sit on a 24-hour axis.
 *
 * A list sorted by time is not a timeline. It says one meeting came before
 * another; it never says one was at nine in the morning and the other at
 * nine at night. Turning an instant into a position is the whole point of
 * the view, so it lives here rather than inside a `:style` binding where
 * nothing can test it.
 *
 * ## The window is the UTC day; the labels are local
 *
 * `GET /api/calendar/{YYYY-MM-DD}` returns the sessions the API bucketed
 * into one **UTC** day (see `~/utils/heatmap` for why the server cannot do
 * better). So the axis spans exactly that UTC day -- midnight to midnight,
 * always exactly 24 hours.
 *
 * The tick *labels*, however, are formatted from the instants this module
 * hands back, in the viewer's own zone. So in Berlin the axis reads 02:00,
 * 05:00, 08:00 ... rather than 00:00, 03:00, 06:00, and a meeting the
 * viewer remembers starting at 11:00 is labelled 11:00.
 *
 * **What that costs:** for anybody outside UTC the axis does not begin at
 * their midnight, and the panel has to say so. The alternative -- a fixed
 * 00:00-to-24:00 *local* axis -- is worse in a way that is easy to miss: a
 * session at 23:30 UTC is 01:30 in Berlin, so it would be drawn at the far
 * *left* of a day whose other meetings it came after. A timeline that puts
 * the last meeting of the day first is not a timeline. Anchoring the
 * window to the bucket the data actually came from makes that impossible.
 *
 * Each tick carries its instant rather than an hour number for one further
 * reason: a day containing a daylight-saving change has two ticks an hour
 * apart in UTC that carry the same local clock time, and formatting each
 * instant separately renders that correctly instead of hiding it.
 */

/** One session as `GET /api/calendar/{date}` returns it. */
export interface DaySession {
  /** A string, not a number. A Discord snowflake exceeds JavaScript's safe
   *  integer range, where a JSON number silently loses its last digits. */
  id: string
  /** An ISO instant. */
  started_at: string
  /** Nullable: a session still running, or one whose worker died before it
   *  wrote a length. Not the same thing as zero. */
  duration_seconds: number | null
  channel_id: string
  channel_name: string | null
}

/** One bar, ready to be positioned as a percentage of the axis. */
export interface TimelineBar {
  id: string
  /** What to write on the bar. Never empty -- see {@link layOutDay}. */
  channel: string
  startedAt: Date
  durationSeconds: number | null
  /** How far along the axis the bar starts, 0 to 1. */
  offset: number
  /** How much of the axis the bar covers, 0 to 1. */
  extent: number
  /** Which row the bar sits in, so meetings that overlap do not hide each
   *  other. */
  lane: number
}

const DAY_MS = 86_400_000

/**
 * The narrowest a bar may be drawn, as a fraction of the day.
 *
 * Half a percent is roughly seven minutes. A two-minute session is 0.14%
 * of a day: drawn to scale it is a hairline nobody can hover, focus or
 * notice. Widening it lies slightly about the length -- which is why the
 * length is also written in the bar's accessible name, where it is exact.
 */
export const MIN_BAR_EXTENT = 0.005

/** The instant the day's axis begins: the UTC midnight the API grouped by. */
export function dayWindowStart(date: string): Date {
  return new Date(`${date}T00:00:00Z`)
}

/**
 * The day's sessions as bars, in start order, packed into lanes.
 *
 * Sturnus records per channel, so two meetings genuinely do run at the same
 * moment. In one lane the later would paint over the earlier and a viewer
 * would count one meeting where there were two, so overlapping bars get a
 * lane each -- greedily, first lane that is free.
 */
export function layOutDay(date: string, sessions: readonly DaySession[]): TimelineBar[] {
  const windowStartMs = dayWindowStart(date).getTime()

  const ordered = [...sessions].sort(
    (a, b) => Date.parse(a.started_at) - Date.parse(b.started_at),
  )

  // Lane occupancy is tracked in absolute milliseconds rather than in
  // fractions of the axis: integers compare exactly, and a meeting that
  // ends at the very moment the next begins should share its lane rather
  // than lose it to a rounding error in the last bit of a double.
  const laneEndsMs: number[] = []

  return ordered.map((session) => {
    const startedAt = new Date(session.started_at)
    const startedMs = startedAt.getTime()
    const seconds = session.duration_seconds

    const offset = Math.min(Math.max((startedMs - windowStartMs) / DAY_MS, 0), 1)
    const scaled = seconds === null || seconds === undefined ? 0 : (seconds * 1000) / DAY_MS
    // Clamped at the right edge: a meeting that ran past midnight continues
    // into the next UTC day, which has its own cell and its own timeline. A
    // bar running off the end would claim this day held more than it did.
    const extent = Math.min(Math.max(scaled, MIN_BAR_EXTENT), 1 - offset)

    // A missing length is unknown, not infinite. Reserving the rest of the
    // day for it would push every later meeting into a lane of its own.
    const endedMs = startedMs + (seconds ?? 0) * 1000
    let lane = laneEndsMs.findIndex((end) => end <= startedMs)
    if (lane === -1) {
      lane = laneEndsMs.length
      laneEndsMs.push(endedMs)
    } else {
      laneEndsMs[lane] = endedMs
    }

    return {
      id: session.id,
      // A channel deleted after the recording has no name left to give, and
      // an unlabelled bar is a bar that says only "something happened".
      channel: session.channel_name ?? `Channel ${session.channel_id}`,
      startedAt,
      durationSeconds: seconds ?? null,
      offset,
      extent,
      lane,
    }
  })
}

/** One mark along the axis. */
export interface AxisTick {
  /** Position along the axis, 0 to 1. */
  offset: number
  /** The instant, so the label can be formatted in the viewer's zone. */
  at: Date
}

/**
 * Marks along the axis, from one UTC midnight to the next inclusive.
 *
 * Both ends are always marked, even when the interval does not divide 24 --
 * an axis whose last label falls short of its right edge invites reading
 * the final bar against the wrong hour.
 */
export function axisTicks(date: string, everyHours = 3): AxisTick[] {
  const startMs = dayWindowStart(date).getTime()
  const ticks: AxisTick[] = []

  for (let hour = 0; hour <= 24; hour += everyHours) {
    ticks.push({ offset: hour / 24, at: new Date(startMs + hour * 3_600_000) })
  }
  if (ticks[ticks.length - 1]!.offset !== 1) {
    ticks.push({ offset: 1, at: new Date(startMs + DAY_MS) })
  }

  return ticks
}

/** What a day added up to. */
export interface DaySummary {
  sessions: number
  totalDurationSeconds: number
  /** How many of those sessions had no length recorded. Counted rather than
   *  summed as zero, so the total can say when it is incomplete. */
  unknownDurations: number
}

export function summarise(sessions: readonly DaySession[]): DaySummary {
  let totalDurationSeconds = 0
  let unknownDurations = 0

  for (const session of sessions) {
    if (session.duration_seconds === null || session.duration_seconds === undefined) {
      unknownDurations += 1
    } else {
      totalDurationSeconds += session.duration_seconds
    }
  }

  return { sessions: sessions.length, totalDurationSeconds, unknownDurations }
}
