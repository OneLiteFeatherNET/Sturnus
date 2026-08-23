/**
 * How a span of seconds is said out loud.
 *
 * Its own module because two very different things need the same words --
 * the heatmap tooltip and the timeline bars -- and a duration that reads
 * "1 h 12 min" in one place and "72 minutes" in the other is two products.
 *
 * What is asserted here is the *decision* -- which unit a length is worth
 * saying in, and which lengths are not lengths at all -- and never the
 * words. The words are `i18n/locales/*.json`'s, and a test that pinned them
 * would be a test that has to be edited every time somebody improves a
 * sentence. Whether the keys below resolve to anything is
 * `test/i18n.spec.ts`'s question; whether the numbers land in them is
 * `test/message.spec.ts`'s.
 */
import { describe, expect, it } from 'vitest'

import { durationMessage, formatDuration } from '../app/utils/duration'

describe('saying a duration', () => {
  it('says nothing was recorded when nothing was', () => {
    expect(durationMessage(0)).toEqual({ key: 'common.durationNone' })
  })

  it('says seconds for a meeting shorter than a minute', () => {
    // A 40-second recording is almost certainly somebody joining a channel
    // by accident. Rounding it to "0 min" would hide that it happened.
    expect(durationMessage(40)).toEqual({ key: 'common.durationSeconds', params: { count: 40 } })
  })

  it('says minutes below an hour', () => {
    expect(durationMessage(600)).toEqual({ key: 'common.durationMinutes', params: { count: 10 } })
  })

  it('rounds a part-minute down rather than inventing a minute', () => {
    expect(durationMessage(119)).toEqual({ key: 'common.durationMinutes', params: { count: 1 } })
  })

  it('says hours and minutes once an hour has passed', () => {
    expect(durationMessage(4320)).toEqual({
      key: 'common.durationHoursMinutes',
      params: { hours: 1, minutes: 12 },
    })
  })

  it('drops the minutes when a duration lands on the hour', () => {
    // A different key rather than a zero in the minutes: "2 h 0 min" is a
    // sentence about arithmetic, and which languages want a bare hour said
    // which way is not this module's business.
    expect(durationMessage(7200)).toEqual({ key: 'common.durationHours', params: { count: 2 } })
  })

  it('keeps counting hours past a day rather than rolling over to days', () => {
    // The API sums a whole UTC day of recordings, and a busy guild can pass
    // 24 hours of them across parallel channels. "1 d 2 h" would read as a
    // meeting that ran overnight, which is not what the number means.
    expect(durationMessage(93600)).toEqual({ key: 'common.durationHours', params: { count: 26 } })
  })

  it('says so when the duration was never recorded', () => {
    // `duration_seconds` is nullable: a session still running, or one whose
    // worker died before it wrote a length. Neither is zero, and neither is
    // the same key as zero.
    expect(durationMessage(null)).toEqual({ key: 'common.durationUnknown' })
    expect(durationMessage(undefined)).toEqual({ key: 'common.durationUnknown' })
  })

  it('names the count so that a language can pluralise by it', () => {
    // The parameter is `count` and not `seconds` on purpose: vue-i18n reads
    // the plural form off a value with that name, so a language that wants
    // a different word for one of something can have it without this
    // module knowing which languages those are.
    expect(durationMessage(1).params).toHaveProperty('count', 1)
    expect(durationMessage(60).params).toHaveProperty('count', 1)
  })
})

describe('the English renderer the Queue page still reads', () => {
  // `~/utils/queue` builds English sentences by hand and is out of scope
  // until the pull request rewriting that page lands. This is what it
  // calls, and it exists to keep that page working unchanged -- so it is
  // worth one test that it still says what it used to.
  it('says the same words it said before any of this', () => {
    expect(formatDuration(0)).toBe('no time')
    expect(formatDuration(40)).toBe('40 s')
    expect(formatDuration(600)).toBe('10 min')
    expect(formatDuration(4320)).toBe('1 h 12 min')
    expect(formatDuration(7200)).toBe('2 h')
    expect(formatDuration(93600)).toBe('26 h')
    expect(formatDuration(null)).toBe('length unknown')
  })
})
