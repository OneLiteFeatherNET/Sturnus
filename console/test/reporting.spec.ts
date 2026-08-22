/**
 * What a server's report means, and what the page is allowed to claim
 * about it.
 *
 * All of it lives in `~/utils/reporting` rather than in the page, because
 * every one of these is a decision -- how an absent average renders, which
 * month comes first, whether a gap in the bar row is drawn, what the
 * speaking time is a total of, whether an empty report is a fault -- and a
 * decision embedded in a template can only be tested by rendering one.
 *
 * The wording is asserted here on purpose, and heavily. Four of this
 * page's figures are misreadable in a specific direction, and a test that
 * only checked the numbers would let the sentence that prevents each
 * misreading quietly drop out:
 *
 * - `speech_seconds` is a sum over a nullable column, so a server with
 *   many unmeasured tracks looks quiet when it was merely unmeasured.
 * - the four nullable figures are null because nothing has finished, and a
 *   zero in their place would claim meetings of no length attended by
 *   nobody.
 * - `recorded_seconds` excludes the sessions still open, which is where a
 *   session that never closed goes to hide.
 * - `months` are cut in the server's zone and are absent rather than
 *   zero-filled, so a quiet stretch closes up unless something puts it
 *   back.
 *
 * And one thing is asserted by its absence: nothing in this module names
 * or ranks a person, because this report is about a server and a
 * per-person readout of attendance and speaking time is a works-council
 * matter rather than a console feature.
 */
import { describe, expect, it } from 'vitest'

import {
  REPORT_EMPTY_NOTE,
  REPORT_MIN_BAR_EXTENT,
  REPORT_MONTH_FILL_LIMIT,
  REPORT_SCOPE_NOTE,
  describeReportError,
  isReportEmpty,
  parseGuildReport,
  reportCaveats,
  reportDocumentedLine,
  reportDocumentedShare,
  reportHeadlineFigures,
  reportMonthLabel,
  reportMonthRows,
  reportMonthsNote,
  reportOpenSessionsLine,
  reportPath,
  reportRecordedLine,
  reportShapeFigures,
  reportSpanLine,
  reportSpeechCaveat,
  reportTimezoneNote,
  type GuildReport,
  type ReportMonth,
} from '../app/utils/reporting'

/** A server that has recorded a good deal and has nothing odd about it, so
 *  each test states only the one property it is actually about. */
function report(overrides: Partial<GuildReport> = {}): GuildReport {
  return {
    guild_id: '4711',
    sessions: 42,
    documented: 38,
    open_sessions: 0,
    recorded_seconds: 151200,
    speech_seconds: 48000,
    unmeasured_tracks: 0,
    tracks: 160,
    distinct_participants: 12,
    average_participants: 3.8,
    largest_meeting: 9,
    average_duration_seconds: 3600,
    longest_duration_seconds: 9000,
    first_session_at: '2025-11-04T09:00:00+00:00',
    last_session_at: '2026-08-21T12:00:00+00:00',
    timezone: 'Europe/Berlin',
    months: [],
    ...overrides,
  }
}

/** A server that has recorded nothing at all. */
function emptyReport(overrides: Partial<GuildReport> = {}): GuildReport {
  return report({
    sessions: 0,
    documented: 0,
    open_sessions: 0,
    recorded_seconds: null,
    speech_seconds: null,
    unmeasured_tracks: 0,
    tracks: 0,
    distinct_participants: 0,
    average_participants: null,
    largest_meeting: null,
    average_duration_seconds: null,
    longest_duration_seconds: null,
    first_session_at: null,
    last_session_at: null,
    months: [],
    ...overrides,
  })
}

function month(overrides: Partial<ReportMonth> & { month: string }): ReportMonth {
  return { sessions: 1, recorded_seconds: 3600, documented: 1, ...overrides }
}

/** What `ApiError` looks like to the function that reads a failure. */
function failure(status: number) {
  return { status, path: '/guilds/4711/report' }
}

/** Every sentence this module can produce for one report, so a test can
 *  assert on what none of them says. */
function everySentence(value: GuildReport): string {
  return [
    ...reportHeadlineFigures(value).map((figure) => `${figure.label} ${figure.value} ${figure.note}`),
    ...reportShapeFigures(value).map((figure) => `${figure.label} ${figure.value} ${figure.note}`),
    ...reportCaveats(value).map((caveat) => `${caveat.label} ${caveat.text}`),
    ...reportMonthRows(value).map((row) => row.detail),
    reportMonthsNote(value),
    reportSpanLine(value),
    REPORT_SCOPE_NOTE,
    REPORT_EMPTY_NOTE,
  ].join(' ')
}

describe('reading the report payload', () => {
  it('reads the whole envelope the endpoint sends', () => {
    const parsed = parseGuildReport({
      guild_id: '4711',
      sessions: 42,
      documented: 38,
      open_sessions: 1,
      recorded_seconds: 151200.0,
      speech_seconds: 48000.0,
      unmeasured_tracks: 7,
      tracks: 160,
      distinct_participants: 12,
      average_participants: 3.8,
      largest_meeting: 9,
      average_duration_seconds: 3600.0,
      longest_duration_seconds: 9000.0,
      first_session_at: '2025-11-04T09:00:00+00:00',
      last_session_at: '2026-08-21T12:00:00+00:00',
      timezone: 'Europe/Berlin',
      months: [{ month: '2026-08', sessions: 5, recorded_seconds: 18000.0, documented: 5 }],
    })
    expect(parsed.guild_id).toBe('4711')
    expect(parsed.sessions).toBe(42)
    expect(parsed.open_sessions).toBe(1)
    expect(parsed.average_participants).toBe(3.8)
    expect(parsed.timezone).toBe('Europe/Berlin')
    expect(parsed.months).toEqual([
      { month: '2026-08', sessions: 5, recorded_seconds: 18000, documented: 5 },
    ])
  })

  it('keeps a null figure null rather than rounding it to zero', () => {
    // The whole reason these four fields are nullable. A server whose
    // meetings have not finished has no average length, and a zero here
    // would claim its meetings are instantaneous.
    const parsed = parseGuildReport({
      average_duration_seconds: null,
      longest_duration_seconds: null,
      average_participants: null,
      largest_meeting: null,
    })
    expect(parsed.average_duration_seconds).toBeNull()
    expect(parsed.longest_duration_seconds).toBeNull()
    expect(parsed.average_participants).toBeNull()
    expect(parsed.largest_meeting).toBeNull()
  })

  it('turns a nonsensical optional figure into an absence, not a zero', () => {
    // "We do not know" is true of a broken figure; "none" is not.
    const parsed = parseGuildReport({
      average_duration_seconds: -5,
      largest_meeting: 'nine',
      speech_seconds: Number.NaN,
    })
    expect(parsed.average_duration_seconds).toBeNull()
    expect(parsed.largest_meeting).toBeNull()
    expect(parsed.speech_seconds).toBeNull()
  })

  it('never reports a negative or nonsensical count', () => {
    // A defect upstream must not render as "-3 meetings" beside a
    // server's name, where it reads as a fact about that server.
    const parsed = parseGuildReport({
      sessions: -3,
      documented: 'many',
      tracks: 2.4,
      unmeasured_tracks: null,
    })
    expect(parsed.sessions).toBe(0)
    expect(parsed.documented).toBe(0)
    expect(parsed.tracks).toBe(2)
    expect(parsed.unmeasured_tracks).toBe(0)
  })

  it('drops a month it cannot place on a calendar', () => {
    // A malformed month cannot be ordered or labelled, and would anchor
    // the gap filling at an arbitrary point in history -- turning one bad
    // string into a thousand rows of zeros.
    const parsed = parseGuildReport({
      months: [
        { month: 'last winter', sessions: 3 },
        { month: '2026-13', sessions: 3 },
        { month: '2026-8', sessions: 3 },
        { month: '2026-08', sessions: 3 },
      ],
    })
    expect(parsed.months.map((entry) => entry.month)).toEqual(['2026-08'])
  })

  it('yields a well-formed report for a payload it cannot make sense of', () => {
    // Never null: a parser that gave up would turn a strange payload into
    // a blank page with no error anywhere, which is the failure mode
    // hardest to report.
    const parsed = parseGuildReport('nonsense')
    expect(parsed.guild_id).toBeNull()
    expect(parsed.sessions).toBe(0)
    expect(parsed.months).toEqual([])
    expect(parsed.timezone).toBe('')
  })

  it('escapes the guild id in the path it builds', () => {
    // A string from an API allowed to contain a slash is a string allowed
    // to address a different endpoint.
    expect(reportPath('4711')).toBe('/guilds/4711/report')
    expect(reportPath('../guilds/1')).toBe('/guilds/..%2Fguilds%2F1/report')
  })
})

describe('the headline figures', () => {
  it('leads with the meetings and says what span they cover', () => {
    const figures = reportHeadlineFigures(report())
    expect(figures.map((figure) => figure.label)).toEqual([
      'Meetings recorded',
      'Meetings written up',
      'Time recorded',
      'Time spoken',
    ])
    expect(figures[0]!.value).toBe('42')
    expect(figures[0]!.note).toContain('4 Nov 2025, 09:00 UTC')
    expect(figures[0]!.note).toContain('21 Aug 2026, 12:00 UTC')
  })

  it('renders a missing total as an absence rather than as no time at all', () => {
    const figures = reportHeadlineFigures(report({ recorded_seconds: null, speech_seconds: null }))
    const recorded = figures.find((figure) => figure.key === 'recorded')!
    expect(recorded.value).toBe('—')
    expect(recorded.tone).toBe('absent')
  })

  it('groups the shape of a meeting apart from how much has happened', () => {
    expect(reportShapeFigures(report()).map((figure) => figure.label)).toEqual([
      'Typical meeting',
      'Longest meeting',
      'People per meeting',
      'Largest meeting',
      'People recorded',
      'Still recording',
    ])
  })

  it('writes an average of people to one decimal, without a false one', () => {
    const perMeeting = (value: number | null) =>
      reportShapeFigures(report({ average_participants: value })).find(
        (figure) => figure.key === 'average-participants',
      )!.value
    expect(perMeeting(3.84)).toBe('3.8')
    // A whole number is not a measurement to a tenth, and "4.0" claims it
    // is.
    expect(perMeeting(4)).toBe('4')
  })
})

describe('a figure that is missing rather than zero', () => {
  it('renders every null as an absence and says why', () => {
    // `null` is deliberately not `0`: a server with no closed sessions has
    // no average length, and printing a zero would state that its meetings
    // are instantaneous and attended by nobody.
    const figures = reportShapeFigures(
      report({
        average_duration_seconds: null,
        longest_duration_seconds: null,
        average_participants: null,
        largest_meeting: null,
      }),
    )
    for (const key of ['average-duration', 'longest-duration', 'average-participants', 'largest-meeting']) {
      const figure = figures.find((candidate) => candidate.key === key)!
      expect(figure.value).toBe('—')
      expect(figure.tone).toBe('absent')
      expect(figure.note).toContain('No meeting in this server has finished')
      expect(figure.note).toContain('not a figure of zero')
    }
  })

  it('never leaves a missing figure without the sentence that explains it', () => {
    // An em dash with nothing beside it reads as "still loading", which is
    // the one thing this page is not doing.
    for (const figure of [...reportHeadlineFigures(emptyReport()), ...reportShapeFigures(emptyReport())]) {
      expect(figure.note?.trim()).not.toBe('')
      expect(figure.note).not.toBeNull()
    }
  })
})

describe('how the documented rate reads', () => {
  it('says how many of this server’s meetings reached a protocol, and how many did not', () => {
    // A rate on its own is read as a property of the software; this is a
    // property of what happened here, so it is written as "n of m".
    const line = reportDocumentedLine(report({ sessions: 42, documented: 38 }))
    expect(line).toContain('38 of the 42 meetings recorded in this server reached a protocol')
    expect(line).toContain('90 %')
    expect(line).toContain('The other 4 were recorded and never written up')
  })

  it('says so plainly when every meeting was written up', () => {
    expect(reportDocumentedLine(report({ sessions: 42, documented: 42 }))).toContain(
      'Every one of the 42 meetings recorded in this server reached a protocol',
    )
  })

  it('never rounds an incomplete rate up to all of them', () => {
    // 999 of 1000 rounds to 100 %, and "100 %" beside a figure that is not
    // all of them tells somebody every meeting is covered when one is not.
    expect(reportDocumentedShare(report({ sessions: 1000, documented: 999 }))).toBe(99)
    expect(reportDocumentedShare(report({ sessions: 1000, documented: 1000 }))).toBe(100)
  })

  it('never rounds a real success down to none of them', () => {
    expect(reportDocumentedShare(report({ sessions: 1000, documented: 1 }))).toBe(1)
    expect(reportDocumentedShare(report({ sessions: 1000, documented: 0 }))).toBe(0)
  })

  it('has no rate at all for a server that has recorded nothing', () => {
    expect(reportDocumentedShare(emptyReport())).toBeNull()
    expect(reportDocumentedLine(emptyReport())).toContain('nothing to write up')
  })

  it('does not count a meeting still recording as a failure to write one up', () => {
    // A meeting that has not ended cannot have been written up, and
    // blaming the pipeline for the clock is the wrong reading of the same
    // two numbers.
    const line = reportDocumentedLine(report({ sessions: 42, documented: 38, open_sessions: 1 }))
    expect(line).toContain('1 meeting is still recording and cannot have been written up yet')
  })
})

describe('the meetings still open', () => {
  it('says nothing is open rather than letting the figure vanish', () => {
    // A figure that appears only when it is bad news is a figure whose
    // absence has to be interpreted.
    expect(reportOpenSessionsLine(report({ open_sessions: 0 }))).toContain(
      'Nothing is being recorded in this server right now',
    )
  })

  it('names the second reading of an open session', () => {
    // One meeting open for ten minutes is a meeting; one open since
    // Tuesday is a session that never closed, and the number alone cannot
    // tell them apart.
    const line = reportOpenSessionsLine(report({ open_sessions: 1 }))
    expect(line).toContain('1 meeting in this server has no end time yet')
    expect(line).toContain('a session that never closed')
    expect(line).toContain('a session open for days is the second')
  })

  it('marks an open session as worth a second look', () => {
    const open = (count: number) =>
      reportShapeFigures(report({ open_sessions: count })).find((figure) => figure.key === 'open')!
    expect(open(0).tone).toBe('plain')
    expect(open(3).tone).toBe('watch')
  })

  it('says the recorded total leaves the open meetings out', () => {
    // `recorded_seconds` excludes them deliberately, and a total whose
    // exclusions go unmentioned is a total whose scope the reader has to
    // infer from its own silence.
    expect(reportRecordedLine(report({ open_sessions: 2 }))).toContain(
      'The 2 meetings still recording are not in it',
    )
    expect(reportRecordedLine(report({ open_sessions: 0 }))).toContain('all of which have ended')
  })
})

describe('what the speaking time is a total of', () => {
  it('refuses to let a hole in the measurement read as a quiet server', () => {
    // The single most misreadable number on this page. `speech_seconds` is
    // a SUM over a nullable column and skips the nulls in silence, so a
    // small figure under a large recorded total reads as "these meetings
    // were quiet" to anybody not told otherwise.
    const caveat = reportSpeechCaveat(report({ tracks: 160, unmeasured_tracks: 7 }))
    expect(caveat).toContain('7 of the 160 recorded tracks in this server were never measured')
    expect(caveat).toContain('a sum skips them in silence')
    expect(caveat).toContain('the total for the other 153 tracks only')
    expect(caveat).toContain('it describes part of what was recorded')
    expect(caveat).toContain('It does not mean this server was quiet.')
  })

  it('says a total covers everything when it does', () => {
    const caveat = reportSpeechCaveat(report({ tracks: 160, unmeasured_tracks: 0 }))
    expect(caveat).toContain('Every one of the 160 recorded tracks')
    expect(caveat).toContain('covers all of what was recorded')
  })

  it('says a measurement was never taken when none of it was', () => {
    // Zero measured tracks is not a silent server; it is a server whose
    // recordings predate the columns that hold the figure.
    const caveat = reportSpeechCaveat(report({ tracks: 160, unmeasured_tracks: 160 }))
    expect(caveat).toContain('None of the 160 recorded tracks')
    expect(caveat).toContain('a measurement that was never taken, not as a server that was quiet')
  })

  it('says there is nothing to measure when nothing was recorded', () => {
    const caveat = reportSpeechCaveat(emptyReport())
    expect(caveat).toContain('No audio has been recorded in this server')
    expect(caveat).toContain('missing rather than zero')
  })

  it('carries the caveat with the figure, not only in a panel', () => {
    // A footnote is read once, by the person who was already being
    // careful.
    const speech = reportHeadlineFigures(report({ tracks: 160, unmeasured_tracks: 7 })).find(
      (figure) => figure.key === 'speech',
    )!
    expect(speech.note).toContain('It does not mean this server was quiet.')
  })
})

describe('which calendar the months were cut in', () => {
  it('names the zone rather than letting the reader assume theirs', () => {
    const note = reportTimezoneNote(report({ timezone: 'Europe/Berlin' }))
    expect(note).toContain('cut in Europe/Berlin')
    expect(note).toContain('not in UTC and not in yours')
  })

  it('says why the server does not bucket by UTC', () => {
    // A meeting at 00:30 belongs to the month the people in it think it
    // does.
    expect(reportTimezoneNote(report())).toContain(
      'A meeting that begins at 00:30 belongs to the month the people in it think it does',
    )
  })

  it('warns that the instants on the page use a different clock again', () => {
    // A page rendered on a server cannot know the reader's zone, so the
    // timestamps stay in UTC while the months do not. Two clocks on one
    // page is a seam worth naming.
    expect(reportTimezoneNote(report())).toContain('written in UTC all the same')
  })

  it('reports the uncertainty when the API named no zone at all', () => {
    const note = reportTimezoneNote(report({ timezone: '' }))
    expect(note).toContain('did not say which calendar')
    expect(note).toContain('do not assume it is yours')
  })

  it('puts both caveats where the figures are, in a fixed order', () => {
    expect(reportCaveats(report()).map((caveat) => caveat.key)).toEqual(['speech', 'timezone'])
  })
})

describe('the span the report covers', () => {
  it('writes both ends in UTC and says which zone that is', () => {
    expect(reportSpanLine(report())).toBe(
      'Everything recorded in this server between 4 Nov 2025, 09:00 UTC and 21 Aug 2026, 12:00 UTC.',
    )
  })

  it('says there is no span rather than printing a dash for one', () => {
    expect(reportSpanLine(emptyReport())).toContain('covers no time at all')
  })

  it('reads a single meeting as one meeting, not as a range of no length', () => {
    const line = reportSpanLine(
      report({ first_session_at: '2026-08-21T12:00:00+00:00', last_session_at: '2026-08-21T12:00:00+00:00' }),
    )
    expect(line).toBe('One meeting, recorded 21 Aug 2026, 12:00 UTC.')
  })

  it('says so when only one end of the span is known', () => {
    const line = reportSpanLine(report({ last_session_at: null }))
    expect(line).toContain('Only one end of the span is known')
    expect(line).toContain('4 Nov 2025, 09:00 UTC')
  })
})

describe('the months, and the gaps between them', () => {
  it('lists them oldest first, so the row reads as a timeline', () => {
    // The opposite of the Queue page's newest-first list, and for the
    // opposite reason: nobody scans this for one particular month, they
    // look at its shape.
    const rows = reportMonthRows(
      report({ months: [month({ month: '2026-03' }), month({ month: '2026-01' })] }),
    )
    expect(rows.map((row) => row.month)).toEqual(['2026-01', '2026-02', '2026-03'])
  })

  it('puts a skipped month back as a row rather than letting the gap close up', () => {
    // The API sends only the months in which something happened. A bar row
    // that puts March next to November draws them as neighbours, and a
    // server that went quiet for eight months reads as one that recorded
    // steadily.
    const rows = reportMonthRows(
      report({ months: [month({ month: '2026-03' }), month({ month: '2026-11' })] }),
    )
    expect(rows).toHaveLength(9)
    expect(rows.filter((row) => row.silent)).toHaveLength(7)
    expect(rows[1]!.silent).toBe(true)
    expect(rows[1]!.sessions).toBe(0)
    expect(rows[1]!.detail).toBe('April 2026: nothing was recorded in this server.')
  })

  it('invents no months before the first or after the last', () => {
    // A server is not silent in the months before it existed, and rows
    // there would be inventing history rather than showing a gap in it.
    const rows = reportMonthRows(report({ months: [month({ month: '2026-08' })] }))
    expect(rows.map((row) => row.month)).toEqual(['2026-08'])
  })

  it('has no rows at all for a server with no months', () => {
    expect(reportMonthRows(emptyReport())).toEqual([])
    expect(reportMonthsNote(emptyReport())).toContain('No month in this server has any recording')
  })

  it('says what it did about the gaps, either way', () => {
    const filled = report({ months: [month({ month: '2026-03' }), month({ month: '2026-06' })] })
    expect(reportMonthsNote(filled)).toContain(
      'The 2 months in which nothing was recorded are listed with a zero rather than left out',
    )
    const solid = report({ months: [month({ month: '2026-03' }), month({ month: '2026-04' })] })
    expect(reportMonthsNote(solid)).toContain('Something was recorded in each of them')
  })

  it('stops filling a span too long to list, and says it stopped', () => {
    // A single stray month would otherwise produce hundreds of rows of
    // zeros and bury the months that carry something -- but a list with
    // gaps silently left out is exactly what the filling exists to
    // prevent, so the page has to admit it.
    const far = report({
      months: [month({ month: '2010-01' }), month({ month: '2026-08' })],
    })
    expect(reportMonthRows(far)).toHaveLength(2)
    const note = reportMonthsNote(far)
    expect(note).toContain('Only the months in which something was recorded are listed')
    expect(note).toContain(`more than ${REPORT_MONTH_FILL_LIMIT / 12} years`)
    expect(note).toContain('not necessarily neighbouring months')
  })

  it('scales the bars against the busiest month, and never to nothing', () => {
    // A busy server makes its quiet months round to nothing, and a month
    // with one meeting rendered as an empty row is indistinguishable from
    // a month with none.
    const rows = reportMonthRows(
      report({
        months: [
          month({ month: '2026-01', sessions: 1 }),
          month({ month: '2026-02', sessions: 400 }),
        ],
      }),
    )
    expect(rows[1]!.extent).toBe(1)
    expect(rows[0]!.extent).toBe(REPORT_MIN_BAR_EXTENT)
    expect(rows[0]!.extent).toBeGreaterThan(0)
  })

  it('draws no bar for a month in which nothing happened', () => {
    const rows = reportMonthRows(
      report({ months: [month({ month: '2026-01' }), month({ month: '2026-03' })] }),
    )
    expect(rows[1]!.extent).toBe(0)
  })

  it('gives every row a sentence for somebody who cannot see the bar', () => {
    // A bar with no text is a bar only its author can read.
    const rows = reportMonthRows(
      report({
        months: [month({ month: '2026-08', sessions: 5, recorded_seconds: 18000, documented: 5 })],
      }),
    )
    expect(rows[0]!.detail).toBe('August 2026: 5 meetings, 5 h recorded, 5 written up.')
  })

  it('does not describe a month the API sent as empty the way it describes one it skipped', () => {
    // The API said something about it, and this page should not overwrite
    // that with an assumption.
    const rows = reportMonthRows(
      report({
        months: [
          month({ month: '2026-01', sessions: 0, recorded_seconds: 0, documented: 0 }),
          month({ month: '2026-02' }),
        ],
      }),
    )
    expect(rows[0]!.silent).toBe(false)
    expect(rows[0]!.detail).toContain('0 meetings')
  })

  it('keys every row uniquely, so a duplicate month cannot render twice', () => {
    const rows = reportMonthRows(
      report({ months: [month({ month: '2026-01' }), month({ month: '2026-01', sessions: 9 })] }),
    )
    expect(rows).toHaveLength(1)
    expect(rows[0]!.sessions).toBe(9)
  })

  it('names a month in full rather than by its key', () => {
    expect(reportMonthLabel('2026-08')).toBe('August 2026')
    expect(reportMonthLabel('2025-11')).toBe('November 2025')
  })

  it('hands back a month key it cannot read rather than a blank', () => {
    // A raw key is at least something the reader can match against the
    // payload.
    expect(reportMonthLabel('whenever')).toBe('whenever')
  })
})

describe('a server with nothing to report', () => {
  it('recognises one that has recorded nothing at all', () => {
    expect(isReportEmpty(emptyReport())).toBe(true)
  })

  it('is not empty once a single meeting exists', () => {
    expect(isReportEmpty(emptyReport({ sessions: 1 }))).toBe(false)
    expect(isReportEmpty(emptyReport({ first_session_at: '2026-08-21T12:00:00+00:00' }))).toBe(false)
    expect(isReportEmpty(emptyReport({ months: [month({ month: '2026-08' })] }))).toBe(false)
  })

  it('shows the figures rather than the empty state for a report that contradicts itself', () => {
    // No sessions standing next to a hundred and sixty tracks is a defect
    // upstream, and showing the figures makes it visible where an empty
    // state would hide it behind an invitation to do something that has
    // already been done.
    expect(isReportEmpty(emptyReport({ tracks: 160 }))).toBe(false)
    expect(isReportEmpty(emptyReport({ distinct_participants: 4 }))).toBe(false)
  })

  it('says so in a sentence rather than in a wall of dashes', () => {
    expect(REPORT_EMPTY_NOTE).toContain('shows no figures rather than a grid of zeros')
    expect(REPORT_EMPTY_NOTE).toContain('a zero would be a measurement')
    expect(REPORT_EMPTY_NOTE).toContain('Once a meeting happens')
  })
})

describe('what this report is not about', () => {
  it('says outright that there is no per-person breakdown behind it', () => {
    // A per-person readout of meeting attendance and speaking time is a
    // means of monitoring conduct and performance at work, which is a
    // works-council matter rather than a console feature. The payload
    // carries no names and no ids; the framing has to match.
    expect(REPORT_SCOPE_NOTE).toContain('about the server as a whole and never about the people in it')
    expect(REPORT_SCOPE_NOTE).toContain('no per-person breakdown behind it')
    expect(REPORT_SCOPE_NOTE).toContain('Counts of people are counts, and stop there.')
  })

  it('describes the participant count as a count and nothing more', () => {
    const people = reportShapeFigures(report()).find((figure) => figure.key === 'participants')!
    expect(people.value).toBe('12')
    expect(people.note).toContain('A count and nothing else')
    expect(people.note).toContain('does not send this page their names')
  })

  it('never hints that a per-person readout is on its way', () => {
    // Every sentence this module can produce, checked at once: an
    // interface that promises a breakdown is as much a promise as one that
    // ships it.
    const prose = everySentence(
      report({ months: [month({ month: '2026-01' }), month({ month: '2026-03' })], open_sessions: 1 }),
    )
    for (const forbidden of [' soon', 'coming', 'per person', 'per-person breakdown of', 'who spoke most', 'top speaker']) {
      expect(prose.toLowerCase()).not.toContain(forbidden.toLowerCase())
    }
  })
})

describe('when the API says no', () => {
  it('says a session has ended rather than that the server is gone', () => {
    expect(describeReportError(failure(401))).toContain('Sign in again')
  })

  it('names what an administrator is when it refuses one who is not', () => {
    expect(describeReportError(failure(403))).toContain('admin_role_id')
  })

  it('covers both readings of a 404 without guessing which', () => {
    // The API answers 404 both for a server that does not exist and for
    // one the caller does not administer, on purpose: it will not confirm
    // the existence of a server to somebody with no business there.
    const message = describeReportError(failure(404))
    expect(message).toContain('does not know this server, or you no longer administer it')
    expect(message).toContain('answers the same way to both')
  })

  it('separates a refusal from never having reached the API at all', () => {
    // `ApiError` uses 0 for "never got a response", which must not read as
    // an answer from the API.
    expect(describeReportError(failure(0))).toContain('Could not reach the API')
    expect(describeReportError(null)).toContain('Could not reach the API')
  })

  it('names an unexpected status rather than inventing a reason for it', () => {
    expect(describeReportError(failure(503))).toContain('Sturnus answered 503')
    expect(describeReportError(failure(503))).toContain('Nothing is known about why')
  })

  it('reads a raw fetch failure’s statusCode as well as an ApiError’s status', () => {
    expect(describeReportError({ statusCode: 403 })).toContain('admin_role_id')
  })
})
