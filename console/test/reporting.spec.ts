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
 * The module no longer writes sentences, so most of what follows asserts on
 * a key and the params that go into it -- both halves, because the params
 * are the decision as much as the key is: the count that chooses "the other
 * one was" over "the other four were" is chosen here and spelled out in the
 * locale file.
 *
 * The wording is still asserted, in the few places where the wording is the
 * point. Four of this page's figures are misreadable in a specific
 * direction, and a test that only checked which key was chosen would let
 * the sentence that prevents each misreading quietly drop out of
 * `en.json` -- where it now lives, and where nothing else in this suite
 * looks at it:
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
 *
 * `en.json` is read from disk, the way `i18n.spec.ts` reads it: what ships
 * is the file, so the file is what is checked. No Vue application and no
 * i18n instance -- a locale file is data, and so is everything this module
 * returns.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  REPORT_EMPTY_HEADING_KEY,
  REPORT_EMPTY_NOTE_KEY,
  REPORT_MIN_BAR_EXTENT,
  REPORT_MONTH_FILL_LIMIT,
  REPORT_SCOPE_NOTE_KEY,
  describeReportError,
  isReportEmpty,
  parseGuildReport,
  reportCaveats,
  reportDocumentedLine,
  reportDocumentedShare,
  reportHeadlineFigures,
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

/* -------------------------------------------------------------------- */
/* The fixtures                                                          */
/* -------------------------------------------------------------------- */

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

/** An instant as this module hands one on: which moment, and which of
 *  `i18n.config.ts`'s formats is to write it. */
function instant(iso: string) {
  return { at: new Date(iso), format: 'utcMoment' }
}

/** A `YYYY-MM` key as the UTC instant its month begins at -- which is what
 *  replaced the table of English month names this module used to keep. */
function monthInstant(key: string) {
  return {
    at: new Date(Date.UTC(Number(key.slice(0, 4)), Number(key.slice(5, 7)) - 1, 1)),
    format: 'monthYear',
  }
}

/* -------------------------------------------------------------------- */
/* The English behind a key                                              */
/* -------------------------------------------------------------------- */

/** Every leaf of `en.json`, as `namespace.name` -> the sentence. */
function flatten(node: unknown, prefix = ''): Record<string, string> {
  const flat: Record<string, string> = {}
  for (const [name, value] of Object.entries(node as Record<string, unknown>)) {
    const key = prefix ? `${prefix}.${name}` : name
    if (value !== null && typeof value === 'object') Object.assign(flat, flatten(value, key))
    else flat[key] = String(value)
  }
  return flat
}

// Resolved from the working directory rather than from `import.meta.url`:
// the tests run under happy-dom, where `import.meta.url` is not a `file:`
// URL and `fileURLToPath` refuses it. Same reasoning as `i18n.spec.ts`.
const EN = flatten(
  JSON.parse(readFileSync(resolve(process.cwd(), 'i18n/locales/en.json'), 'utf8')),
)

/** What a key says in English, and a failure rather than a blank for a key
 *  no locale file carries -- a key nobody added renders on the page as
 *  itself, which is a bug the reader cannot act on. */
function sentence(key: string): string {
  const said = EN[key]
  if (said === undefined) throw new Error(`no English for ${key}`)
  return said
}

/** Every key in a message, its nested messages included. An instant has no
 *  key and contributes none. */
function keysOf(value: unknown, into: string[]): string[] {
  if (value === null || typeof value !== 'object') return into
  const message = value as { key?: unknown, params?: Record<string, unknown> }
  if (typeof message.key === 'string') into.push(message.key)
  for (const param of Object.values(message.params ?? {})) keysOf(param, into)
  return into
}

/** Every key this module can reach for one report: labels, figures, notes,
 *  caveats, rows and the two standing sentences. */
function everyKey(value: GuildReport): string[] {
  const keys: string[] = [REPORT_SCOPE_NOTE_KEY, REPORT_EMPTY_HEADING_KEY, REPORT_EMPTY_NOTE_KEY]
  for (const figure of [...reportHeadlineFigures(value), ...reportShapeFigures(value)]) {
    keys.push(figure.labelKey)
    keysOf(figure.value, keys)
    for (const note of figure.note) keysOf(note, keys)
  }
  for (const caveat of reportCaveats(value)) {
    keys.push(caveat.labelKey)
    for (const line of caveat.text) keysOf(line, keys)
  }
  for (const row of reportMonthRows(value)) keysOf(row.detail, keys)
  for (const line of reportMonthsNote(value)) keysOf(line, keys)
  keysOf(reportSpanLine(value), keys)
  return keys
}

/** Every sentence this module can produce for one report, in English, so a
 *  test can assert on what none of them says. */
function everySentence(value: GuildReport): string {
  return everyKey(value).map(sentence).join(' ')
}

/* -------------------------------------------------------------------- */
/* The tests                                                             */
/* -------------------------------------------------------------------- */

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
    // A malformed month cannot be ordered or turned into an instant, and
    // would anchor the gap filling at an arbitrary point in history --
    // turning one bad string into a thousand rows of zeros.
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
    // Keys, and named `labelKey` so that nothing puts one on screen by
    // mistake.
    expect(figures.map((figure) => figure.labelKey)).toEqual([
      'admin.reporting.sessionsLabel',
      'admin.reporting.documentedLabel',
      'admin.reporting.recordedLabel',
      'admin.reporting.speechLabel',
    ])
    // A bare number rather than a written one: the locale groups it, so a
    // German reader gets their own separators without this module knowing
    // which they are.
    expect(figures[0]!.value).toBe(42)
    expect(figures[0]!.note).toEqual([
      {
        key: 'admin.reporting.spanBetween',
        params: {
          from: instant('2025-11-04T09:00:00+00:00'),
          to: instant('2026-08-21T12:00:00+00:00'),
        },
      },
    ])
  })

  it('renders a missing total as an absence rather than as no time at all', () => {
    // `null` is what the em dash used to be, and is the better shape for
    // it: a dash was a value as far as everything downstream could tell,
    // so nothing could tell an absence from a figure without comparing
    // against a glyph. The tone is what colours it now.
    const figures = reportHeadlineFigures(report({ recorded_seconds: null, speech_seconds: null }))
    const recorded = figures.find((figure) => figure.key === 'recorded')!
    expect(recorded.value).toBeNull()
    expect(recorded.tone).toBe('absent')
  })

  it('writes a total as a length rather than as a pile of seconds', () => {
    const recorded = reportHeadlineFigures(report({ recorded_seconds: 151200 })).find(
      (figure) => figure.key === 'recorded',
    )!
    expect(recorded.value).toEqual({ key: 'common.durationHours', params: { count: 42 } })
    expect(recorded.tone).toBe('plain')
  })

  it('groups the shape of a meeting apart from how much has happened', () => {
    expect(reportShapeFigures(report()).map((figure) => figure.labelKey)).toEqual([
      'admin.reporting.averageDurationLabel',
      'admin.reporting.longestDurationLabel',
      'admin.reporting.averageParticipantsLabel',
      'admin.reporting.largestMeetingLabel',
      'admin.reporting.participantsLabel',
      'admin.reporting.openLabel',
    ])
  })

  it('writes an average of people to one decimal, without a false one', () => {
    const perMeeting = (value: number | null) =>
      reportShapeFigures(report({ average_participants: value })).find(
        (figure) => figure.key === 'average-participants',
      )!.value
    // Rounded here, because a mean of 4.25 people is a precision the
    // payload does not have. The trailing `.0` that used to be trimmed by
    // hand goes on its own now: a number carries no trailing zero, and the
    // locale's number format writes no digit nobody asked for -- so German
    // gets its comma with it.
    expect(perMeeting(3.84)).toBe(3.8)
    expect(perMeeting(4)).toBe(4)
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
    const reasons: Record<string, string> = {
      'average-duration': 'admin.reporting.absentAverageDuration',
      'longest-duration': 'admin.reporting.absentLongestDuration',
      'average-participants': 'admin.reporting.absentAverageParticipants',
      'largest-meeting': 'admin.reporting.absentLargestMeeting',
    }
    for (const [key, reason] of Object.entries(reasons)) {
      const figure = figures.find((candidate) => candidate.key === key)!
      expect(figure.value).toBeNull()
      expect(figure.tone).toBe('absent')
      // Two sentences rather than one: the second is the same for all four
      // and is a key of its own, so that four copies of it cannot drift
      // apart one edit at a time.
      expect(figure.note).toEqual([{ key: reason }, { key: 'admin.reporting.absentNotZero' }])
    }
    expect(sentence('admin.reporting.absentNotZero')).toContain('not a figure of zero')
  })

  it('never leaves a missing figure without the sentence that explains it', () => {
    // An absent figure with nothing beside it reads as "still loading",
    // which is the one thing this page is not doing.
    for (const figure of [
      ...reportHeadlineFigures(emptyReport()),
      ...reportShapeFigures(emptyReport()),
    ]) {
      expect(figure.note.length).toBeGreaterThan(0)
      for (const line of figure.note) expect(line.key).not.toBe('')
    }
  })

  it('names only keys the locale file actually carries', () => {
    // A key nothing translated renders on the page as itself, which is a
    // bug the reader can see and cannot act on. `sentence` throws for a key
    // `en.json` has never heard of.
    for (const key of everyKey(report({ months: [month({ month: '2026-01' })] }))) {
      expect(() => sentence(key)).not.toThrow()
    }
  })
})

describe('how the documented rate reads', () => {
  it('says how many of this server’s meetings reached a protocol, and how many did not', () => {
    // A rate on its own is read as a property of the software; this is a
    // property of what happened here, so it is written as "n of m". The
    // count that governs the verb is the number *missing* -- "the other one
    // was" against "the other four were" -- and the rest are values beside
    // it.
    expect(reportDocumentedLine(report({ sessions: 42, documented: 38 }))).toEqual([
      {
        key: 'admin.reporting.documentedPartial',
        params: { count: 4, documented: 38, sessions: 42, share: 90 },
      },
    ])
  })

  it('counts a single meeting left unwritten in the singular', () => {
    expect(reportDocumentedLine(report({ sessions: 42, documented: 41 }))).toEqual([
      {
        key: 'admin.reporting.documentedPartial',
        params: { count: 1, documented: 41, sessions: 42, share: 98 },
      },
    ])
  })

  it('says so plainly when every meeting was written up', () => {
    expect(reportDocumentedLine(report({ sessions: 42, documented: 42 }))).toEqual([
      { key: 'admin.reporting.documentedAll', params: { count: 42 } },
    ])
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
    expect(reportDocumentedLine(emptyReport())).toEqual([
      { key: 'admin.reporting.documentedNothing' },
    ])
  })

  it('does not count a meeting still recording as a failure to write one up', () => {
    // A meeting that has not ended cannot have been written up, and
    // blaming the pipeline for the clock is the wrong reading of the same
    // two numbers. A sentence of its own rather than one glued to the end
    // of the previous one: whether German wants it first is not this
    // module's to decide.
    expect(reportDocumentedLine(report({ sessions: 42, documented: 38, open_sessions: 1 }))).toEqual(
      [
        {
          key: 'admin.reporting.documentedPartial',
          params: { count: 4, documented: 38, sessions: 42, share: 90 },
        },
        { key: 'admin.reporting.documentedOpen', params: { count: 1 } },
      ],
    )
  })
})

describe('the meetings still open', () => {
  it('says nothing is open rather than letting the figure vanish', () => {
    // A figure that appears only when it is bad news is a figure whose
    // absence has to be interpreted.
    expect(reportOpenSessionsLine(report({ open_sessions: 0 }))).toEqual([
      { key: 'admin.reporting.openNone' },
    ])
  })

  it('names the second reading of an open session', () => {
    // One meeting open for ten minutes is a meeting; one open since
    // Tuesday is a session that never closed, and the number alone cannot
    // tell them apart.
    expect(reportOpenSessionsLine(report({ open_sessions: 1 }))).toEqual([
      { key: 'admin.reporting.openSome', params: { count: 1 } },
      { key: 'admin.reporting.openAmbiguous' },
    ])
    expect(sentence('admin.reporting.openAmbiguous')).toContain('a session that never closed')
    expect(sentence('admin.reporting.openAmbiguous')).toContain(
      'a session open for days is the second',
    )
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
    // infer from its own silence. Said whether or not anything is open.
    expect(reportRecordedLine(report({ open_sessions: 2 }))).toEqual([
      { key: 'admin.reporting.recordedScope' },
      { key: 'admin.reporting.recordedOpenExcluded', params: { count: 2 } },
    ])
    expect(reportRecordedLine(report({ open_sessions: 0 }))).toEqual([
      { key: 'admin.reporting.recordedAllEnded' },
    ])
    expect(sentence('admin.reporting.recordedAllEnded')).toContain('all of which have ended')
  })
})

describe('what the speaking time is a total of', () => {
  it('refuses to let a hole in the measurement read as a quiet server', () => {
    // The single most misreadable number on this page. `speech_seconds` is
    // a SUM over a nullable column and skips the nulls in silence, so a
    // small figure under a large recorded total reads as "these meetings
    // were quiet" to anybody not told otherwise.
    //
    // The count that governs the verb is the unmeasured one -- "one was
    // never measured" against "seven were" -- and the total is a value
    // alongside it.
    expect(reportSpeechCaveat(report({ tracks: 160, unmeasured_tracks: 7 }))).toEqual([
      { key: 'admin.reporting.speechPartlyMeasured', params: { count: 7, tracks: 160 } },
      { key: 'admin.reporting.speechPartlyMeasuredTotal', params: { count: 153 } },
      { key: 'admin.reporting.speechNotQuiet' },
    ])
    // The one sentence on this page that exists to rule out a specific
    // wrong reading. It is worth checking that it still says it.
    expect(sentence('admin.reporting.speechNotQuiet')).toBe(
      'It does not mean this server was quiet.',
    )
  })

  it('says a total covers everything when it does', () => {
    expect(reportSpeechCaveat(report({ tracks: 160, unmeasured_tracks: 0 }))).toEqual([
      { key: 'admin.reporting.speechAllMeasured', params: { count: 160 } },
    ])
  })

  it('says a measurement was never taken when none of it was', () => {
    // Zero measured tracks is not a silent server; it is a server whose
    // recordings predate the columns that hold the figure.
    expect(reportSpeechCaveat(report({ tracks: 160, unmeasured_tracks: 160 }))).toEqual([
      { key: 'admin.reporting.speechNoneMeasured', params: { count: 160 } },
    ])
    expect(sentence('admin.reporting.speechNoneMeasured')).toContain(
      'a measurement that was never taken, not as a server that was quiet',
    )
  })

  it('says there is nothing to measure when nothing was recorded', () => {
    expect(reportSpeechCaveat(emptyReport())).toEqual([{ key: 'admin.reporting.speechNoTracks' }])
    expect(sentence('admin.reporting.speechNoTracks')).toContain('missing rather than zero')
  })

  it('carries the caveat with the figure, not only in a panel', () => {
    // A footnote is read once, by the person who was already being
    // careful.
    const speech = reportHeadlineFigures(report({ tracks: 160, unmeasured_tracks: 7 })).find(
      (figure) => figure.key === 'speech',
    )!
    expect(speech.note).toContainEqual({ key: 'admin.reporting.speechNotQuiet' })
  })
})

describe('which calendar the months were cut in', () => {
  it('names the zone rather than letting the reader assume theirs', () => {
    // An IANA zone name is not a word in any language -- `Europe/Berlin` is
    // `Europe/Berlin` -- so it travels as the string it is, into a hole the
    // sentence keeps for it.
    expect(reportTimezoneNote(report({ timezone: 'Europe/Berlin' }))).toEqual({
      key: 'admin.reporting.timezoneKnown',
      params: { zone: 'Europe/Berlin' },
    })
    expect(sentence('admin.reporting.timezoneKnown')).toContain('{zone}')
    expect(sentence('admin.reporting.timezoneKnown')).toContain('not in UTC and not in yours')
  })

  it('says why the server does not bucket by UTC', () => {
    // A meeting at 00:30 belongs to the month the people in it think it
    // does. The reason is the sentence's, so it is checked in the sentence.
    expect(sentence('admin.reporting.timezoneKnown')).toContain(
      'A meeting that begins at 00:30 belongs to the month the people in it think it does',
    )
  })

  it('warns that the instants on the page use a different clock again', () => {
    // A page rendered on a server cannot know the reader's zone, so the
    // timestamps stay in UTC while the months do not. Two clocks on one
    // page is a seam worth naming.
    expect(sentence('admin.reporting.timezoneKnown')).toContain('written in UTC all the same')
  })

  it('reports the uncertainty when the API named no zone at all', () => {
    expect(reportTimezoneNote(report({ timezone: '' }))).toEqual({
      key: 'admin.reporting.timezoneUnknown',
    })
    expect(sentence('admin.reporting.timezoneUnknown')).toContain('did not say which calendar')
    expect(sentence('admin.reporting.timezoneUnknown')).toContain('do not assume it is yours')
  })

  it('puts both caveats where the figures are, in a fixed order', () => {
    expect(reportCaveats(report()).map((caveat) => caveat.key)).toEqual(['speech', 'timezone'])
    expect(reportCaveats(report()).map((caveat) => caveat.labelKey)).toEqual([
      'admin.reporting.caveatSpeechLabel',
      'admin.reporting.caveatTimezoneLabel',
    ])
  })
})

describe('the span the report covers', () => {
  it('writes both ends in UTC and says which zone that is', () => {
    // The zone is named by the `utcMoment` format rather than by this
    // module, which is what lets it be named in whichever language is
    // reading without two instants being written twice.
    expect(reportSpanLine(report())).toEqual({
      key: 'admin.reporting.spanBetween',
      params: {
        from: instant('2025-11-04T09:00:00+00:00'),
        to: instant('2026-08-21T12:00:00+00:00'),
      },
    })
  })

  it('says there is no span rather than printing a dash for one', () => {
    expect(reportSpanLine(emptyReport())).toEqual({ key: 'admin.reporting.spanNothing' })
  })

  it('reads a single meeting as one meeting, not as a range of no length', () => {
    expect(
      reportSpanLine(
        report({
          first_session_at: '2026-08-21T12:00:00+00:00',
          last_session_at: '2026-08-21T12:00:00+00:00',
        }),
      ),
    ).toEqual({
      key: 'admin.reporting.spanOne',
      params: { at: instant('2026-08-21T12:00:00+00:00') },
    })
  })

  it('says so when only one end of the span is known', () => {
    // Each end can be missing on its own, and each case gets its own
    // sentence rather than an em dash standing in for half a range.
    expect(reportSpanLine(report({ last_session_at: null }))).toEqual({
      key: 'admin.reporting.spanPartial',
      params: { at: instant('2025-11-04T09:00:00+00:00') },
    })
    expect(reportSpanLine(report({ first_session_at: null }))).toEqual({
      key: 'admin.reporting.spanPartial',
      params: { at: instant('2026-08-21T12:00:00+00:00') },
    })
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
    expect(rows[1]!.recorded).toBeNull()
    expect(rows[1]!.detail).toEqual({
      key: 'admin.reporting.monthSilent',
      params: { month: monthInstant('2026-04') },
    })
  })

  it('invents no months before the first or after the last', () => {
    // A server is not silent in the months before it existed, and rows
    // there would be inventing history rather than showing a gap in it.
    const rows = reportMonthRows(report({ months: [month({ month: '2026-08' })] }))
    expect(rows.map((row) => row.month)).toEqual(['2026-08'])
  })

  it('has no rows at all for a server with no months', () => {
    expect(reportMonthRows(emptyReport())).toEqual([])
    expect(reportMonthsNote(emptyReport())).toEqual([{ key: 'admin.reporting.monthsNothing' }])
  })

  it('says what it did about the gaps, either way', () => {
    const filled = report({ months: [month({ month: '2026-03' }), month({ month: '2026-06' })] })
    expect(reportMonthsNote(filled)).toEqual([
      { key: 'admin.reporting.monthsOldestFirst' },
      { key: 'admin.reporting.monthsSomeSilent', params: { count: 2 } },
    ])
    const solid = report({ months: [month({ month: '2026-03' }), month({ month: '2026-04' })] })
    expect(reportMonthsNote(solid)).toEqual([
      { key: 'admin.reporting.monthsOldestFirst' },
      { key: 'admin.reporting.monthsAllBusy' },
    ])
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
    // The limit travels as the years it is, so the sentence can say it
    // without this module knowing the word for "years".
    expect(reportMonthsNote(far)).toEqual([
      {
        key: 'admin.reporting.monthsTooLong',
        params: { years: REPORT_MONTH_FILL_LIMIT / 12 },
      },
    ])
    expect(sentence('admin.reporting.monthsTooLong')).toContain(
      'not necessarily neighbouring months',
    )
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
    // A bar with no text is a bar only its author can read. The month, the
    // count and the length are three holes in one sentence rather than
    // three fragments glued together -- German is free to move them.
    const rows = reportMonthRows(
      report({
        months: [month({ month: '2026-08', sessions: 5, recorded_seconds: 18000, documented: 5 })],
      }),
    )
    expect(rows[0]!.recorded).toEqual({ key: 'common.durationHours', params: { count: 5 } })
    expect(rows[0]!.detail).toEqual({
      key: 'admin.reporting.monthDetail',
      params: {
        month: monthInstant('2026-08'),
        count: 5,
        recorded: { key: 'common.durationHours', params: { count: 5 } },
        documented: 5,
      },
    })
  })

  it('names a month by the instant it begins at rather than by its key', () => {
    // This replaces a table of English month names kept in the module. The
    // names are `Intl`'s now, through the `monthYear` format, which is
    // pinned to UTC -- so the first of August cannot slide back into July
    // on the way to the screen.
    const rows = reportMonthRows(report({ months: [month({ month: '2026-08' })] }))
    expect(rows[0]!.at).toEqual(new Date('2026-08-01T00:00:00.000Z'))
    // The raw key stays too: it is unique across the rows, which is what
    // keys a `v-for` safely, and it is what a reader matches against the
    // payload.
    expect(rows[0]!.month).toBe('2026-08')
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
    expect(rows[0]!.detail).toEqual({
      key: 'admin.reporting.monthDetail',
      params: {
        month: monthInstant('2026-01'),
        count: 0,
        recorded: { key: 'common.durationSeconds', params: { count: 0 } },
        documented: 0,
      },
    })
  })

  it('keys every row uniquely, so a duplicate month cannot render twice', () => {
    const rows = reportMonthRows(
      report({ months: [month({ month: '2026-01' }), month({ month: '2026-01', sessions: 9 })] }),
    )
    expect(rows).toHaveLength(1)
    expect(rows[0]!.sessions).toBe(9)
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
    // A key rather than a sentence now, so the empty state can be read in
    // German too -- but the sentence behind it still has to say the three
    // things it was written to say.
    expect(REPORT_EMPTY_HEADING_KEY).toBe('admin.reporting.emptyHeading')
    expect(REPORT_EMPTY_NOTE_KEY).toBe('admin.reporting.emptyNote')
    expect(sentence(REPORT_EMPTY_NOTE_KEY)).toContain('shows no figures rather than a grid of zeros')
    expect(sentence(REPORT_EMPTY_NOTE_KEY)).toContain('a zero would be a measurement')
    expect(sentence(REPORT_EMPTY_NOTE_KEY)).toContain('Once a meeting happens')
  })
})

describe('what this report is not about', () => {
  it('says outright that there is no per-person breakdown behind it', () => {
    // A per-person readout of meeting attendance and speaking time is a
    // means of monitoring conduct and performance at work, which is a
    // works-council matter rather than a console feature. The payload
    // carries no names and no ids; the framing has to match, in both
    // languages.
    expect(REPORT_SCOPE_NOTE_KEY).toBe('admin.reporting.scopeNote')
    expect(sentence(REPORT_SCOPE_NOTE_KEY)).toContain(
      'about the server as a whole and never about the people in it',
    )
    expect(sentence(REPORT_SCOPE_NOTE_KEY)).toContain('no per-person breakdown behind it')
    expect(sentence(REPORT_SCOPE_NOTE_KEY)).toContain('Counts of people are counts, and stop there.')
  })

  it('describes the participant count as a count and nothing more', () => {
    const people = reportShapeFigures(report()).find((figure) => figure.key === 'participants')!
    expect(people.value).toBe(12)
    expect(people.note).toEqual([{ key: 'admin.reporting.participantsNote' }])
    expect(sentence('admin.reporting.participantsNote')).toContain('A count and nothing else')
    expect(sentence('admin.reporting.participantsNote')).toContain(
      'does not send this page their names',
    )
  })

  it('never hints that a per-person readout is on its way', () => {
    // Every sentence this module can reach for one report, resolved through
    // `en.json` and checked at once: an interface that promises a breakdown
    // is as much a promise as one that ships it. Checked in English because
    // English is the source of truth -- a German sentence is a translation
    // of one of these, and `i18n.spec.ts` is what keeps the two in step.
    const prose = everySentence(
      report({ months: [month({ month: '2026-01' }), month({ month: '2026-03' })], open_sessions: 1 }),
    )
    for (const forbidden of [
      ' soon',
      'coming',
      'per person',
      'per-person breakdown of',
      'who spoke most',
      'top speaker',
    ]) {
      expect(prose.toLowerCase()).not.toContain(forbidden.toLowerCase())
    }
  })
})

describe('when the API says no', () => {
  it('says a session has ended rather than that the server is gone', () => {
    expect(describeReportError(failure(401))).toEqual({ key: 'admin.reporting.errorSession' })
  })

  it('names what an administrator is when it refuses one who is not', () => {
    expect(describeReportError(failure(403))).toEqual({ key: 'admin.reporting.errorNotAdmin' })
    // The setting is a field in a config file and is spelled the same in
    // every language, so the sentence that names it has to keep it.
    expect(sentence('admin.reporting.errorNotAdmin')).toContain('admin_role_id')
  })

  it('covers both readings of a 404 without guessing which', () => {
    // The API answers 404 both for a server that does not exist and for
    // one the caller does not administer, on purpose: it will not confirm
    // the existence of a server to somebody with no business there.
    expect(describeReportError(failure(404))).toEqual({
      key: 'admin.reporting.errorUnknownGuild',
    })
    expect(sentence('admin.reporting.errorUnknownGuild')).toContain(
      'does not know this server, or you no longer administer it',
    )
    expect(sentence('admin.reporting.errorUnknownGuild')).toContain('answers the same way to both')
  })

  it('separates a refusal from never having reached the API at all', () => {
    // `ApiError` uses 0 for "never got a response", which must not read as
    // an answer from the API.
    expect(describeReportError(failure(0))).toEqual({ key: 'admin.reporting.errorUnreachable' })
    expect(describeReportError(null)).toEqual({ key: 'admin.reporting.errorUnreachable' })
  })

  it('names an unexpected status rather than inventing a reason for it', () => {
    // The status travels as a string. It is a number without being a
    // quantity, and a quantity would be written in the locale's grouping --
    // there is no such status as 1,000, and none as 1.000 either.
    expect(describeReportError(failure(503))).toEqual({
      key: 'admin.reporting.errorStatus',
      params: { status: '503' },
    })
    expect(sentence('admin.reporting.errorStatus')).toContain('Nothing is known about why')
  })

  it('reads a raw fetch failure’s statusCode as well as an ApiError’s status', () => {
    expect(describeReportError({ statusCode: 403 })).toEqual({
      key: 'admin.reporting.errorNotAdmin',
    })
  })
})
