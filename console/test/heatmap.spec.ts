/**
 * The year grid and the colour scale.
 *
 * Both are decisions -- where a sparse list of days lands in a grid of
 * weeks, and how much recording counts as "a lot" -- and a decision
 * embedded in a template can only be tested by rendering one. So they live
 * here, and the component does nothing but paint what these return.
 */
import { describe, expect, it } from 'vitest'

import {
  INTENSITY_LABELS,
  INTENSITY_THRESHOLDS_SECONDS,
  WEEKDAY_LABELS,
  buildYearGrid,
  describeCell,
  formatIsoDate,
  intensityFor,
  monthColumns,
  shiftWithinYear,
  type CalendarDay,
} from '../app/utils/heatmap'

/** A day the API would have returned, with everything but the date defaulted. */
function day(date: string, over: Partial<CalendarDay> = {}): CalendarDay {
  return {
    date,
    sessions: 1,
    total_duration_seconds: 600,
    participants: 2,
    ...over,
  }
}

/** Every cell in the grid that stands for a real date, in date order. */
function datedCells(weeks: ReturnType<typeof buildYearGrid>) {
  return weeks.flat().filter((cell) => cell.date !== null)
}

describe('building the year grid', () => {
  it('lays the year out in columns of seven, Monday first', () => {
    for (const week of buildYearGrid(2026, [])) {
      expect(week).toHaveLength(7)
    }
  })

  it('pads the first week so a year starting mid-week starts on its weekday', () => {
    // 1 January 2026 is a Thursday. Monday, Tuesday and Wednesday of that
    // week belong to 2025 and must be blanks, not December's days shown
    // under a 2026 heading.
    const first = buildYearGrid(2026, [])[0]!
    expect(first.map((cell) => cell.date)).toEqual([
      null,
      null,
      null,
      '2026-01-01',
      '2026-01-02',
      '2026-01-03',
      '2026-01-04',
    ])
  })

  it('pads nothing at the front of a year that begins on a Monday', () => {
    // 1 January 2024 is a Monday.
    expect(buildYearGrid(2024, [])[0]![0]!.date).toBe('2024-01-01')
  })

  it('pads six days at the front of a year that begins on a Sunday', () => {
    // 1 January 2023 is a Sunday -- the worst case for a Monday-first grid.
    const first = buildYearGrid(2023, [])[0]!
    expect(first.slice(0, 6).map((cell) => cell.date)).toEqual([null, null, null, null, null, null])
    expect(first[6]!.date).toBe('2023-01-01')
  })

  it('pads the last week so the grid stays rectangular', () => {
    // 31 December 2026 is a Thursday; Friday to Sunday belong to 2027.
    const weeks = buildYearGrid(2026, [])
    const last = weeks[weeks.length - 1]!
    expect(last.map((cell) => cell.date)).toEqual([
      '2026-12-28',
      '2026-12-29',
      '2026-12-30',
      '2026-12-31',
      null,
      null,
      null,
    ])
  })

  it('holds every day of an ordinary year exactly once', () => {
    const dates = datedCells(buildYearGrid(2026, [])).map((cell) => cell.date)
    expect(dates).toHaveLength(365)
    expect(new Set(dates).size).toBe(365)
    expect(dates[0]).toBe('2026-01-01')
    expect(dates[364]).toBe('2026-12-31')
  })

  it('holds the leap day of a leap year', () => {
    const dates = datedCells(buildYearGrid(2024, [])).map((cell) => cell.date)
    expect(dates).toHaveLength(366)
    expect(dates).toContain('2024-02-29')
  })

  it('gives a day nobody recorded on a cell of its own rather than a gap', () => {
    // The API sends an entry only for days that had recordings. A day with
    // none is still a day, and a grid that skipped it would slide every
    // later day onto the wrong weekday.
    const cell = datedCells(buildYearGrid(2026, [day('2026-01-02')])).find(
      (c) => c.date === '2026-01-01',
    )!
    expect(cell.sessions).toBe(0)
    expect(cell.totalDurationSeconds).toBe(0)
    expect(cell.participants).toBe(0)
  })

  it('places a recorded day on the date the API gave it', () => {
    const weeks = buildYearGrid(2026, [
      day('2026-08-21', { sessions: 3, total_duration_seconds: 4320, participants: 5 }),
    ])
    const cell = datedCells(weeks).find((c) => c.date === '2026-08-21')!
    expect(cell.sessions).toBe(3)
    expect(cell.totalDurationSeconds).toBe(4320)
    expect(cell.participants).toBe(5)
    // A Friday, so the fifth row of its column.
    const column = weeks.find((week) => week.some((c) => c.date === '2026-08-21'))!
    expect(column.indexOf(cell)).toBe(4)
  })

  it('ignores a day the API sent that does not belong to this year', () => {
    // Defensive: a clock skew or an off-by-one on the server must not put a
    // 2025 day into the 2026 grid, where it would have nowhere to go.
    const weeks = buildYearGrid(2026, [day('2025-12-31'), day('2026-01-01')])
    expect(datedCells(weeks).filter((c) => c.sessions > 0).map((c) => c.date)).toEqual([
      '2026-01-01',
    ])
  })

  it('names the seven rows so the grid says which day a row is', () => {
    expect(WEEKDAY_LABELS).toEqual([
      'Monday',
      'Tuesday',
      'Wednesday',
      'Thursday',
      'Friday',
      'Saturday',
      'Sunday',
    ])
  })
})

describe('the month headings above the grid', () => {
  it('names all twelve months of the year', () => {
    expect(monthColumns(buildYearGrid(2026, [])).map((m) => m.label)).toEqual([
      'Jan',
      'Feb',
      'Mar',
      'Apr',
      'May',
      'Jun',
      'Jul',
      'Aug',
      'Sep',
      'Oct',
      'Nov',
      'Dec',
    ])
  })

  it('covers every column exactly once, so the headings stay over their weeks', () => {
    const weeks = buildYearGrid(2026, [])
    const spans = monthColumns(weeks).reduce((total, month) => total + month.span, 0)
    expect(spans).toBe(weeks.length)
  })

  it('gives a week that straddles a month boundary to the month it started in', () => {
    // 1 February 2026 is a Sunday, so its week is six days of January. A
    // heading that jumped to February there would sit above January.
    const weeks = buildYearGrid(2026, [])
    const january = monthColumns(weeks)[0]!
    const straddling = weeks.findIndex((week) => week.some((cell) => cell.date === '2026-02-01'))
    expect(straddling).toBeLessThan(january.span)
  })
})

describe('mapping a duration to an intensity', () => {
  it('gives a day with nothing recorded the lowest step', () => {
    expect(intensityFor(0)).toBe(0)
  })

  it('lifts a day with any recording at all off the floor', () => {
    // A five-minute session is not a blank day, and the grid must not say
    // it was.
    expect(intensityFor(300)).toBe(1)
  })

  it('steps up at half an hour, two hours and four hours', () => {
    expect(intensityFor(1799)).toBe(1)
    expect(intensityFor(1800)).toBe(2)
    expect(intensityFor(7199)).toBe(2)
    expect(intensityFor(7200)).toBe(3)
    expect(intensityFor(14399)).toBe(3)
    expect(intensityFor(14400)).toBe(4)
  })

  it('does not climb past the top step for an extraordinary day', () => {
    expect(intensityFor(999999)).toBe(4)
  })

  it('offers five steps, which is as many as a lightness ramp can separate', () => {
    // More steps than this and two neighbouring shades of one hue are
    // indistinguishable -- which would make the scale decorative rather
    // than readable.
    expect(INTENSITY_THRESHOLDS_SECONDS).toEqual([1800, 7200, 14400])
    expect(INTENSITY_LABELS).toHaveLength(4 + 1)
  })

  it('has a word for every step, because colour cannot be the only channel', () => {
    for (const label of INTENSITY_LABELS) {
      expect(label.trim()).not.toBe('')
    }
  })
})

describe('describing a cell', () => {
  it('reads the date without a timezone shifting it', () => {
    // Formatting from the string rather than from a parsed Date: a browser
    // west of Greenwich would turn a parsed 2026-08-21 into 20 August.
    expect(formatIsoDate('2026-08-21')).toBe('21 August 2026')
  })

  it('names the day, the count, the length, the people and the step', () => {
    const weeks = buildYearGrid(2026, [
      day('2026-08-21', { sessions: 3, total_duration_seconds: 4320, participants: 5 }),
    ])
    const cell = datedCells(weeks).find((c) => c.date === '2026-08-21')!
    expect(describeCell(cell)).toBe(
      'Friday, 21 August 2026 (UTC): 3 sessions, 1 h 12 min, 5 people. Activity: moderate (2 of 4).',
    )
  })

  it('says plainly that nothing was recorded, rather than saying zero of everything', () => {
    const cell = datedCells(buildYearGrid(2026, [])).find((c) => c.date === '2026-01-01')!
    expect(describeCell(cell)).toBe('Thursday, 1 January 2026 (UTC): no recordings.')
  })

  it('counts one session and one person in the singular', () => {
    const weeks = buildYearGrid(2026, [
      day('2026-03-02', { sessions: 1, total_duration_seconds: 600, participants: 1 }),
    ])
    const cell = datedCells(weeks).find((c) => c.date === '2026-03-02')!
    expect(describeCell(cell)).toBe(
      'Monday, 2 March 2026 (UTC): 1 session, 10 min, 1 person. Activity: light (1 of 4).',
    )
  })

  it('says nothing for a padding cell, which stands for no day at all', () => {
    const blank = buildYearGrid(2026, [])[0]![0]!
    expect(blank.date).toBeNull()
    expect(describeCell(blank)).toBe('')
  })
})

describe('walking the grid with the arrow keys', () => {
  it('moves one day at a time down a column', () => {
    expect(shiftWithinYear('2026-08-21', 1, 2026)).toBe('2026-08-22')
    expect(shiftWithinYear('2026-08-21', -1, 2026)).toBe('2026-08-20')
  })

  it('moves a whole week sideways, which is what a column is', () => {
    expect(shiftWithinYear('2026-08-21', 7, 2026)).toBe('2026-08-28')
    expect(shiftWithinYear('2026-08-21', -7, 2026)).toBe('2026-08-14')
  })

  it('crosses a month boundary without arithmetic of its own', () => {
    expect(shiftWithinYear('2026-01-31', 1, 2026)).toBe('2026-02-01')
    expect(shiftWithinYear('2026-03-01', -1, 2026)).toBe('2026-02-28')
  })

  it('lands on the leap day of a leap year', () => {
    expect(shiftWithinYear('2024-02-28', 1, 2024)).toBe('2024-02-29')
  })

  it('stops at the first day rather than walking into last year', () => {
    // The grid holds this year only. A key press that moved focus to a cell
    // that is not rendered would drop focus to the document, which is how a
    // keyboard user loses their place entirely.
    expect(shiftWithinYear('2026-01-01', -7, 2026)).toBe('2026-01-01')
  })

  it('stops at the last day rather than walking into next year', () => {
    expect(shiftWithinYear('2026-12-31', 7, 2026)).toBe('2026-12-31')
  })
})
