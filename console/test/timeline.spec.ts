/**
 * Where a day's sessions sit on a 24-hour axis.
 *
 * A list sorted by time is not a timeline: it says a meeting happened
 * before another one, never that one was at nine in the morning and the
 * other at nine at night. Turning an instant into a position is the whole
 * point of the view, so it is a tested function rather than an inline
 * expression in a `:style` binding.
 */
import { describe, expect, it } from 'vitest'

import {
  MIN_BAR_EXTENT,
  axisTicks,
  dayWindowStart,
  layOutDay,
  summarise,
  type DaySession,
} from '../app/utils/timeline'

/** A session the API would have returned, with sensible defaults. */
function session(startedAt: string, over: Partial<DaySession> = {}): DaySession {
  return {
    id: `s-${startedAt}`,
    started_at: startedAt,
    duration_seconds: 3600,
    channel_id: '900000000000000001',
    channel_name: 'Standup',
    ...over,
  }
}

describe('the window a day occupies', () => {
  it('begins at the UTC midnight the API grouped the day by', () => {
    // The API buckets by UTC day, so the window has to be the UTC day too.
    // Anchoring it to local midnight would drop the sessions the API put in
    // this bucket that fall outside the viewer's own calendar day.
    expect(dayWindowStart('2026-08-21').toISOString()).toBe('2026-08-21T00:00:00.000Z')
  })
})

describe('placing a session on the axis', () => {
  it('puts a morning meeting and an evening one at opposite ends', () => {
    const bars = layOutDay('2026-08-21', [
      session('2026-08-21T09:00:00Z', { id: 'morning' }),
      session('2026-08-21T21:00:00Z', { id: 'evening' }),
    ])
    expect(bars.find((b) => b.id === 'morning')!.offset).toBeCloseTo(0.375, 6)
    expect(bars.find((b) => b.id === 'evening')!.offset).toBeCloseTo(0.875, 6)
  })

  it('places a lone session by when it was, not at the left edge', () => {
    // One session is the case where a "timeline" most easily degenerates
    // into a single bar that says nothing about the clock.
    const [bar] = layOutDay('2026-08-21', [session('2026-08-21T18:30:00Z')])
    expect(bar!.offset).toBeCloseTo(0.7708333, 6)
  })

  it('sizes a bar by how long the meeting ran', () => {
    const [bar] = layOutDay('2026-08-21', [
      session('2026-08-21T09:00:00Z', { duration_seconds: 5400 }),
    ])
    expect(bar!.extent).toBeCloseTo(0.0625, 6)
  })

  it('keeps a two-minute meeting wide enough to see', () => {
    // Two minutes is 0.14% of a day. Drawn to scale it is a hairline that
    // cannot be hovered, focused or noticed.
    const [bar] = layOutDay('2026-08-21', [
      session('2026-08-21T09:00:00Z', { duration_seconds: 120 }),
    ])
    expect(bar!.extent).toBe(0.005)
    expect(MIN_BAR_EXTENT).toBe(0.005)
  })

  it('stops a bar at midnight when the meeting ran past it', () => {
    // The overflow belongs to the next UTC day, which has its own cell and
    // its own timeline. A bar that ran off the end would suggest this day
    // held more than it did.
    const [bar] = layOutDay('2026-08-21', [
      session('2026-08-21T23:00:00Z', { duration_seconds: 7200 }),
    ])
    expect(bar!.offset + bar!.extent).toBeCloseTo(1, 6)
  })

  it('shows a session whose length was never recorded as a mark, not a zero', () => {
    // `duration_seconds` is nullable -- a session still running, or one
    // whose worker died before writing a length. It still happened, and at
    // a knowable time.
    const [bar] = layOutDay('2026-08-21', [
      session('2026-08-21T09:00:00Z', { duration_seconds: null }),
    ])
    expect(bar!.durationSeconds).toBeNull()
    expect(bar!.extent).toBe(0.005)
  })

  it('orders the bars by when they started', () => {
    const bars = layOutDay('2026-08-21', [
      session('2026-08-21T21:00:00Z', { id: 'late' }),
      session('2026-08-21T09:00:00Z', { id: 'early' }),
    ])
    expect(bars.map((b) => b.id)).toEqual(['early', 'late'])
  })
})

describe('sessions that overlap', () => {
  it('gives two channels recorded at once a lane each', () => {
    // Sturnus records per channel, so two meetings genuinely do run at the
    // same moment. Stacked in one lane the later one would paint over the
    // earlier and a viewer would count one meeting where there were two.
    const bars = layOutDay('2026-08-21', [
      session('2026-08-21T09:00:00Z', { id: 'a' }),
      session('2026-08-21T09:30:00Z', { id: 'b' }),
    ])
    expect(bars.map((b) => b.lane)).toEqual([0, 1])
  })

  it('reuses a lane once it is free again', () => {
    const bars = layOutDay('2026-08-21', [
      session('2026-08-21T09:00:00Z', { id: 'a', duration_seconds: 1800 }),
      session('2026-08-21T09:30:00Z', { id: 'b', duration_seconds: 1800 }),
    ])
    expect(bars.map((b) => b.lane)).toEqual([0, 0])
  })

  it('does not let an unfinished session block the lane for the rest of the day', () => {
    // A missing length is unknown, not infinite. Reserving the remainder of
    // the day for it would push every later meeting into a lane of its own.
    const bars = layOutDay('2026-08-21', [
      session('2026-08-21T09:00:00Z', { id: 'a', duration_seconds: null }),
      session('2026-08-21T15:00:00Z', { id: 'b' }),
    ])
    expect(bars.map((b) => b.lane)).toEqual([0, 0])
  })
})

describe('labelling a bar', () => {
  it('uses the channel name when the API knew it', () => {
    const [bar] = layOutDay('2026-08-21', [session('2026-08-21T09:00:00Z')])
    expect(bar!.channel).toBe('Standup')
  })

  it('falls back to the channel id when the name is gone', () => {
    // A channel deleted after the recording has no name left to give.
    const [bar] = layOutDay('2026-08-21', [
      session('2026-08-21T09:00:00Z', { channel_name: null, channel_id: '900000000000000001' }),
    ])
    expect(bar!.channel).toBe('Channel 900000000000000001')
  })

  it('carries a snowflake through as the string it arrived as', () => {
    // A Discord snowflake exceeds JavaScript's safe integer range. Anything
    // that turned this into a number would produce an id that looks right
    // and names nobody.
    const [bar] = layOutDay('2026-08-21', [
      session('2026-08-21T09:00:00Z', {
        id: '1408284735632900103',
        channel_name: null,
        channel_id: '1408284735632900104',
      }),
    ])
    expect(bar!.id).toBe('1408284735632900103')
    expect(bar!.channel).toBe('Channel 1408284735632900104')
  })
})

describe('the hour marks along the axis', () => {
  it('marks both midnights so the axis has a beginning and an end', () => {
    const ticks = axisTicks('2026-08-21', 3)
    expect(ticks[0]!.offset).toBe(0)
    expect(ticks[0]!.at.toISOString()).toBe('2026-08-21T00:00:00.000Z')
    expect(ticks[ticks.length - 1]!.offset).toBe(1)
    expect(ticks[ticks.length - 1]!.at.toISOString()).toBe('2026-08-22T00:00:00.000Z')
  })

  it('spaces the marks evenly at the requested interval', () => {
    const ticks = axisTicks('2026-08-21', 6)
    expect(ticks.map((t) => t.offset)).toEqual([0, 0.25, 0.5, 0.75, 1])
  })

  it('marks each tick with the instant it stands for, so a label can be local', () => {
    // The label is formatted in the viewer's zone from this instant rather
    // than from an hour number, which is what makes the axis survive a day
    // with a daylight-saving change in it.
    const ticks = axisTicks('2026-03-29', 12)
    expect(ticks.map((t) => t.at.toISOString())).toEqual([
      '2026-03-29T00:00:00.000Z',
      '2026-03-29T12:00:00.000Z',
      '2026-03-30T00:00:00.000Z',
    ])
  })
})

describe('summarising a day', () => {
  it('adds up what was recorded', () => {
    const summary = summarise([
      session('2026-08-21T09:00:00Z', { duration_seconds: 3600 }),
      session('2026-08-21T15:00:00Z', { duration_seconds: 1800 }),
    ])
    expect(summary.sessions).toBe(2)
    expect(summary.totalDurationSeconds).toBe(5400)
  })

  it('counts the sessions whose length is missing instead of treating them as nothing', () => {
    // Summing a null as zero would report a shorter day than happened, with
    // nothing on screen to say the total is incomplete.
    const summary = summarise([
      session('2026-08-21T09:00:00Z', { duration_seconds: 3600 }),
      session('2026-08-21T15:00:00Z', { duration_seconds: null }),
    ])
    expect(summary.totalDurationSeconds).toBe(3600)
    expect(summary.unknownDurations).toBe(1)
  })

  it('reports an empty day as empty', () => {
    expect(summarise([])).toEqual({ sessions: 0, totalDurationSeconds: 0, unknownDurations: 0 })
  })
})
