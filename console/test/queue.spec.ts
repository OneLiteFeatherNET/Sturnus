/**
 * What a guild's transcription queue means, and what the page is allowed
 * to claim about it.
 *
 * All of it lives in `~/utils/queue` rather than in the page, because
 * every one of these is a decision -- which row comes first, whether a row
 * is stuck or merely waiting, whether an empty page is good news -- and a
 * decision embedded in a template can only be tested by rendering one.
 *
 * The wording is asserted here on purpose, and heavily. Three of the
 * figures this page shows are *derived*, and each one is derived from
 * something the API cannot actually see:
 *
 * - `running_past_lease` is measured against an assumed lease, not the
 *   worker's own `job_lease_seconds`.
 * - `oldest_pending_session_ended_at` is a session's end standing in for a
 *   job's enqueue time, which does not exist in the schema at all.
 * - the session list is cut, so its length is not the size of the backlog.
 *
 * A test that only checked the numbers would let any of those caveats
 * quietly drop out of the page, and the first person to notice would be an
 * administrator who restarted a healthy worker because a console told them
 * it was dead.
 */
import { describe, expect, it } from 'vitest'

import {
  CLEAR_QUEUE_NOTE,
  LIFECYCLE_SCOPE_NOTE,
  attentionItems,
  describeQueueError,
  isQueueClear,
  isQueueMoving,
  lifecycleFigures,
  needsPersonCount,
  oldestPendingLine,
  orderQueueSessions,
  parseGuildQueue,
  pastLeaseLine,
  queueAttention,
  queueChannelLabel,
  queueChannelNote,
  queuePath,
  queueSessionState,
  sessionCounts,
  sessionStartLine,
  sessionsSummaryLine,
  truncationNotice,
  undocumentedLine,
  type GuildQueue,
  type QueueCounts,
  type QueuedSession,
} from '../app/utils/queue'

/** A session with everything harmless, so each test states only the one
 *  property it is actually about. Closed, transcribed and written up: the
 *  state nothing on this page has anything to say about. */
function session(overrides: Partial<QueuedSession> & { id: string }): QueuedSession {
  return {
    channel_id: '555',
    channel_name: 'meeting',
    started_at: '2026-08-21T12:00:00+00:00',
    ended_at: '2026-08-21T13:00:00+00:00',
    status: 'documented',
    document_url: 'https://outline.example/doc',
    counts: { pending: 0, running: 0, done: 4, dead: 0 },
    ...overrides,
  }
}

function counts(overrides: Partial<QueueCounts> = {}): QueueCounts {
  return { pending: 0, running: 0, done: 0, dead: 0, ...overrides }
}

/** A guild with nothing outstanding, for the same reason. */
function queue(overrides: Partial<GuildQueue> = {}): GuildQueue {
  return {
    guild_id: '4711',
    counts: counts(),
    running_past_lease: 0,
    oldest_pending_session_ended_at: null,
    closed_undocumented: 0,
    lease_seconds: 1800,
    truncated: false,
    sessions: [],
    ...overrides,
  }
}

/** What `ApiError` looks like to the function that reads a failure. */
function failure(status: number) {
  return { status, path: '/guilds/4711/queue' }
}

/** A fixed clock, so an age in a sentence is a fact about the fixture and
 *  not about when the suite happened to run. */
const NOW = Date.parse('2026-08-21T16:20:00+00:00')

describe('reading the queue payload', () => {
  it('reads the whole envelope the endpoint sends', () => {
    const parsed = parseGuildQueue({
      guild_id: '4711',
      counts: { pending: 2, running: 1, done: 40, dead: 1 },
      running_past_lease: 0,
      oldest_pending_session_ended_at: '2026-08-21T13:00:00+00:00',
      closed_undocumented: 0,
      lease_seconds: 1800.0,
      truncated: false,
      sessions: [
        {
          id: '4711',
          channel_id: '555',
          channel_name: 'meeting',
          started_at: '2026-08-21T12:00:00+00:00',
          ended_at: '2026-08-21T13:00:00+00:00',
          status: 'closed',
          document_url: null,
          counts: { pending: 2, running: 1, done: 0, dead: 0 },
        },
      ],
    })
    expect(parsed.guild_id).toBe('4711')
    expect(parsed.counts).toEqual({ pending: 2, running: 1, done: 40, dead: 1 })
    expect(parsed.lease_seconds).toBe(1800)
    expect(parsed.sessions).toHaveLength(1)
    expect(parsed.sessions[0]!.counts.pending).toBe(2)
    expect(parsed.sessions[0]!.document_url).toBeNull()
  })

  it('keeps every id a string even if it arrived as a number', () => {
    // A channel snowflake past the safe integer range has already lost its
    // last digits before this function sees it. Stringifying does not undo
    // that; it keeps the recording link well-formed so the damage surfaces
    // as a 404 rather than as somebody opening a different meeting.
    const parsed = parseGuildQueue({ sessions: [{ id: 12, channel_id: 555 }] })
    expect(parsed.sessions[0]!.id).toBe('12')
    expect(parsed.sessions[0]!.channel_id).toBe('555')
  })

  it('drops a session with no id at all', () => {
    // The link to its recording is the only thing a row offers, and a row
    // that cannot be opened is a row that can only be misread.
    expect(parseGuildQueue({ sessions: [{ channel_id: '555' }, { id: '1' }] }).sessions).toHaveLength(1)
  })

  it('treats a missing name as no name rather than as an empty one', () => {
    expect(parseGuildQueue({ sessions: [{ id: '1' }] }).sessions[0]!.channel_name).toBeNull()
    expect(
      parseGuildQueue({ sessions: [{ id: '1', channel_name: '   ' }] }).sessions[0]!.channel_name,
    ).toBeNull()
  })

  it('never reports a negative or nonsensical count', () => {
    // A defect upstream must not render as "-3 pending" beside a server's
    // name, where it reads as a fact about that server.
    const parsed = parseGuildQueue({
      counts: { pending: -3, running: 'many', done: 2.4, dead: null },
      running_past_lease: -1,
      closed_undocumented: 'lots',
    })
    expect(parsed.counts).toEqual({ pending: 0, running: 0, done: 2, dead: 0 })
    expect(parsed.running_past_lease).toBe(0)
    expect(parsed.closed_undocumented).toBe(0)
  })

  it('treats a missing truncation flag as a whole list', () => {
    // Erring the other way would put a warning about a hidden backlog on
    // every complete page, and a warning that is always there is one
    // nobody reads on the day it is true.
    expect(parseGuildQueue({}).truncated).toBe(false)
    expect(parseGuildQueue({ truncated: 'true' }).truncated).toBe(false)
    expect(parseGuildQueue({ truncated: true }).truncated).toBe(true)
  })

  it('yields a well-formed queue for a payload it cannot make sense of', () => {
    // Never null: a parser that gave up would turn a strange payload into
    // a blank page with no error anywhere, which is the failure mode
    // hardest to report.
    const parsed = parseGuildQueue('nonsense')
    expect(parsed.guild_id).toBeNull()
    expect(parsed.sessions).toEqual([])
    expect(parsed.counts).toEqual({ pending: 0, running: 0, done: 0, dead: 0 })
  })

  it('escapes the guild id in the path it builds', () => {
    // A string from an API allowed to contain a slash is a string allowed
    // to address a different endpoint.
    expect(queuePath('4711')).toBe('/guilds/4711/queue')
    expect(queuePath('../guilds/1')).toBe('/guilds/..%2Fguilds%2F1/queue')
  })
})

describe('naming a session', () => {
  it('names the channel it happened in', () => {
    expect(queueChannelLabel(session({ id: '1', channel_name: 'meeting' }))).toBe('#meeting')
  })

  it('falls back to the whole channel id, never a shortened one', () => {
    // Snowflakes minted in the same era share their leading digits, so a
    // truncated id names a group rather than a channel.
    expect(
      queueChannelLabel(session({ id: '1', channel_name: null, channel_id: '1129384756123456789' })),
    ).toBe('Channel 1129384756123456789')
  })

  it('says why a row has an id instead of a name', () => {
    // A bare snowflake where every other row carries a #name reads as a
    // fault in the console. It is not one.
    const note = queueChannelNote(session({ id: '1', channel_name: null }))
    expect(note).toContain('deleted since the meeting')
    expect(note).toContain('the recording itself is unaffected')
  })

  it('says nothing extra about a row that has a name', () => {
    expect(queueChannelNote(session({ id: '1', channel_name: 'meeting' }))).toBeNull()
  })

  it('writes the start in UTC and says which zone that is', () => {
    // The server render cannot know the reader's zone, so a second
    // rendering in the browser would disagree with the first.
    expect(sessionStartLine(session({ id: '1' }))).toBe('Started 21 Aug 2026, 12:00 UTC.')
  })

  it('says outright when there is no start time rather than printing a dash', () => {
    expect(sessionStartLine(session({ id: '1', started_at: '' }))).toContain('was not recorded')
  })

  it('lists a row’s four counts in lifecycle order', () => {
    const rendered = sessionCounts(
      session({ id: '1', counts: counts({ pending: 2, running: 1, done: 3, dead: 4 }) }),
    )
    expect(rendered.map((c) => c.label)).toEqual(['Pending', 'Running', 'Done', 'Dead'])
    expect(rendered.map((c) => c.value)).toEqual(['2', '1', '3', '4'])
  })
})

describe('what state a session is in', () => {
  it('calls a session with a permanently failed speaker out first', () => {
    const state = queueSessionState(
      session({ id: '1', counts: counts({ done: 3, dead: 1 }), document_url: null }),
    )
    expect(state.tone).toBe('alarm')
    expect(state.label).toBe('1 speaker failed for good')
    expect(state.detail).toContain('will not be retried on their own')
    expect(state.detail).toContain('re-queueing it there')
  })

  it('says a documented session can still be missing a voice', () => {
    // A session reaches `documented` with a dead job in it, looks finished
    // from every other angle, and this page is the only place anybody
    // finds out. That is the whole reason such a session is listed at all.
    const state = queueSessionState(
      session({
        id: '1',
        status: 'documented',
        document_url: 'https://outline.example/doc',
        counts: counts({ done: 3, dead: 1 }),
      }),
    )
    expect(state.tone).toBe('alarm')
    expect(state.detail).toContain('A protocol was written anyway, without them')
    expect(state.detail).toContain('finds a voice missing')
  })

  it('counts the failed speakers rather than saying "some"', () => {
    const state = queueSessionState(session({ id: '1', counts: counts({ dead: 3 }) }))
    expect(state.label).toBe('3 speakers failed for good')
  })

  it('reads a running job as work in hand, not as a problem', () => {
    const state = queueSessionState(
      session({ id: '1', counts: counts({ pending: 2, running: 1 }), document_url: null }),
    )
    expect(state.tone).toBe('watch')
    expect(state.label).toBe('Being transcribed')
    expect(state.detail).toContain('2 jobs behind it are still waiting')
    expect(state.detail).toContain('unless the figures stop changing')
  })

  it('distinguishes queued-and-nobody-working from queued-and-being-worked', () => {
    // Pending with nothing running is the shape of "no worker is taking
    // work at all", which is a different thing to look at than a busy one.
    const state = queueSessionState(
      session({ id: '1', counts: counts({ pending: 2 }), document_url: null }),
    )
    expect(state.label).toBe('Waiting for a worker')
    expect(state.detail).toContain('none running')
    expect(state.detail).toContain('no worker is taking work at all')
  })

  it('reads an open session with no jobs as a recording happening right now', () => {
    // The one row of zeros on this page that is not an absence of work but
    // the absence of a reason for work. Presenting it as "nothing to do"
    // would hide a live meeting.
    const state = queueSessionState(
      session({ id: '1', status: 'open', ended_at: null, document_url: null, counts: counts() }),
    )
    expect(state.tone).toBe('clear')
    expect(state.label).toBe('Recording now')
    expect(state.detail).toContain('being recorded at this moment')
    expect(state.detail).toContain('created when the recording ends')
  })

  it('trusts the missing end time over an unrecognised status string', () => {
    // A status this console does not know must not turn a running meeting
    // into an unexplained row of zeros.
    expect(
      queueAttention(
        session({ id: '1', status: 'something-new', ended_at: null, document_url: null, counts: counts() }),
      ),
    ).toBe('recording')
  })

  it('says nothing will happen on its own for a closed session with no protocol', () => {
    const state = queueSessionState(
      session({ id: '1', status: 'closed', document_url: null, counts: counts({ done: 4 }) }),
    )
    expect(state.tone).toBe('alarm')
    expect(state.label).toBe('Closed with nothing queued')
    expect(state.detail).toContain('nothing about it will change on its own')
    expect(state.detail).toContain('re-queue it from the recording')
  })

  it('separates a session that never had a job from one whose jobs all finished', () => {
    // "Nobody consented" and "everything transcribed but nothing was
    // written up" both look like a closed session with no protocol, and
    // only one of them can be fixed by asking for the work again.
    const state = queueSessionState(
      session({ id: '1', status: 'closed', document_url: null, counts: counts() }),
    )
    expect(state.label).toBe('Nothing was ever queued')
    expect(state.detail).toContain('nobody in the channel had consented')
    expect(state.detail).toContain('no protocol will appear on its own')
  })

  it('treats a written-up session waiting on its own flag as nothing to do', () => {
    const state = queueSessionState(
      session({ id: '1', status: 'closed', counts: counts({ done: 4 }) }),
    )
    expect(state.tone).toBe('clear')
    expect(state.label).toBe('Waiting to be marked done')
    expect(state.detail).toContain('there is nothing to do here')
  })

  it('never leaves a row without a state', () => {
    // Every combination the API can send has to land somewhere, because a
    // row with an empty badge is a row whose meaning the reader invents.
    for (const ended of [null, '2026-08-21T13:00:00+00:00']) {
      for (const document of [null, 'https://outline.example/doc']) {
        for (const c of [counts(), counts({ pending: 1 }), counts({ running: 1 }), counts({ dead: 1 })]) {
          const state = queueSessionState(session({ id: '1', ended_at: ended, document_url: document, counts: c }))
          expect(state.label.trim()).not.toBe('')
          expect(state.detail.trim()).not.toBe('')
        }
      }
    }
  })
})

describe('the order the sessions are listed in', () => {
  it('puts the rows nothing will move without a person first', () => {
    const ordered = orderQueueSessions([
      session({ id: '1', counts: counts({ pending: 1 }), document_url: null }),
      session({ id: '2', counts: counts({ dead: 1 }) }),
    ])
    expect(ordered.map((s) => s.id)).toEqual(['2', '1'])
  })

  it('ranks stuck, then moving, then recording, then merely unflagged', () => {
    const ordered = orderQueueSessions([
      session({ id: 'flagging', status: 'closed', counts: counts({ done: 2 }) }),
      session({ id: 'live', status: 'open', ended_at: null, document_url: null, counts: counts() }),
      session({ id: 'moving', document_url: null, counts: counts({ running: 1 }) }),
      session({ id: 'stuck', document_url: null, counts: counts({ dead: 1 }) }),
    ])
    expect(ordered.map((s) => s.id)).toEqual(['stuck', 'moving', 'live', 'flagging'])
  })

  it('lists the newest first inside a rank, in every rank alike', () => {
    // Deliberately not oldest-first for the stuck rows. Somebody arrives
    // here because a team has just said "this morning's meeting has no
    // protocol", and they scan for the meeting they were told about; a
    // list that counts backwards in one rank and forwards in another
    // cannot be scanned at all. How long the backlog has been there is
    // what the oldest-pending figure answers instead.
    const ordered = orderQueueSessions([
      session({ id: 'old', started_at: '2026-08-01T09:00:00+00:00', counts: counts({ dead: 1 }) }),
      session({ id: 'new', started_at: '2026-08-21T09:00:00+00:00', counts: counts({ dead: 1 }) }),
    ])
    expect(ordered.map((s) => s.id)).toEqual(['new', 'old'])
  })

  it('sinks a session with an unreadable start to the end of its rank', () => {
    // Still a row worth seeing, and with no claim to a position among the
    // rows that carry a real time.
    const ordered = orderQueueSessions([
      session({ id: 'broken', started_at: 'not a date', counts: counts({ dead: 1 }) }),
      session({ id: 'old', started_at: '2026-08-01T09:00:00+00:00', counts: counts({ dead: 1 }) }),
    ])
    expect(ordered.map((s) => s.id)).toEqual(['old', 'broken'])
  })

  it('never leaves two sessions recorded in parallel in an arbitrary order', () => {
    // Every comparison ends at the id, which is unique, so the order is
    // total and the rows do not swap places between renders.
    const twice = () =>
      orderQueueSessions([
        session({ id: '9', counts: counts({ dead: 1 }) }),
        session({ id: '10', counts: counts({ dead: 1 }) }),
      ]).map((s) => s.id)
    expect(twice()).toEqual(['10', '9'])
    expect(twice()).toEqual(['10', '9'])
  })

  it('orders tied ids numerically rather than as strings', () => {
    // As plain strings "1000" sorts before "999", which would put an older
    // session above a newer one at the exact moment the tiebreak is all
    // there is to go on.
    const ordered = orderQueueSessions([
      session({ id: '999', counts: counts({ dead: 1 }) }),
      session({ id: '1000', counts: counts({ dead: 1 }) }),
    ])
    expect(ordered.map((s) => s.id)).toEqual(['1000', '999'])
  })

  it('leaves the list it was given alone', () => {
    const given = [session({ id: '1' }), session({ id: '2', counts: counts({ dead: 1 }) })]
    orderQueueSessions(given)
    expect(given.map((s) => s.id)).toEqual(['1', '2'])
  })
})

describe('how much of the list is somebody’s problem', () => {
  it('counts only the rows nothing queued will move on', () => {
    expect(
      needsPersonCount([
        session({ id: '1', counts: counts({ dead: 1 }) }),
        session({ id: '2', document_url: null, status: 'closed', counts: counts({ done: 1 }) }),
        session({ id: '3', document_url: null, counts: counts({ running: 1 }) }),
        session({ id: '4', status: 'open', ended_at: null, document_url: null, counts: counts() }),
      ]),
    ).toBe(2)
  })

  it('says how many rows need a person and that they are listed first', () => {
    const line = sessionsSummaryLine([
      session({ id: '1', counts: counts({ dead: 1 }) }),
      session({ id: '2', document_url: null, counts: counts({ running: 1 }) }),
    ])
    expect(line).toContain('2 unfinished sessions here')
    expect(line).toContain('1 of them needs somebody')
    expect(line).toContain('Those are listed first.')
  })

  it('says outright when none of them is waiting on a person', () => {
    const line = sessionsSummaryLine([session({ id: '1', document_url: null, counts: counts({ running: 1 }) })])
    expect(line).toContain('1 unfinished session here')
    expect(line).toContain('none of them is waiting on a person')
  })

  it('has something to say about an empty list', () => {
    expect(sessionsSummaryLine([])).toBe('No unfinished sessions are listed for this server.')
  })
})

describe('the four lifecycle counts', () => {
  it('renders them in the order a job moves through them', () => {
    const figures = lifecycleFigures(queue({ counts: counts({ pending: 2, running: 1, done: 40, dead: 1 }) }))
    expect(figures.map((f) => f.label)).toEqual(['Pending', 'Running', 'Done', 'Dead'])
    expect(figures.map((f) => f.value)).toEqual(['2', '1', '40', '1'])
  })

  it('says that they are guild-wide and not a sum of the list below', () => {
    // A reader who adds up the rows and gets a different number has found
    // the difference between the two, not a fault, and the page has to be
    // the thing that tells them so.
    expect(LIFECYCLE_SCOPE_NOTE).toContain('across all time')
    expect(LIFECYCLE_SCOPE_NOTE).toContain('not a sum of the sessions listed')
    expect(LIFECYCLE_SCOPE_NOTE).toContain('pending, then running, then done')
  })

  it('marks a dead count that is not zero, and only that one', () => {
    const withDead = lifecycleFigures(queue({ counts: counts({ dead: 1, done: 9 }) }))
    expect(withDead.map((f) => f.tone)).toEqual(['clear', 'clear', 'clear', 'alarm'])
    const withoutDead = lifecycleFigures(queue({ counts: counts({ done: 9 }) }))
    expect(withoutDead.every((f) => f.tone === 'clear')).toBe(true)
  })

  it('says what a zero in the dead column means rather than leaving it bare', () => {
    const figures = lifecycleFigures(queue())
    expect(figures[3]!.note).toBe('Nothing in this server has failed for good.')
  })

  it('explains what a dead job costs somebody', () => {
    const figures = lifecycleFigures(queue({ counts: counts({ dead: 2 }) }))
    expect(figures[3]!.note).toContain('not retried on their own')
    expect(figures[3]!.note).toContain('a speaker missing from a protocol')
  })

  it('gives every stage a note, so no figure is left to be guessed at', () => {
    for (const figure of lifecycleFigures(queue())) {
      expect(figure.note.trim()).not.toBe('')
    }
  })
})

describe('the jobs running past their lease', () => {
  it('names the lease the count was measured against', () => {
    // The whole point. The lease that actually applies is the worker's own
    // job_lease_seconds, which the API process cannot see.
    const line = pastLeaseLine(queue({ running_past_lease: 2, lease_seconds: 1800 }))
    expect(line).toContain('2 running jobs have been held longer than the 1800-second lease')
    expect(line).toContain("the worker's own job_lease_seconds, which the API process cannot see")
    expect(line).toContain('measured against the lease it assumed rather than the real one')
  })

  it('says what it means if the worker’s lease is not higher', () => {
    const line = pastLeaseLine(queue({ running_past_lease: 1 }))
    expect(line).toContain('One running job has been held')
    expect(line).toContain('the worker holding these died')
    expect(line).toContain('no amount of waiting fixes that')
  })

  it('keeps the caveat when the count is zero', () => {
    // A zero reported without it would read as "no worker has died", which
    // this figure cannot establish: a raised lease hides an overdue job.
    const line = pastLeaseLine(queue({ running_past_lease: 0 }))
    expect(line).toContain('No running job has been held longer than the 1800-second lease')
    expect(line).toContain("the worker's own job_lease_seconds, which the API process cannot see")
    expect(line).toContain('reassuring rather than conclusive')
  })

  it('refuses to name a lease it was not given', () => {
    // "past the 0-second lease" would read as though every running job
    // were overdue, which is the opposite of what the figure says.
    const line = pastLeaseLine(queue({ running_past_lease: 1, lease_seconds: 0 }))
    expect(line).toContain('longer than the lease the API assumed')
    expect(line).not.toContain('0-second')
  })

  it('rounds a fractional lease rather than printing it', () => {
    expect(pastLeaseLine(queue({ running_past_lease: 1, lease_seconds: 1800.0 }))).toContain(
      '1800-second lease',
    )
  })
})

describe('the closed sessions with no protocol', () => {
  it('says that nothing is queued and nothing will start', () => {
    const line = undocumentedLine(queue({ closed_undocumented: 3 }))
    expect(line).toContain('3 closed sessions have no unfinished jobs left and still no protocol')
    expect(line).toContain('nothing will start on its own')
    expect(line).toContain('waits for a person')
  })

  it('reads a single one as one', () => {
    expect(undocumentedLine(queue({ closed_undocumented: 1 }))).toContain('One closed session has')
  })

  it('reads the zero as the good news it is', () => {
    const line = undocumentedLine(queue())
    expect(line).toContain('Nothing is sitting finished and unwritten.')
  })
})

describe('the oldest job still waiting', () => {
  it('says the figure is dated by the session’s end, not by the job', () => {
    // `transcription_job` records no enqueue time at all. Calling this a
    // job age would be inventing a column.
    const line = oldestPendingLine(
      queue({ oldest_pending_session_ended_at: '2026-08-21T13:00:00+00:00' }),
      NOW,
    )
    expect(line).toContain('a session that ended 21 Aug 2026, 13:00 UTC')
    expect(line).toContain("dated by the session's end rather than by the job")
    expect(line).toContain('transcription_job records no enqueue time at all')
  })

  it('says that a re-queued job reads older than it is', () => {
    const line = oldestPendingLine(
      queue({ oldest_pending_session_ended_at: '2026-08-21T13:00:00+00:00' }),
      NOW,
    )
    expect(line).toContain("keeps its session's original end")
    expect(line).toContain('reads older than the job itself')
  })

  it('turns the instant into an age once there is a clock to compare it to', () => {
    expect(
      oldestPendingLine(queue({ oldest_pending_session_ended_at: '2026-08-21T13:00:00+00:00' }), NOW),
    ).toContain('— 3 h 20 min ago')
  })

  it('omits the age entirely when there is no clock yet', () => {
    // The server render has no reader's clock, and a paragraph whose text
    // differs between the two renders is a hydration mismatch. The moment
    // is shown in both; the age arrives after mounting.
    const line = oldestPendingLine(
      queue({ oldest_pending_session_ended_at: '2026-08-21T13:00:00+00:00' }),
      null,
    )
    expect(line).toContain('a session that ended 21 Aug 2026, 13:00 UTC.')
    expect(line).not.toContain('ago')
  })

  it('omits an age that would be negative rather than printing one', () => {
    // A clock skew between the API host and the reader's machine is not
    // something to render as "-2 min ago".
    expect(
      oldestPendingLine(
        queue({ oldest_pending_session_ended_at: '2026-08-21T17:00:00+00:00' }),
        NOW,
      ),
    ).not.toContain('ago')
  })

  it('says nothing is waiting rather than showing a dash', () => {
    expect(oldestPendingLine(queue(), NOW)).toBe(
      'Nothing is waiting: this server has no job in pending at all.',
    )
  })
})

describe('the three figures somebody has to act on', () => {
  it('shows all three whether or not they are zero', () => {
    // A row that appears only when it is bad news is a row whose absence
    // has to be interpreted, and "there is no warning" and "this page does
    // not warn" look identical on screen.
    expect(attentionItems(queue(), NOW).map((i) => i.key)).toEqual([
      'past-lease',
      'closed-undocumented',
      'oldest-pending',
    ])
  })

  it('puts the number worth reading first at the front', () => {
    // No amount of waiting fixes a job whose worker died holding it.
    expect(attentionItems(queue(), NOW)[0]!.label).toBe('Running past their lease')
  })

  it('raises the tone only on the figures that are not zero', () => {
    const calm = attentionItems(queue(), NOW)
    expect(calm.map((i) => i.tone)).toEqual(['clear', 'clear', 'clear'])
    const loud = attentionItems(
      queue({
        running_past_lease: 1,
        closed_undocumented: 2,
        oldest_pending_session_ended_at: '2026-08-21T13:00:00+00:00',
      }),
      NOW,
    )
    expect(loud.map((i) => i.tone)).toEqual(['alarm', 'alarm', 'watch'])
  })

  it('gives the oldest-pending figure a value that says nothing is waiting', () => {
    expect(attentionItems(queue(), NOW)[2]!.value).toBe('Nothing waiting')
  })

  it('carries the caveat into the detail of every figure', () => {
    for (const item of attentionItems(queue({ running_past_lease: 1 }), NOW)) {
      expect(item.detail.trim()).not.toBe('')
      expect(item.label.trim()).not.toBe('')
      expect(item.value.trim()).not.toBe('')
    }
  })
})

describe('what the list does not show', () => {
  it('says the list was cut and that its length is not the backlog', () => {
    // Otherwise a page showing twenty sessions reads as "there are
    // twenty", which is the one question a backlog page is opened with.
    const notice = truncationNotice(
      queue({ truncated: true, sessions: Array.from({ length: 20 }, (_, i) => session({ id: String(i) })) }),
    )
    expect(notice).toContain('Only the newest 20 unfinished sessions are listed; this server has more.')
    expect(notice).toContain('a window on the backlog and not its size')
    expect(notice).toContain('The four job counts above are guild-wide and do count all of it.')
  })

  it('reads the number of rows off the list rather than naming the server’s limit', () => {
    // The limit is the server's to change, and a sentence naming a number
    // the API no longer uses is a sentence that lies without anybody
    // editing it.
    expect(truncationNotice(queue({ truncated: true, sessions: [session({ id: '1' })] }))).toContain(
      'Only one unfinished session is listed; this server has more.',
    )
  })

  it('says nothing at all about a list that is whole', () => {
    expect(truncationNotice(queue({ sessions: [session({ id: '1' })] }))).toBeNull()
  })
})

describe('whether anything is happening at all', () => {
  it('polls while a job is pending or running', () => {
    expect(isQueueMoving(queue({ counts: counts({ pending: 1 }) }))).toBe(true)
    expect(isQueueMoving(queue({ counts: counts({ running: 1 }) }))).toBe(true)
  })

  it('stops polling for a dead job, which will never change on its own', () => {
    // A page that reloaded for ever waiting for news that cannot arrive is
    // a load generator, not a status page.
    expect(isQueueMoving(queue({ counts: counts({ dead: 3 }) }))).toBe(false)
    expect(isQueueMoving(queue({ counts: counts({ done: 40 }) }))).toBe(false)
  })

  it('reads the guild-wide counts rather than the listed rows', () => {
    // The list is cut and the counts are not: a guild with jobs queued is
    // still moving even if every row that fitted happens to be finished.
    expect(
      isQueueMoving(queue({ counts: counts({ pending: 5 }), truncated: true, sessions: [session({ id: '1' })] })),
    ).toBe(true)
  })

  it('calls a server clear only when all six ways of being unwell are absent', () => {
    expect(isQueueClear(queue({ counts: counts({ done: 40 }) }))).toBe(true)
    expect(isQueueClear(queue({ sessions: [session({ id: '1' })] }))).toBe(false)
    expect(isQueueClear(queue({ counts: counts({ pending: 1 }) }))).toBe(false)
    expect(isQueueClear(queue({ running_past_lease: 1 }))).toBe(false)
    expect(isQueueClear(queue({ closed_undocumented: 1 }))).toBe(false)
    expect(isQueueClear(queue({ truncated: true }))).toBe(false)
    expect(isQueueClear(queue({ oldest_pending_session_ended_at: '2026-08-21T13:00:00+00:00' }))).toBe(false)
  })

  it('does not treat a server that once had a failure as permanently unwell', () => {
    // `done` and `dead` describe what has happened, not what is
    // outstanding, and a page that never went green again would stop being
    // read.
    expect(isQueueClear(queue({ counts: counts({ done: 40, dead: 2 }) }))).toBe(true)
  })

  it('writes the empty state as the good news it is', () => {
    // A queue page with nothing on it is the state everybody wants and the
    // state that looks most like a broken page.
    expect(CLEAR_QUEUE_NOTE).toContain('no worker is holding one past its lease')
    expect(CLEAR_QUEUE_NOTE).toContain('There is nothing to do here')
  })
})

describe('when the API says no', () => {
  it('sends somebody back to sign in on a 401', () => {
    expect(describeQueueError(failure(401))).toContain('Sign in again')
  })

  it('names where administrator status comes from on a 403', () => {
    expect(describeQueueError(failure(403))).toContain('admin_role_id')
  })

  it('covers both meanings of a 404 without guessing which', () => {
    // The API answers 404 for a guild that does not exist and for one the
    // caller does not administer alike, on purpose: it will not confirm
    // the existence of a server to somebody with no business there.
    const message = describeQueueError(failure(404))
    expect(message).toContain('does not know this server, or you no longer administer it')
    expect(message).toContain('answers the same way to both')
  })

  it('distinguishes a refusal from never reaching the API at all', () => {
    // `ApiError` uses 0 for "never got a response", which must not print
    // as "Sturnus answered 0".
    expect(describeQueueError(failure(0))).toContain('Could not reach the API')
    expect(describeQueueError(null)).toContain('Could not reach the API')
  })

  it('names an unexpected status rather than inventing a reason for it', () => {
    expect(describeQueueError(failure(503))).toContain('Sturnus answered 503')
  })

  it('never echoes anything the failure carried with it', () => {
    // `useApi` strips the body off every failed request on purpose, so an
    // in-cluster hostname can never reach the hydration payload. Nothing
    // here may reintroduce one from a message field either.
    const leaky = { status: 500, message: 'http://sturnus-api:8080/api/guilds/4711/queue failed' }
    expect(describeQueueError(leaky)).not.toContain('sturnus-api')
  })
})
