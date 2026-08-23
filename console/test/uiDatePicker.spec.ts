/**
 * A calendar, and the one thing a calendar in this console must never do.
 *
 * It must never emit a naive instant. `POST .../revoke` answers 400 to an
 * ISO-8601 string with no offset, the settings page currently fakes a date
 * picker with a text input for exactly that reason, and the arithmetic
 * that gets a wall-clock moment to the API intact was already written and
 * argued for in `~/utils/effectiveInstant`. So this module writes no
 * second answer to that question: it calls that one, and what is tested
 * here is that it calls it correctly and that the offset it attaches is
 * the offset of *the chosen moment* rather than of today.
 *
 * The rest is grid arithmetic — which day sits in which cell, what an
 * arrow key does to a date, where a month boundary falls — and every one
 * of those is off-by-one territory that a rendered calendar hides
 * beautifully.
 */
import { describe, expect, it } from 'vitest'

import {
  DEFAULT_TIME,
  clampDay,
  dayOfLocal,
  describeChoice,
  instantFrom,
  localFrom,
  monthGrid,
  monthInstant,
  monthOf,
  moveDay,
  offsetOfLocal,
  shapeOf,
  shiftMonth,
  withDay,
} from '../app/utils/uiDatePicker'

/** The instant a host machine reads a wall-clock value as, whatever zone
 *  it is sitting in. The yardstick `effectiveInstant.spec.ts` uses too. */
function asHostSeesIt(local: string): number {
  const [date, clock] = local.split('T')
  const [year, month, day] = date!.split('-').map(Number)
  const [hour, minute] = clock!.split(':').map(Number)
  return new Date(year!, month! - 1, day!, hour!, minute!).getTime()
}

describe('the instant that leaves the control', () => {
  it('carries an explicit offset, always', () => {
    const iso = instantFrom('2026-08-23T14:30')!
    expect(iso).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$/)
  })

  it('never writes `Z`, not even where the offset really is zero', () => {
    // `+00:00` and `Z` are both legal and one of them is a special case.
    // A formatter with no special cases cannot get its special case wrong,
    // which is the rule `effectiveInstant` already set and this follows.
    expect(instantFrom('2026-08-23T14:30')).not.toContain('Z')
  })

  it('names the same moment the host machine names', () => {
    // The whole of the daylight-saving correctness, borrowed wholesale.
    // A January value gets January's offset and a July value gets July's,
    // whichever side of the clock change this test happens to run on.
    for (const local of ['2026-01-15T09:00', '2026-07-15T09:00']) {
      expect(new Date(instantFrom(local)!).getTime()).toBe(asHostSeesIt(local))
    }
  })

  it('is nothing at all for a date that does not exist', () => {
    // `2026-02-30` gets through some browsers' own validation and `Date`
    // rolls it silently into March.
    expect(instantFrom('2026-02-30T09:00')).toBeNull()
    expect(instantFrom('')).toBeNull()
    expect(instantFrom('23/08/2026')).toBeNull()
  })

  it('comes back as the same wall clock it went out as', () => {
    const local = '2026-08-23T14:30'
    expect(localFrom(instantFrom(local)!)).toBe(local)
  })

  it('reads an instant written in some other zone into this one', () => {
    // What an API answer looks like. The wall clock changes; the moment
    // does not, and that is the property worth asserting.
    const iso = '2026-08-23T12:30:00+00:00'
    expect(new Date(localFrom(iso)!.replace(' ', 'T')).getTime()).not.toBeNaN()
    expect(new Date(instantFrom(localFrom(iso)!)!).getTime()).toBe(new Date(iso).getTime())
  })

  it('yields nothing for an instant it cannot read', () => {
    expect(localFrom('not an instant')).toBeNull()
  })

  it('says which offset it is about to attach, in the shape ISO-8601 writes', () => {
    expect(offsetOfLocal('2026-08-23T14:30')).toMatch(/^[+-]\d{2}:\d{2}$/)
  })
})

describe('what the control says it will send', () => {
  it('quotes the instant itself, offset and all', () => {
    // A control whose entire reason to exist is the offset should show the
    // offset. This is a console for administrators, and the string that
    // goes over the wire is the useful thing to see.
    const note = describeChoice('2026-08-23T14:30')
    expect(note.key).toBe('ui.datePicker.sends')
    expect(String(note.params?.instant)).toBe(instantFrom('2026-08-23T14:30'))
  })

  it('says so plainly when what has been typed is not a moment', () => {
    expect(describeChoice('2026-02-30T09:00')).toEqual({ key: 'ui.datePicker.unreadable' })
  })

  it('says nothing at all when nothing has been chosen', () => {
    expect(describeChoice('')).toEqual({ key: 'ui.datePicker.nothingChosen' })
  })
})

describe('the day and the time, which are two different things', () => {
  it('reads the day out of a wall-clock value', () => {
    expect(dayOfLocal('2026-08-23T14:30')).toBe('2026-08-23')
  })

  it('reads nothing out of a value that is not one', () => {
    expect(dayOfLocal('')).toBeNull()
    expect(dayOfLocal('rubbish')).toBeNull()
  })

  it('changes the day and keeps the time, because clicking a date is not setting a clock', () => {
    expect(withDay('2026-08-23T14:30', '2026-09-01')).toBe('2026-09-01T14:30')
  })

  it('starts a first choice at the top of the day', () => {
    // Clicking a date in an empty control means the date. Inventing the
    // current time would emit an instant nobody chose and nobody can see
    // they chose.
    expect(withDay('', '2026-09-01')).toBe(`2026-09-01T${DEFAULT_TIME}`)
  })
})

describe('the month', () => {
  it('is the month a day falls in', () => {
    expect(monthOf('2026-08-23')).toBe('2026-08')
  })

  it('steps forwards and backwards over the year boundary', () => {
    expect(shiftMonth('2026-12', 1)).toBe('2027-01')
    expect(shiftMonth('2026-01', -1)).toBe('2025-12')
    expect(shiftMonth('2026-08', 6)).toBe('2027-02')
  })

  it('has an instant, so its name is `Intl`\'s rather than a table of English words', () => {
    const at = monthInstant('2026-08')
    expect(at.getUTCFullYear()).toBe(2026)
    expect(at.getUTCMonth()).toBe(7)
    expect(at.getUTCDate()).toBe(1)
  })
})

describe('the grid', () => {
  const grid = monthGrid('2026-08', {})

  it('is six whole weeks, so the calendar does not change height month to month', () => {
    // A grid that grows a row in some months makes everything under it
    // jump, and the button somebody was about to press moves.
    expect(grid).toHaveLength(6)
    for (const week of grid) expect(week).toHaveLength(7)
  })

  it('starts on a Monday, the way the rest of this console counts weeks', () => {
    // `heatmap.ts` already settled this: readers run their meetings on
    // weekdays, and a week that ends at the weekend keeps those five
    // together. Two calendars in one console disagreeing about where a
    // week starts is worse than either choice.
    expect(new Date(`${grid[0]![0]!.day}T00:00:00Z`).getUTCDay()).toBe(1)
  })

  it('runs without a gap from the first cell to the last', () => {
    const days = grid.flat().map((cell) => cell.day)
    for (let at = 1; at < days.length; at += 1) {
      const previous = new Date(`${days[at - 1]}T00:00:00Z`).getTime()
      expect(new Date(`${days[at]}T00:00:00Z`).getTime() - previous).toBe(86_400_000)
    }
  })

  it('holds every day of the month it is a grid of', () => {
    const inMonth = grid.flat().filter((cell) => cell.inMonth).map((cell) => cell.dayOfMonth)
    expect(inMonth).toHaveLength(31)
    expect(inMonth[0]).toBe(1)
    expect(inMonth.at(-1)).toBe(31)
  })

  it('marks the days that lead in and trail out as not belonging to it', () => {
    // They are real days and they are clickable — a calendar that refuses
    // the 1st of the next month because it is in the last row is a
    // calendar you have to page to use.
    expect(grid[0]![0]!.inMonth).toBe(false)
    expect(grid[0]![0]!.day).toBe('2026-07-27')
  })

  it('gets February right in a leap year, which is where a grid goes wrong', () => {
    const february = monthGrid('2024-02', {})
    const days = february.flat().filter((cell) => cell.inMonth)
    expect(days).toHaveLength(29)
    expect(days.at(-1)!.day).toBe('2024-02-29')
  })

  it('marks the chosen day and today, and they are not the same mark', () => {
    const marked = monthGrid('2026-08', { chosen: '2026-08-23', today: '2026-08-10' })
    const flat = marked.flat()
    expect(flat.filter((cell) => cell.chosen).map((cell) => cell.day)).toEqual(['2026-08-23'])
    expect(flat.filter((cell) => cell.today).map((cell) => cell.day)).toEqual(['2026-08-10'])
  })

  it('disables what falls outside the bounds it was given', () => {
    const bounded = monthGrid('2026-08', { min: '2026-08-10', max: '2026-08-20' })
    const enabled = bounded.flat().filter((cell) => !cell.disabled).map((cell) => cell.day)
    expect(enabled[0]).toBe('2026-08-10')
    expect(enabled.at(-1)).toBe('2026-08-20')
    expect(enabled).toHaveLength(11)
  })

  it('disables nothing when it was given no bounds', () => {
    expect(grid.flat().some((cell) => cell.disabled)).toBe(false)
  })
})

describe('moving about with the keyboard', () => {
  it('steps a day sideways and a week vertically', () => {
    expect(moveDay('2026-08-23', 'ArrowLeft')).toBe('2026-08-22')
    expect(moveDay('2026-08-23', 'ArrowRight')).toBe('2026-08-24')
    expect(moveDay('2026-08-23', 'ArrowUp')).toBe('2026-08-16')
    expect(moveDay('2026-08-23', 'ArrowDown')).toBe('2026-08-30')
  })

  it('crosses a month boundary rather than stopping at it', () => {
    expect(moveDay('2026-08-31', 'ArrowRight')).toBe('2026-09-01')
    expect(moveDay('2026-01-01', 'ArrowLeft')).toBe('2025-12-31')
  })

  it('pages a whole month at a time', () => {
    expect(moveDay('2026-08-15', 'PageUp')).toBe('2026-07-15')
    expect(moveDay('2026-08-15', 'PageDown')).toBe('2026-09-15')
  })

  it('lands on the last day of a shorter month rather than spilling into the next', () => {
    // `2026-03-31` minus a month is not `2026-03-03`, which is what
    // `setMonth` does if nobody stops it.
    expect(moveDay('2026-03-31', 'PageUp')).toBe('2026-02-28')
    expect(moveDay('2024-03-31', 'PageUp')).toBe('2024-02-29')
  })

  it('reaches the ends of the week with Home and End', () => {
    // Sunday is the far end, because the week begins on Monday.
    expect(moveDay('2026-08-23', 'Home')).toBe('2026-08-17')
    expect(moveDay('2026-08-17', 'End')).toBe('2026-08-23')
  })

  it('has no opinion about any other key', () => {
    // `null` is what lets the component leave Tab, Enter and Escape alone
    // rather than swallowing them inside a grid.
    expect(moveDay('2026-08-23', 'Enter')).toBeNull()
    expect(moveDay('2026-08-23', 'Tab')).toBeNull()
  })

  it('has no opinion about a day it cannot read', () => {
    expect(moveDay('rubbish', 'ArrowLeft')).toBeNull()
  })
})

describe('the bounds', () => {
  it('pull a day back inside them', () => {
    expect(clampDay('2026-08-01', '2026-08-10', null)).toBe('2026-08-10')
    expect(clampDay('2026-08-31', null, '2026-08-20')).toBe('2026-08-20')
  })

  it('leave a day that is already inside them alone', () => {
    expect(clampDay('2026-08-15', '2026-08-10', '2026-08-20')).toBe('2026-08-15')
  })

  it('do nothing when there are none', () => {
    expect(clampDay('2026-08-15', null, null)).toBe('2026-08-15')
  })
})

/**
 * The two things this control can be choosing.
 *
 * A withdrawal is a moment and needs its offset. A recordings filter is a
 * pair of *inclusive calendar days* -- `sturnus.console.filters` says so
 * and reads them with `date.fromisoformat` -- and a day has no offset to
 * carry. The strategy is here rather than as a ternary at each of the six
 * places the component touches its value, and what is worth pinning is
 * that neither shape leaks into the other: a day that grew an offset
 * would be a request the API refuses, and an instant that lost one would
 * be a 400.
 */
describe('choosing a moment', () => {
  const shape = shapeOf('instant')

  it('is typed into the field the browser knows how to step', () => {
    expect(shape.inputType).toBe('datetime-local')
  })

  it('emits an instant carrying its offset, never a naive one', () => {
    const emitted = shape.toModel('2026-08-21T14:30') ?? ''
    expect(emitted).toMatch(/^2026-08-21T14:30:00[+-]\d{2}:\d{2}$/)
  })

  it('emits nothing at all for a field that is not a moment yet', () => {
    expect(shape.toModel('')).toBeNull()
    expect(shape.toModel('2026-02-30T10:00')).toBeNull()
  })

  it('says what it is about to send, because the offset is the point', () => {
    expect(shape.note('2026-08-21T14:30')?.key).toBe('ui.datePicker.sends')
  })

  it('keeps the time when the calendar moves the day', () => {
    expect(shape.onDay('2026-08-21T14:30', '2026-09-02')).toBe('2026-09-02T14:30')
  })
})

describe('choosing a day', () => {
  const shape = shapeOf('day')

  it('is typed into a date field, which has no time to leave empty', () => {
    expect(shape.inputType).toBe('date')
  })

  it('emits the day itself, with nothing attached to it', () => {
    // An offset here would claim a precision `?from=2026-08-21` does not
    // have -- and, being the browser's, would differ from the one a server
    // render attached, on exactly the filtered links this is for.
    expect(shape.toModel('2026-08-21')).toBe('2026-08-21')
  })

  it('emits nothing for a day that does not exist', () => {
    // Reachable from a hand-edited URL, and `Date` rolls it into March
    // rather than refusing it.
    expect(shape.toModel('2026-02-30')).toBeNull()
    expect(shape.toModel('')).toBeNull()
  })

  it('shows nothing for a model value that is not a day', () => {
    expect(shape.toField('2026-08-21T14:30:00+02:00')).toBe('')
    expect(shape.toField(null)).toBe('')
  })

  it('shows a day it was given', () => {
    expect(shape.toField(' 2026-08-21 ')).toBe('2026-08-21')
  })

  it('opens the calendar on the day it holds', () => {
    expect(shape.dayOf('2026-08-21')).toBe('2026-08-21')
  })

  it('replaces the day outright, because there is nothing else to keep', () => {
    expect(shape.onDay('2026-08-21', '2026-09-02')).toBe('2026-09-02')
  })

  it('adds no note, because a day has no offset to explain', () => {
    // "Sent as 2026-08-21" under a field reading 2026-08-21 is a line of
    // chrome that says nothing, and a filter bar carrying two of them is
    // one nobody reads the rest of.
    expect(shape.note('2026-08-21')).toBeNull()
    expect(shape.note('')).toBeNull()
  })
})
