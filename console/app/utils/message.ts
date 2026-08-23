/**
 * What a pure module hands back when it has decided on a sentence but not
 * on a language.
 *
 * `i18n/README.md` states the rule: a module under `app/utils` returns a
 * translation key, never a sentence, because threading a translator through
 * these functions would give every one of them a Vue application to carry
 * before it could be asked a question. A key is data, and data is what a
 * pure function should return.
 *
 * Most of this console's sentences are not bare keys, though. They count
 * things, they name a channel, they quote an instant, and several of them
 * put one decided sentence inside another -- a heatmap cell says a date, a
 * number of meetings, a length and a word for how busy the day was, and
 * every one of those four is itself a decision this module's callers make.
 * So a message is a small tree:
 *
 * - a **key**, always;
 * - **params**, each of which is a string, a quantity, an instant, or
 *   another message.
 *
 * Nesting rather than concatenation is the whole point. A sentence built by
 * gluing fragments together carries the word order of whoever glued it, and
 * no translation can move the pieces; a sentence with named holes in it can
 * be rewritten from scratch in German and still be handed the same four
 * values. It is the same reason `sign-in.vue` renders its one marked-up
 * sentence through `<i18n-t>` instead of splitting it around the `<code>`.
 *
 * **A number in `params` is a quantity.** `useSay` writes it in the
 * locale's number format, so 48213 reads as `48,213` for an English reader
 * and `48.213` for a German one. Anything that is a number without being a
 * quantity -- a year, a status code, an id, a page number -- is passed as a
 * string, because `2,026` is not a year.
 *
 * **A param named `count` chooses the plural form.** vue-i18n picks the
 * branch of `one | many` from it, so a module says how many there are and
 * the locale file decides what that does to the sentence. German and
 * English disagree about that often enough that an `if` in a module would
 * be an English decision made in a place German cannot reach.
 */

/**
 * An instant, and the shape it is to be written in.
 *
 * `format` names a datetime format in `i18n/i18n.config.ts`, which is where
 * the zone each of them is pinned to is decided and argued for. A module
 * says *which* instant and *which* shape; it never says which month names
 * or which order the parts go in, because those are the two things that
 * differ between the languages.
 */
export interface Instant {
  at: Date
  format: string
}

export type MessageValue = string | number | Instant | Message

export interface Message {
  key: string
  params?: Record<string, MessageValue>
}

/**
 * The one thing that means "there is no figure here". Never "0".
 *
 * An em dash rather than "0", "n/a" or an empty cell: it reads as an
 * absence at a glance, lines up in a column of numbers, and cannot be
 * mistaken for a measurement. It is a glyph and not a word, which is why it
 * lives here rather than in the locale files -- there is nothing about it
 * for a translator to decide.
 *
 * One constant for the whole console. It used to be three: `NOTHING` in
 * `~/utils/format`, `NO_FIGURE` in `~/utils/reporting` and `NOT_MEASURED`
 * here, all of them the same character and each free to stop being it.
 */
export const NOT_MEASURED = '—'

export function isInstant(value: unknown): value is Instant {
  return typeof value === 'object' && value !== null && 'at' in value
}
