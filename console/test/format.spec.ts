/**
 * How the dashboard's figures read, and what it says when it does not
 * know.
 *
 * Every expectation below is written out as a literal. Asserting against
 * the module's own constant would pin the two together rather than pin the
 * output: an em dash quietly replaced by a hyphen would still pass.
 */
import { describe, expect, it } from 'vitest'

import {
  channelLabel,
  describeFailure,
  describeSession,
  failureStatus,
  formatCount,
  formatDuration,
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
    expect(formatDuration(8040)).toBe('2 h 14 min')
  })

  it('drops a unit that would read as zero', () => {
    expect(formatDuration(7200)).toBe('2 h')
    expect(formatDuration(120)).toBe('2 min')
  })

  it('keeps seconds while they still matter', () => {
    expect(formatDuration(45)).toBe('45 s')
    expect(formatDuration(90)).toBe('1 min 30 s')
  })

  it('renders an unmeasured duration as an em dash rather than zero', () => {
    // Null is "nobody ever measured this"; zero is "measured, and it was
    // silent". A dashboard that printed 0 s for the first would be stating
    // something it does not know.
    expect(formatDuration(null)).toBe('—')
    expect(formatDuration(undefined)).toBe('—')
  })

  it('renders a measured silence as zero, because that is a fact', () => {
    expect(formatDuration(0)).toBe('0 s')
  })

  it('refuses a duration that cannot be true instead of inventing one', () => {
    // A negative or non-finite figure is a defect upstream. Showing an em
    // dash says "no figure"; showing "-3 s" would say the person spoke for
    // less than no time.
    expect(formatDuration(-3)).toBe('—')
    expect(formatDuration(Number.NaN)).toBe('—')
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBe('—')
  })

  it('rounds a fractional second rather than printing it', () => {
    expect(formatDuration(59.6)).toBe('1 min')
    expect(formatDuration(0.4)).toBe('0 s')
  })

  it('counts hours past a day rather than starting a day column', () => {
    // 30 h is a figure somebody can compare to last month's. "1 d 6 h"
    // makes them do arithmetic before they can.
    expect(formatDuration(108000)).toBe('30 h')
  })
})

describe('a count', () => {
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
    // The same string on the server and in the browser. Formatting in the
    // viewer's zone would render one time during the server render and a
    // different one on hydration, which Vue reports as a mismatch and the
    // reader sees as a flicker.
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
  it('prefers the name people know it by', () => {
    expect(channelLabel(pointer({ channel_name: 'standup' }))).toBe('#standup')
  })

  it('falls back to the id, in full, when the name was never captured', () => {
    // A deleted channel has no name to fetch. The full id is the only
    // thing left that identifies it -- a shortened one names nothing and
    // cannot be searched for.
    expect(channelLabel(pointer({ channel_name: null }))).toBe('Channel 1408912345678901234')
  })

  it('keeps every digit of a snowflake, which a number would not', () => {
    const label = channelLabel(pointer({ channel_name: null }))
    expect(label).toContain('1408912345678901234')
    expect(label).not.toContain('1408912345678901000')
  })
})

describe('describing one session', () => {
  it('gives the channel, the moment and the length', () => {
    expect(describeSession(pointer())).toEqual({
      id: '900000000000000001',
      channel: '#standup',
      when: '14 Aug 2026, 09:07 UTC',
      duration: '2 h 14 min',
    })
  })

  it('says an unfinished session has no length rather than calling it empty', () => {
    // A session still running, or one whose end was never recorded, has no
    // duration. Zero would claim it was over the moment it began.
    expect(describeSession(pointer({ duration_seconds: null, ended_at: null }))?.duration).toBe('—')
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
    expect(speechNote(8040, 3)).toBe('3 tracks recorded before Sturnus measured speech are not counted.')
  })

  it('counts a single omitted track in the singular', () => {
    expect(speechNote(8040, 1)).toBe('1 track recorded before Sturnus measured speech is not counted.')
  })

  it('explains an absent total instead of leaving a bare dash', () => {
    expect(speechNote(null, 4)).toBe('None of your 4 tracks were measured; they predate the measurement.')
  })

  it('explains an absent total even when nothing was skipped', () => {
    expect(speechNote(null, 0)).toBe('Nothing has been measured yet.')
  })
})

describe('the figures the dashboard shows', () => {
  it('leads with how long this person has spoken', () => {
    const [first] = summaryFigures(summary())
    expect(first?.label).toBe('Time you have spoken')
    expect(first?.value).toBe('2 h 14 min')
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
    expect(figures[0]?.value).toBe('—')
    expect(figures[0]?.note).toBe('None of your 2 tracks were measured; they predate the measurement.')
  })

  it('says how many of the attended sessions produced a protocol', () => {
    const figures = summaryFigures(summary({ sessions_attended: 12, sessions_with_protocol: 9 }))
    const protocols = figures.find((f) => f.key === 'protocols')
    expect(protocols?.value).toBe('9')
    expect(protocols?.note).toBe('of 12 sessions attended')
  })

  it('counts a single attended session in the singular', () => {
    const figures = summaryFigures(summary({ sessions_attended: 1, sessions_with_protocol: 1 }))
    expect(figures.find((f) => f.key === 'protocols')?.note).toBe('of 1 session attended')
  })

  it('groups a large word count', () => {
    expect(summaryFigures(summary()).find((f) => f.key === 'words')?.value).toBe('48,213')
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
    expect(highlights.map((h) => h.label)).toEqual(['Most recent', 'Longest', 'First'])
    expect(highlights[1]?.session.duration).toBe('4 h')
  })

  it('omits a highlight the endpoint had no session for', () => {
    // Three empty cards labelled "Longest", "First" and "Most recent"
    // would suggest the data is loading rather than absent.
    const highlights = sessionHighlights(summary({ most_recent_session: pointer() }))
    expect(highlights.map((h) => h.label)).toEqual(['Most recent'])
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
    expect(describeFailure({ statusCode: 503 })).toBe('The API answered 503 and could not produce your figures.')
  })

  it('treats a refused session as a sign-in problem rather than a failure', () => {
    // Somebody whose cookie expired between the render and this call is
    // not looking at a broken console. Telling them to try again would
    // send them round a loop that cannot succeed.
    expect(describeFailure({ statusCode: 401 })).toBe('Your session is no longer valid.')
    expect(describeFailure({ statusCode: 403 })).toBe('Your session is no longer valid.')
  })

  it('says which side is behind when the endpoint is not there', () => {
    // The console and the API ship as two images and can be deployed
    // apart. "404" alone would send somebody looking for a bug in their
    // own account.
    expect(describeFailure({ statusCode: 404 })).toBe('This API has no dashboard yet; it is older than this console.')
  })

  it('says the API was unreachable when there is no status at all', () => {
    expect(describeFailure(new Error('fetch failed'))).toBe('The API could not be reached.')
    expect(describeFailure(null)).toBe('The API could not be reached.')
  })

  it('never repeats the internal address back to the browser', () => {
    // The server-side render calls the API through its in-cluster Service.
    // `$fetch` puts that URL in the message it throws, and echoing it into
    // the page would publish an internal hostname to anybody who loads a
    // failing dashboard.
    const leak = {
      statusCode: 500,
      message: '[GET] "http://sturnus-api:8080/api/dashboard": 500 Internal Server Error',
    }
    expect(describeFailure(leak)).not.toContain('sturnus-api')
    expect(describeFailure(leak)).not.toContain('http')
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
