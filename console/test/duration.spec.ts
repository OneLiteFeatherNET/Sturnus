/**
 * How a span of seconds is said out loud.
 *
 * Its own module because two very different things need the same words --
 * the heatmap tooltip and the timeline bars -- and a duration that reads
 * "1 h 12 min" in one place and "72 minutes" in the other is two products.
 */
import { describe, expect, it } from 'vitest'

import { formatDuration } from '../app/utils/duration'

describe('saying a duration', () => {
  it('says nothing was recorded when nothing was', () => {
    expect(formatDuration(0)).toBe('no time')
  })

  it('says seconds for a meeting shorter than a minute', () => {
    // A 40-second recording is almost certainly somebody joining a channel
    // by accident. Rounding it to "0 min" would hide that it happened.
    expect(formatDuration(40)).toBe('40 s')
  })

  it('says minutes below an hour', () => {
    expect(formatDuration(600)).toBe('10 min')
  })

  it('rounds a part-minute down rather than inventing a minute', () => {
    expect(formatDuration(119)).toBe('1 min')
  })

  it('says hours and minutes once an hour has passed', () => {
    expect(formatDuration(4320)).toBe('1 h 12 min')
  })

  it('drops the minutes when a duration lands on the hour', () => {
    expect(formatDuration(7200)).toBe('2 h')
  })

  it('keeps counting hours past a day rather than rolling over to days', () => {
    // The API sums a whole UTC day of recordings, and a busy guild can pass
    // 24 hours of them across parallel channels. "1 d 2 h" would read as a
    // meeting that ran overnight, which is not what the number means.
    expect(formatDuration(93600)).toBe('26 h')
  })

  it('says so when the duration was never recorded', () => {
    // `duration_seconds` is nullable: a session still running, or one whose
    // worker died before it wrote a length. Neither is zero.
    expect(formatDuration(null)).toBe('length unknown')
  })
})
