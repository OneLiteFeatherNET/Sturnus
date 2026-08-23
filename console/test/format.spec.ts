/**
 * How the dashboard's figures read, and what it says when it does not
 * know.
 *
 * These functions no longer decide on a language, so this file no longer
 * asserts on one. What comes back is a key and, where the sentence counts
 * something, the numbers it counts -- and both halves are pinned, because
 * the params are part of the decision. A test that checked only the key
 * would let the count that chooses "one track" over "three tracks" quietly
 * change.
 *
 * Every expectation is still written out as a literal. Asserting against
 * the module's own constants would pin the two together rather than pin the
 * output: a key renamed on both sides at once would still pass, and the
 * locale file that has to carry it would not have been asked.
 *
 * Nothing here builds a Vue application or an i18n instance, which is the
 * whole point of the module being pure. What these keys read like in
 * English is `i18n/locales/en.json`'s business.
 */
import { describe, expect, it } from 'vitest'

import {
  describeFailure,
  describeSession,
  failureStatus,
  figureCount,
  figureDuration,
  figureMoment,
  formatCount,
  formatMoment,
  hasNothingRecorded,
  isSessionFailure,
  sessionHighlights,
  speechNote,
  summaryFigures,
  type DashboardSummary,
  type SessionPointer,
} from '../app/utils/format'

/** A snowflake past `Number.MAX_SAFE_INTEGER`, which is every real one. */
const SNOWFLAKE = '1408912345678901234'

function pointer(overrides: Partial<SessionPointer> = {}): SessionPointer {
  return {
    id: '900000000000000001',
    started_at: '2026-08-14T09:07:00Z',
    ended_at: '2026-08-14T11:21:00Z',
    duration_seconds: 8040,
    channel_id: SNOWFLAKE,
    channel_name: 'standup',
    ...overrides,
  }
}

function summary(overrides: Partial<DashboardSummary> = {}): DashboardSummary {
  return {
    total_speech_seconds: 8040,
    unmeasured_tracks: 0,
    sessions_attended: 12,
    sessions_with_protocol: 9,
    people_spoken_with: 7,
    words_transcribed: 48213,
    longest_session: null,
    most_recent_session: null,
    first_session: null,
    ...overrides,
  }
}

describe('a duration', () => {
  it('reads as hours and minutes rather than a pile of seconds', () => {
    // Two named holes rather than one glued sentence: German is free to put
    // the minutes first, and neither language is counting anything here --
    // "2 h 14 min" has no singular to choose, which is why this key carries
    // `hours` and `minutes` and no `count`.
    expect(figureDuration(8040)).toEqual({
      key: 'common.durationHoursMinutes',
      params: { hours: 2, minutes: 14 },
    })
  })

  it('drops a unit that would read as zero', () => {
    // A single unit does have a singular to choose, so the figure travels
    // as `count` and the locale file decides what that does to the word.
    expect(figureDuration(7200)).toEqual({ key: 'common.durationHours', params: { count: 2 } })
    expect(figureDuration(120)).toEqual({ key: 'common.durationMinutes', params: { count: 2 } })
  })

  it('keeps seconds while they still matter', () => {
    expect(figureDuration(45)).toEqual({ key: 'common.durationSeconds', params: { count: 45 } })
    expect(figureDuration(90)).toEqual({
      key: 'common.durationMinutesSeconds',
      params: { minutes: 1, seconds: 30 },
    })
  })

  it('renders an unmeasured duration as an absence rather than zero', () => {
    // Null is "nobody ever measured this"; zero is "measured, and it was
    // silent". A dashboard that printed 0 s for the first would be stating
    // something it does not know.
    //
    // The absence is now `null` rather than an em dash inside a string,
    // which is the better shape for the same distinction: a dash was a
    // value as far as everything downstream could tell, so a caller could
    // not style it, count it or refuse to render a card for it without
    // comparing against a glyph.
    expect(figureDuration(null)).toBeNull()
    expect(figureDuration(undefined)).toBeNull()
  })

  it('renders a measured silence as zero, because that is a fact', () => {
    expect(figureDuration(0)).toEqual({ key: 'common.durationSeconds', params: { count: 0 } })
  })

  it('refuses a duration that cannot be true instead of inventing one', () => {
    // A negative or non-finite figure is a defect upstream. Answering with
    // no figure says "no figure"; showing "-3 s" would say the person spoke
    // for less than no time.
    expect(figureDuration(-3)).toBeNull()
    expect(figureDuration(Number.NaN)).toBeNull()
    expect(figureDuration(Number.POSITIVE_INFINITY)).toBeNull()
  })

  it('rounds a fractional second rather than printing it', () => {
    expect(figureDuration(59.6)).toEqual({ key: 'common.durationMinutes', params: { count: 1 } })
    expect(figureDuration(0.4)).toEqual({ key: 'common.durationSeconds', params: { count: 0 } })
  })

  it('counts hours past a day rather than starting a day column', () => {
    // 30 h is a figure somebody can compare to last month's. "1 d 6 h"
    // makes them do arithmetic before they can.
    expect(figureDuration(108000)).toEqual({ key: 'common.durationHours', params: { count: 30 } })
  })
})

describe('a count the locale will write', () => {
  it('hands the number over ungrouped, so the reader gets their own separators', () => {
    // This used to group by hand and say so, because `Intl` formats for the
    // runtime's locale and would have disagreed between a server render and
    // a browser set to German. The locale is chosen now rather than
    // resolved, and it is the same on both sides, so the grouping can be
    // the reader's: 48,213 in English and 48.213 in German, decided in one
    // place instead of agreed on in two.
    expect(figureCount(48213)).toBe(48213)
    expect(figureCount(1000000)).toBe(1000000)
  })

  it('leaves a small figure alone', () => {
    expect(figureCount(0)).toBe(0)
    expect(figureCount(999)).toBe(999)
  })

  it('rounds a fractional count rather than handing on a decimal', () => {
    expect(figureCount(2.4)).toBe(2)
    expect(figureCount(2.6)).toBe(3)
  })

  it('renders an absent count as an absence rather than zero', () => {
    // `null` where the em dash used to be, and for the same reason it is
    // `null` for a duration: an absence that is a value cannot be told from
    // a value by anything that receives it.
    expect(figureCount(null)).toBeNull()
    expect(figureCount(undefined)).toBeNull()
  })

  it('refuses a count that cannot be true', () => {
    expect(figureCount(-1)).toBeNull()
    expect(figureCount(Number.NaN)).toBeNull()
  })
})

describe('a count, in the English the untranslated pages still read', () => {
  // `~/utils/queue` and `~/utils/consents` still build English sentences by
  // hand, so `formatCount` stays exactly as it was and stays tested exactly
  // as it was. It goes when the last of those three pages is translated.
  it('groups thousands so a large figure can be read at a glance', () => {
    expect(formatCount(48213)).toBe('48,213')
    expect(formatCount(1000000)).toBe('1,000,000')
  })

  it('leaves a small figure alone', () => {
    expect(formatCount(0)).toBe('0')
    expect(formatCount(999)).toBe('999')
  })

  it('renders an absent count as an em dash rather than zero', () => {
    expect(formatCount(null)).toBe('—')
    expect(formatCount(undefined)).toBe('—')
  })

  it('refuses a count that cannot be true', () => {
    expect(formatCount(-1)).toBe('—')
    expect(formatCount(Number.NaN)).toBe('—')
  })
})

describe('a moment', () => {
  it('reads in UTC, and says so', () => {
    // The same instant on the server and in the browser. Formatting in the
    // viewer's zone would render one time during the server render and a
    // different one on hydration, which Vue reports as a mismatch and the
    // reader sees as a flicker. Naming the zone is the honest half of that
    // trade, and the `utcMoment` format is where it is named -- in
    // whichever language is reading.
    expect(figureMoment('2026-08-14T09:07:00Z')).toEqual({
      at: new Date('2026-08-14T09:07:00Z'),
      format: 'utcMoment',
    })
  })

  it('converts an offset rather than trusting the digits in the string', () => {
    expect(figureMoment('2026-08-14T11:07:00+02:00')?.at.toISOString()).toBe(
      '2026-08-14T09:07:00.000Z',
    )
  })

  it('renders an absent or unparseable moment as an absence', () => {
    // Not an em dash any more. A caller that has no instant should be able
    // to leave the line out rather than print a dash where a date goes.
    expect(figureMoment(null)).toBeNull()
    expect(figureMoment('not a date')).toBeNull()
  })
})

describe('a moment, in the English the untranslated pages still read', () => {
  // The other half of what `~/utils/queue` and `~/utils/consents` read; see
  // `formatCount` above.
  it('reads in UTC, and says so', () => {
    expect(formatMoment('2026-08-14T09:07:00Z')).toBe('14 Aug 2026, 09:07 UTC')
  })

  it('converts an offset rather than trusting the digits in the string', () => {
    expect(formatMoment('2026-08-14T11:07:00+02:00')).toBe('14 Aug 2026, 09:07 UTC')
  })

  it('renders an absent or unparseable moment as an em dash', () => {
    expect(formatMoment(null)).toBe('—')
    expect(formatMoment('not a date')).toBe('—')
  })
})

describe('naming the channel a session happened in', () => {
  // Asserted through `describeSession` rather than directly. The naming is
  // no longer exported: `~/utils/recordings` exports a `channelLabel` of
  // its own, and for as long as both were exported Nuxt auto-imported one
  // of them into every component and dropped the other by file order.
  it('prefers the name people know it by', () => {
    // A channel's own name is not a word in any language, so it travels as
    // the string it is rather than as a key.
    expect(describeSession(pointer({ channel_name: 'standup' }))?.channel).toBe('#standup')
  })

  it('falls back to the id, in full, when the name was never captured', () => {
    // A deleted channel has no name to fetch. The full id is the only
    // thing left that identifies it -- a shortened one names nothing and
    // cannot be searched for. The word "Channel" around it is a word, so
    // that half is a key.
    expect(describeSession(pointer({ channel_name: null }))?.channel).toEqual({
      key: 'recordings.channelById',
      params: { id: SNOWFLAKE },
    })
  })

  it('keeps every digit of a snowflake, which a number would not', () => {
    const channel = describeSession(pointer({ channel_name: null }))?.channel as {
      params: { id: string }
    }
    // A string, and stays one: a snowflake past `Number.MAX_SAFE_INTEGER`
    // loses its last digits the moment anything treats it as a quantity.
    expect(channel.params.id).toBe(SNOWFLAKE)
    expect(channel.params.id).not.toBe('1408912345678901000')
  })
})

describe('describing one session', () => {
  it('gives the channel, the moment and the length', () => {
    expect(describeSession(pointer())).toEqual({
      id: '900000000000000001',
      channel: '#standup',
      when: { at: new Date('2026-08-14T09:07:00Z'), format: 'utcMoment' },
      duration: { key: 'common.durationHoursMinutes', params: { hours: 2, minutes: 14 } },
    })
  })

  it('says an unfinished session has no length rather than calling it empty', () => {
    // A session still running, or one whose end was never recorded, has no
    // duration. Zero would claim it was over the moment it began, and the
    // absence travels as an absence rather than as a dash the card would
    // have to recognise by its glyph.
    expect(
      describeSession(pointer({ duration_seconds: null, ended_at: null }))?.duration,
    ).toBeNull()
  })

  it('describes nothing when there is no session to describe', () => {
    expect(describeSession(null)).toBeNull()
  })
})

describe('the note under the speech total', () => {
  it('stays quiet when the total covers everything', () => {
    expect(speechNote(8040, 0)).toBeNull()
  })

  it('says what a total left out, because a silent omission is worse', () => {
    expect(speechNote(8040, 3)).toEqual({
      key: 'dashboard.speechSkipped',
      params: { count: 3 },
    })
  })

  it('counts a single omitted track in the singular', () => {
    // The `if` that used to choose between "1 track ... is" and "3 tracks
    // ... are" was an English decision made where German could not reach
    // it. The count is what travels now, and each locale file says what one
    // of them does to the sentence.
    expect(speechNote(8040, 1)).toEqual({
      key: 'dashboard.speechSkipped',
      params: { count: 1 },
    })
  })

  it('explains an absent total instead of leaving a bare dash', () => {
    expect(speechNote(null, 4)).toEqual({
      key: 'dashboard.speechNothingMeasured',
      params: { count: 4 },
    })
  })

  it('explains an absent total even when nothing was skipped', () => {
    // No params at all: the sentence counts nothing, so there is nothing
    // for a locale to agree with.
    expect(speechNote(null, 0)).toEqual({ key: 'dashboard.speechNothingYet' })
  })
})

describe('the figures the dashboard shows', () => {
  it('leads with how long this person has spoken', () => {
    const [first] = summaryFigures(summary())
    // A key, and named `labelKey` so that nothing puts it on screen by
    // mistake.
    expect(first?.labelKey).toBe('dashboard.speechLabel')
    expect(first?.value).toEqual({
      key: 'common.durationHoursMinutes',
      params: { hours: 2, minutes: 14 },
    })
  })

  it('shows the five figures the endpoint can answer for', () => {
    expect(summaryFigures(summary()).map((f) => f.key)).toEqual([
      'speech',
      'sessions',
      'protocols',
      'people',
      'words',
    ])
  })

  it('carries the unmeasured warning on the figure it applies to', () => {
    const figures = summaryFigures(summary({ total_speech_seconds: null, unmeasured_tracks: 2 }))
    expect(figures[0]?.value).toBeNull()
    expect(figures[0]?.note).toEqual({
      key: 'dashboard.speechNothingMeasured',
      params: { count: 2 },
    })
  })

  it('says how many of the attended sessions produced a protocol', () => {
    const figures = summaryFigures(summary({ sessions_attended: 12, sessions_with_protocol: 9 }))
    const protocols = figures.find((f) => f.key === 'protocols')
    // A bare number rather than a rendered one: the denominator belongs
    // next to the numerator, and both are written by the same locale.
    expect(protocols?.value).toBe(9)
    expect(protocols?.note).toEqual({ key: 'dashboard.protocolsNote', params: { count: 12 } })
  })

  it('counts a single attended session in the singular', () => {
    const figures = summaryFigures(summary({ sessions_attended: 1, sessions_with_protocol: 1 }))
    expect(figures.find((f) => f.key === 'protocols')?.note).toEqual({
      key: 'dashboard.protocolsNote',
      params: { count: 1 },
    })
  })

  it('leaves the denominator off entirely when no session was attended', () => {
    // "of 0 sessions attended" under a protocol count is a sentence that
    // contradicts itself.
    const figures = summaryFigures(summary({ sessions_attended: 0, sessions_with_protocol: 0 }))
    expect(figures.find((f) => f.key === 'protocols')?.note).toBeNull()
  })

  it('hands a large word count over for the locale to group', () => {
    expect(summaryFigures(summary()).find((f) => f.key === 'words')?.value).toBe(48213)
  })
})

describe('the three sessions worth pointing at', () => {
  it('names each one it has', () => {
    const highlights = sessionHighlights(
      summary({
        most_recent_session: pointer({ channel_name: 'standup' }),
        longest_session: pointer({ channel_name: 'retro', duration_seconds: 14400 }),
        first_session: pointer({ channel_name: 'kickoff' }),
      }),
    )
    expect(highlights.map((h) => h.labelKey)).toEqual([
      'dashboard.highlightMostRecent',
      'dashboard.highlightLongest',
      'dashboard.highlightFirst',
    ])
    expect(highlights[1]?.session.duration).toEqual({
      key: 'common.durationHours',
      params: { count: 4 },
    })
  })

  it('omits a highlight the endpoint had no session for', () => {
    // Three empty cards labelled "Longest", "First" and "Most recent"
    // would suggest the data is loading rather than absent.
    const highlights = sessionHighlights(summary({ most_recent_session: pointer() }))
    expect(highlights.map((h) => h.labelKey)).toEqual(['dashboard.highlightMostRecent'])
  })

  it('points at nothing when there is nothing to point at', () => {
    expect(sessionHighlights(summary())).toEqual([])
  })
})

describe('deciding whether there is anything to show at all', () => {
  it('treats somebody who has never been recorded as empty', () => {
    expect(
      hasNothingRecorded(
        summary({
          total_speech_seconds: null,
          sessions_attended: 0,
          sessions_with_protocol: 0,
          people_spoken_with: 0,
          words_transcribed: 0,
        }),
      ),
    ).toBe(true)
  })

  it('is not empty for somebody who attended a session and said nothing', () => {
    // Measured silence is a result. It deserves a grid of zeros, not an
    // invitation to join a channel they have already been in.
    expect(
      hasNothingRecorded(
        summary({
          total_speech_seconds: 0,
          sessions_attended: 1,
          sessions_with_protocol: 0,
          people_spoken_with: 0,
          words_transcribed: 0,
        }),
      ),
    ).toBe(false)
  })

  it('is not empty when a figure contradicts a zero session count', () => {
    // An attendance count of zero next to 400 transcribed words is a
    // defect somewhere upstream. Showing the figures makes it visible;
    // showing the empty state would hide it behind an invitation.
    expect(
      hasNothingRecorded(
        summary({
          total_speech_seconds: null,
          sessions_attended: 0,
          sessions_with_protocol: 0,
          people_spoken_with: 0,
          words_transcribed: 400,
        }),
      ),
    ).toBe(false)
  })

  it('is not empty when a session pointer exists despite the counts', () => {
    expect(
      hasNothingRecorded(
        summary({
          total_speech_seconds: null,
          sessions_attended: 0,
          sessions_with_protocol: 0,
          people_spoken_with: 0,
          words_transcribed: 0,
          most_recent_session: pointer(),
        }),
      ),
    ).toBe(false)
  })
})

describe('explaining a failed load', () => {
  it('names the status the API answered with', () => {
    // The status travels as a string. It is a number without being a
    // quantity, and a quantity would be written in the locale's grouping --
    // there is no such status as 1,000, and no such status as 1.000 either.
    expect(describeFailure({ statusCode: 503 })).toEqual({
      key: 'dashboard.failureStatus',
      params: { code: '503' },
    })
  })

  it('treats a refused session as a sign-in problem rather than a failure', () => {
    // Somebody whose cookie expired between the render and this call is
    // not looking at a broken console. Telling them to try again would
    // send them round a loop that cannot succeed.
    expect(describeFailure({ statusCode: 401 })).toEqual({ key: 'dashboard.failureSession' })
    expect(describeFailure({ statusCode: 403 })).toEqual({ key: 'dashboard.failureSession' })
  })

  it('says which side is behind when the endpoint is not there', () => {
    // The console and the API ship as two images and can be deployed
    // apart. "404" alone would send somebody looking for a bug in their
    // own account.
    expect(describeFailure({ statusCode: 404 })).toEqual({ key: 'dashboard.failureNoDashboard' })
  })

  it('says the API was unreachable when there is no status at all', () => {
    expect(describeFailure(new Error('fetch failed'))).toEqual({
      key: 'dashboard.failureUnreachable',
    })
    expect(describeFailure(null)).toEqual({ key: 'dashboard.failureUnreachable' })
  })

  it('never repeats the internal address back to the browser', () => {
    // The server-side render calls the API through its in-cluster Service.
    // `$fetch` puts that URL in the message it throws, and echoing it into
    // the page would publish an internal hostname to anybody who loads a
    // failing dashboard. Checked over the whole message, params included:
    // the URL would reach the page through a param as readily as through a
    // sentence.
    const leak = {
      statusCode: 500,
      message: '[GET] "http://sturnus-api:8080/api/dashboard": 500 Internal Server Error',
    }
    const described = JSON.stringify(describeFailure(leak))
    expect(described).not.toContain('sturnus-api')
    expect(described).not.toContain('http')
    expect(describeFailure(leak)).toEqual({
      key: 'dashboard.failureStatus',
      params: { code: '500' },
    })
  })
})

describe('deciding what to offer after a failure', () => {
  it('offers a fresh sign-in when the session was refused', () => {
    expect(isSessionFailure({ statusCode: 401 })).toBe(true)
    expect(isSessionFailure({ statusCode: 403 })).toBe(true)
  })

  it('offers a retry for anything that might succeed on a second try', () => {
    // A 503 is a restarting API; a network failure is a moment ago. Both
    // are worth one button press. A refused session is not.
    expect(isSessionFailure({ statusCode: 503 })).toBe(false)
    expect(isSessionFailure(new Error('fetch failed'))).toBe(false)
    expect(isSessionFailure(null)).toBe(false)
  })
})

describe('the status behind a failure', () => {
  it('reads the status `$fetch` reports', () => {
    expect(failureStatus({ statusCode: 503 })).toBe(503)
  })

  it('reads the status a response object reports', () => {
    expect(failureStatus({ status: 404 })).toBe(404)
  })

  it('has no status for a call that never reached the API', () => {
    // Null rather than a stand-in number: 0 or 500 would read as an answer
    // the API gave, and it gave none.
    expect(failureStatus(new Error('fetch failed'))).toBeNull()
    expect(failureStatus(null)).toBeNull()
    expect(failureStatus(undefined)).toBeNull()
  })
})
