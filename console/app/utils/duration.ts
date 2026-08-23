/**
 * How the console says a span of time out loud.
 *
 * Its own module because two unrelated views need the same words -- the
 * heatmap tooltip and the timeline bars -- and a duration that reads
 * "1 h 12 min" in one place and "72 minutes" in the other reads as two
 * products stitched together.
 *
 * **This used to say that nothing here was localised**, and the reason
 * given was hydration: `Intl` renders for whatever locale the runtime
 * resolves, and a server and a browser that disagreed about one would
 * disagree about all 365 cells of the calendar grid. That reasoning was
 * about an *ambient* locale. The console now has a chosen one, carried in a
 * cookie that travels with the request, so the server and the browser
 * render the same language and there is nothing left to mismatch.
 *
 * What replaces it is not `Intl.RelativeTimeFormat` either. This function
 * returns a key and its numbers, and the locale file decides the words --
 * which is how German gets "1 Std. 12 Min." rather than an abbreviation
 * borrowed from English.
 */
import type { Message } from './message'

/**
 * A duration in words.
 *
 * `null` is not zero. `duration_seconds` is nullable on the API: a session
 * still running, or one whose worker died before it wrote a length. Saying
 * "no time" for those would claim a meeting took no time at all.
 *
 * Hours keep counting past 24 rather than rolling into days, because the
 * heatmap sums a whole UTC day across parallel channels and can genuinely
 * pass 24 hours of recording. "1 d 2 h" would read as one meeting that ran
 * overnight, which is not what the number means.
 *
 * Named `durationMessage` rather than `formatDuration`, which is what it
 * was: Nuxt auto-imports every export under `app/utils` into one namespace,
 * and this name collided with `~/utils/format`'s. The build warned, picked
 * one by file order and told nobody, so the calendar and the dashboard were
 * one rename away from quietly swapping their idea of what a duration looks
 * like. The suffix also says what the return value is -- a key and its
 * numbers, not a sentence anybody can put on screen.
 */
export function durationMessage(seconds: number | null | undefined): Message {
  if (seconds === null || seconds === undefined) return { key: 'common.durationUnknown' }

  const total = Math.max(0, Math.floor(seconds))
  if (total === 0) return { key: 'common.durationNone' }
  // Below a minute the seconds are the interesting part: a 40-second
  // recording is somebody joining a channel by accident, and rounding it
  // to "0 min" would hide that it happened at all.
  if (total < 60) return { key: 'common.durationSeconds', params: { count: total } }

  const minutes = Math.floor(total / 60)
  if (minutes < 60) return { key: 'common.durationMinutes', params: { count: minutes } }

  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0
    ? { key: 'common.durationHours', params: { count: hours } }
    : { key: 'common.durationHoursMinutes', params: { hours, minutes: rest } }
}

/**
 * The same duration, in English, as a string.
 *
 * **English, and knowingly so.** `~/utils/queue` still builds English
 * sentences by hand for the Queue page, which is being rewritten in another
 * pull request; translating it here would collide with that one. This is
 * what it calls, and it keeps exactly the behaviour it was written against.
 * It goes when that page is translated.
 */
export function formatDuration(seconds: number | null | undefined): string {
  const said = durationMessage(seconds)
  const values = said.params ?? {}
  switch (said.key) {
    case 'common.durationUnknown':
      return 'length unknown'
    case 'common.durationNone':
      return 'no time'
    case 'common.durationSeconds':
      return `${values.count} s`
    case 'common.durationMinutes':
      return `${values.count} min`
    case 'common.durationHours':
      return `${values.count} h`
    default:
      return `${values.hours} h ${values.minutes} min`
  }
}
