/**
 * How the console says a span of time out loud.
 *
 * Its own module because two unrelated views need the same words -- the
 * heatmap tooltip and the timeline bars -- and a duration that reads
 * "1 h 12 min" in one place and "72 minutes" in the other reads as two
 * products stitched together.
 *
 * Nothing here is localised. `Intl.RelativeTimeFormat` would give better
 * prose in other languages, but it also renders differently on the server
 * than in the browser whenever the two disagree about a locale, and a
 * hydration mismatch on every cell of a 365-cell grid is a poor trade for
 * prose nobody has asked for yet.
 */

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
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return 'length unknown'

  const total = Math.max(0, Math.floor(seconds))
  if (total === 0) return 'no time'
  // Below a minute the seconds are the interesting part: a 40-second
  // recording is somebody joining a channel by accident, and rounding it
  // to "0 min" would hide that it happened at all.
  if (total < 60) return `${total} s`

  const minutes = Math.floor(total / 60)
  if (minutes < 60) return `${minutes} min`

  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} min`
}
