/**
 * A calendar in this console's own palette, and the instant it emits.
 *
 * There is no date picker in this console at all. `/settings` fakes one
 * with a text input, `/recordings` has two bare `date` fields, and the
 * consent page has a `datetime-local` — three shapes for one job, none of
 * which can be styled and one of which renders as a plain text box in
 * browsers that do not implement it.
 *
 * ## The instant is the whole point
 *
 * `POST /consents/.../revoke` answers 400 to an ISO-8601 string with no
 * offset. `~/utils/effectiveInstant` already worked that out, argued for
 * it in prose, and got the hard part right: the offset attached is the
 * offset **of the chosen wall-clock moment**, not of today, so an
 * administrator in Berlin choosing a January instant in July gets `+01:00`
 * rather than `+02:00`. An hour is exactly the error nobody notices until
 * a recording lands on the wrong side of a withdrawal.
 *
 * So this module writes no second answer to that question. `instantFrom`
 * and `localFrom` are two lines each and both of them are calls into
 * `effectiveInstant`. A second implementation would be a second thing to
 * get right at every daylight-saving boundary, and the two would disagree
 * first on the page that matters most.
 *
 * ## The rest is grid arithmetic
 *
 * Which day is in which cell, what an arrow key does to a date, where a
 * month boundary falls. All of it off-by-one territory, and all of it
 * invisible in a rendered calendar — a grid that quietly starts on Sunday
 * looks exactly like one that starts on Monday until you read the column
 * headings.
 *
 * Weeks begin on Monday, and the weekday headings are `Intl`'s. Both are
 * `heatmap.ts`'s decisions, reused rather than re-taken: two calendars in
 * one console disagreeing about where a week starts is worse than either
 * choice on its own.
 */
import {
  isoFromLocalInput,
  localInputFromIso,
  localOffsetMinutes,
  offsetLabel,
} from './effectiveInstant'
import { dayInstant } from './heatmap'
import type { Message } from './message'

/** A UTC calendar day, `YYYY-MM-DD`. */
export type Day = string
/** A calendar month, `YYYY-MM`. */
export type Month = string

/**
 * The time a first choice lands on.
 *
 * Midnight, because clicking a date means the date. Seeding the current
 * clock time instead would emit an instant nobody chose and — worse —
 * nobody can see they chose: the calendar shows a day and the value
 * carries a time of 14:37.
 */
export const DEFAULT_TIME = '00:00'

const LOCAL = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/
const DAY = /^(\d{4})-(\d{2})-(\d{2})$/

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

function partsOf(day: Day): [number, number, number] | null {
  const match = DAY.exec(day)
  if (!match) return null
  const [, year, month, date] = match as unknown as string[]
  const parsed = new Date(Date.UTC(Number(year), Number(month) - 1, Number(date)))
  // `2026-02-30` parses digit by digit; `Date.UTC` rolls it into March
  // rather than refusing, so the roll-forward is what is detected.
  if (
    parsed.getUTCFullYear() !== Number(year)
    || parsed.getUTCMonth() !== Number(month) - 1
    || parsed.getUTCDate() !== Number(date)
  ) {
    return null
  }
  return [Number(year), Number(month), Number(date)]
}

function dayString(at: Date): Day {
  return `${at.getUTCFullYear()}-${pad(at.getUTCMonth() + 1)}-${pad(at.getUTCDate())}`
}

/** How many days a month holds. Day zero of the next month is the last of
 *  this one, which is the one piece of `Date` arithmetic that is easier
 *  than the thing it replaces. */
function daysInMonth(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate()
}

/* -------------------------------------------------------------------- */
/* The instant — borrowed entire from `effectiveInstant`                 */
/* -------------------------------------------------------------------- */

/**
 * A wall-clock value as an ISO-8601 instant carrying its own offset.
 *
 * `null` for anything that is not a moment: a half-typed value, or
 * `2026-02-30`, which some browsers' own validation lets through.
 *
 * Never `Z`, even where the offset really is zero — `effectiveInstant`
 * writes `+00:00` instead, on the grounds that a formatter with no special
 * cases cannot get its special case wrong.
 */
export function instantFrom(local: string, reference: Date = new Date()): string | null {
  return isoFromLocalInput(local, localOffsetMinutes(local, reference))
}

/**
 * An instant as the wall clock it is in the browser's zone.
 *
 * The direction an API answer arrives in. The offset used is the one that
 * applies at that instant rather than the one that applies now, for the
 * same daylight-saving reason as everything else here.
 */
export function localFrom(iso: string): string | null {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return null
  return localInputFromIso(iso, at.getTimezoneOffset())
}

/** The offset about to be attached, written the way ISO-8601 writes it. */
export function offsetOfLocal(local: string, reference: Date = new Date()): string {
  return offsetLabel(localOffsetMinutes(local, reference))
}

/**
 * What the control says it is about to send.
 *
 * The instant itself, offset and all, quoted rather than reformatted. This
 * is a console for administrators and the string that goes over the wire
 * is the useful thing to show — a control whose entire reason to exist is
 * the offset should let somebody see the offset.
 */
export function describeChoice(local: string, reference: Date = new Date()): Message {
  if (local.trim() === '') return { key: 'ui.datePicker.nothingChosen' }
  const iso = instantFrom(local, reference)
  if (!iso) return { key: 'ui.datePicker.unreadable' }
  return { key: 'ui.datePicker.sends', params: { instant: iso } }
}

/* -------------------------------------------------------------------- */
/* The day and the time, which are two different things                  */
/* -------------------------------------------------------------------- */

/** The day part of a wall-clock value, or `null` when there is not one. */
export function dayOfLocal(local: string): Day | null {
  const match = LOCAL.exec(local.trim())
  return match ? (match[1] ?? null) : null
}

/**
 * The same value on a different day.
 *
 * The time survives, because clicking a date in a calendar is not setting
 * a clock: somebody who typed 14:30 and then changed their mind about the
 * day meant to keep 14:30.
 */
export function withDay(local: string, day: Day): string {
  const match = LOCAL.exec(local.trim())
  return `${day}T${match?.[2] ?? DEFAULT_TIME}`
}

/* -------------------------------------------------------------------- */
/* The month and its grid                                                */
/* -------------------------------------------------------------------- */

export function monthOf(day: Day): Month {
  return day.slice(0, 7)
}

export function shiftMonth(month: Month, delta: number): Month {
  const [year, index] = month.split('-').map(Number)
  const at = new Date(Date.UTC(year ?? 1970, (index ?? 1) - 1 + delta, 1))
  return `${at.getUTCFullYear()}-${pad(at.getUTCMonth() + 1)}`
}

/** The instant a month begins at, so its name comes from `Intl` rather
 *  than from a table of English words kept in a module. */
export function monthInstant(month: Month): Date {
  return dayInstant(`${month}-01`)
}

export interface CalendarCell {
  day: Day
  dayOfMonth: number
  /** False for the days that lead into the grid and trail out of it. They
   *  are still real days and still clickable — a calendar that refuses the
   *  1st of next month because it sits in the last row is a calendar you
   *  have to page in order to use. */
  inMonth: boolean
  chosen: boolean
  today: boolean
  disabled: boolean
}

export interface CalendarMarks {
  chosen?: Day | null
  today?: Day | null
  min?: Day | null
  max?: Day | null
}

/**
 * Six weeks of seven days, Monday first.
 *
 * Always six, never five and never seven. A grid that changes height from
 * month to month makes everything below it jump when somebody pages, and
 * the button they were about to press moves out from under the pointer.
 */
export function monthGrid(month: Month, marks: CalendarMarks): CalendarCell[][] {
  const [year, index] = month.split('-').map(Number)
  const first = new Date(Date.UTC(year ?? 1970, (index ?? 1) - 1, 1))
  // `getUTCDay()` counts from Sunday; this grid counts from Monday.
  const lead = (first.getUTCDay() + 6) % 7
  const start = first.getTime() - lead * 86_400_000

  const weeks: CalendarCell[][] = []
  for (let week = 0; week < 6; week += 1) {
    const row: CalendarCell[] = []
    for (let column = 0; column < 7; column += 1) {
      const at = new Date(start + (week * 7 + column) * 86_400_000)
      const day = dayString(at)
      row.push({
        day,
        dayOfMonth: at.getUTCDate(),
        inMonth: day.startsWith(month),
        chosen: day === marks.chosen,
        today: day === marks.today,
        disabled:
          (marks.min != null && day < marks.min) || (marks.max != null && day > marks.max),
      })
    }
    weeks.push(row)
  }
  return weeks
}

/* -------------------------------------------------------------------- */
/* The keyboard                                                          */
/* -------------------------------------------------------------------- */

/**
 * Where a key moves the cursor in the grid, or `null` for a key this grid
 * has no opinion about.
 *
 * `null` is what lets the component leave Enter, Escape and Tab alone
 * rather than swallowing them inside a calendar — a grid that eats Tab is
 * a keyboard trap.
 *
 * Paging by month clamps rather than spilling: `31 March` a month back is
 * `28 February`, not `3 March`, which is what happens when a month is
 * decremented and the day is left where it was.
 */
export function moveDay(day: Day, key: string): Day | null {
  const parts = partsOf(day)
  if (!parts) return null
  const [year, month, date] = parts
  const at = Date.UTC(year, month - 1, date)

  switch (key) {
    case 'ArrowLeft':
      return dayString(new Date(at - 86_400_000))
    case 'ArrowRight':
      return dayString(new Date(at + 86_400_000))
    case 'ArrowUp':
      return dayString(new Date(at - 7 * 86_400_000))
    case 'ArrowDown':
      return dayString(new Date(at + 7 * 86_400_000))
    case 'PageUp':
    case 'PageDown': {
      const target = new Date(Date.UTC(year, month - 1 + (key === 'PageUp' ? -1 : 1), 1))
      const room = daysInMonth(target.getUTCFullYear(), target.getUTCMonth() + 1)
      return dayString(
        new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth(), Math.min(date, room))),
      )
    }
    case 'Home':
    case 'End': {
      const weekday = (new Date(at).getUTCDay() + 6) % 7
      const shift = key === 'Home' ? -weekday : 6 - weekday
      return dayString(new Date(at + shift * 86_400_000))
    }
    default:
      return null
  }
}

/** A day pulled back inside the bounds, if it fell outside them. ISO days
 *  sort the way they read, so this is a string comparison and cannot go
 *  wrong at a month boundary. */
export function clampDay(day: Day, min: Day | null = null, max: Day | null = null): Day {
  if (min != null && day < min) return min
  if (max != null && day > max) return max
  return day
}
