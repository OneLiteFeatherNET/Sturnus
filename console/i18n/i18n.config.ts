/**
 * The Vue I18n runtime, as opposed to the module wiring in `nuxt.config.ts`.
 *
 * The first decision here: **a missing German string falls back to English,
 * never to the key.** A reader who has chosen German and meets a sentence
 * nobody has translated yet should read an English sentence -- which is
 * information -- rather than `admin.queue.emptyNote`, which is the console
 * admitting a bug in a place the reader cannot act on.
 *
 * The warnings are silenced in production for the same reason they are kept
 * in development: a missing key is a thing to fix while writing the page,
 * and a thing to survive quietly once it is in front of somebody.
 *
 * The second decision is the date and number formats below, and it is the
 * larger one.
 *
 * ## Dates are named shapes, not hand-assembled strings
 *
 * Several modules used to write their own month names out in English, with
 * a comment defending the choice: `Intl` formats for whatever locale the
 * runtime resolves, so a server render and a browser could disagree and Vue
 * would report the difference as a hydration mismatch. That argument was
 * sound and it no longer applies. The locale is now a decision this
 * application makes -- carried in the `sturnus_locale` cookie, so it
 * travels with the request and the server and the browser render the same
 * language. There is nothing left to disagree about, and hand-written
 * `MONTH_NAMES` arrays could only ever have been English.
 *
 * ## The zone each shape is written in is pinned, and it is not the same
 *
 * Everything derived from a **UTC day** -- the calendar grid, the day
 * heading, a report's months -- is formatted with `timeZone: 'UTC'`. Those
 * are not instants; they are buckets the API cut in UTC, and re-reading one
 * in the viewer's zone is how `2026-08-21` becomes 20 August for anybody
 * west of Greenwich. `utcMoment` is an instant and says its zone out loud,
 * which is the honest half of showing somebody a time that is not theirs.
 *
 * `clock` is the exception and the only one: it carries no zone, so it
 * renders in the viewer's. It labels the day timeline, which never renders
 * on the server -- it appears after a click, by which time there is a
 * browser with a real zone to ask -- and the panel says which zone it is
 * showing.
 *
 * ## Why the formats are registered under four keys
 *
 * The locale *code* is `en`; the language is `en-GB`, and `Intl` reads them
 * differently -- `August 21, 2026` against `21 August 2026`. `useSay`
 * therefore formats with the tag rather than the code, and a tag with no
 * formats registered under it would silently fall back to the code's. So
 * both spellings carry the same table.
 */
const DATETIME_FORMATS = {
  /** A whole day, weekday included. `Intl` decides whether a comma goes
   *  after the weekday, which is a thing the two languages disagree on. */
  fullDate: {
    weekday: 'long',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    timeZone: 'UTC',
  },
  /** A day on its own. */
  longDate: { year: 'numeric', month: 'long', day: 'numeric', timeZone: 'UTC' },
  /** A month, for a report's rows. */
  monthYear: { year: 'numeric', month: 'long', timeZone: 'UTC' },
  /** A month heading above a run of week columns, where three letters is
   *  all the width there is. */
  shortMonth: { month: 'short', timeZone: 'UTC' },
  /** A weekday name, and the shortened one the heatmap's rows carry. */
  weekday: { weekday: 'long', timeZone: 'UTC' },
  weekdayShort: { weekday: 'short', timeZone: 'UTC' },
  /** An instant, in UTC, saying so. `timeZoneName` is what makes it say
   *  so: an unlabelled time in a zone that is not yours is worse than a
   *  labelled one. */
  utcMoment: {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: 'UTC',
    timeZoneName: 'short',
  },
  /** A time of day in the viewer's own zone. The only shape here without a
   *  zone pinned to it. */
  clock: { hour: '2-digit', minute: '2-digit', hour12: false },
} as const

const NUMBER_FORMATS = {
  /** A share of a track that was speech. German writes the space before
   *  the sign and English does not, which is exactly the sort of thing
   *  nobody remembers to do by hand. */
  percent: { style: 'percent', maximumFractionDigits: 0 },
} as const

export default defineI18nConfig(() => ({
  fallbackLocale: 'en',
  missingWarn: import.meta.dev,
  fallbackWarn: import.meta.dev,
  datetimeFormats: {
    'en': DATETIME_FORMATS,
    'en-GB': DATETIME_FORMATS,
    'de': DATETIME_FORMATS,
    'de-DE': DATETIME_FORMATS,
  },
  numberFormats: {
    'en': NUMBER_FORMATS,
    'en-GB': NUMBER_FORMATS,
    'de': NUMBER_FORMATS,
    'de-DE': NUMBER_FORMATS,
  },
}))
