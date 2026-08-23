/**
 * Reading a translation on a page that may be rendering because things are
 * broken.
 *
 * Everywhere else in the console `$t` is enough: if a locale file did not
 * load, the page it belonged to did not load either, and the reader gets
 * `error.vue`. `error.vue` is the page that has to survive that. It renders
 * precisely when something failed, and one of the things that can have
 * failed is the fetch of a lazily-loaded locale file -- at which point
 * `t('error.unreachableHeading')` returns `error.unreachableHeading`, and
 * the page whose job is to explain a failure in plain words is instead
 * showing its own source code to somebody who came here confused already.
 *
 * So the error page carries its English text in the source and treats the
 * translation as an improvement on it rather than as the thing itself.
 * English, because English is the source language: a reader who gets it has
 * a readable sentence, which is more than a key and more than a blank.
 */

/** The shape of `t` from `useI18n`, narrowed to what this needs. */
export type Translate = (key: string, named?: Record<string, unknown>) => string

/**
 * The translation of `key`, or `english` if there is not one.
 *
 * "There is not one" covers every way this can fail at once, because on the
 * error page they are the same situation: no translator at all (the i18n
 * plugin itself did not install), a translator that throws, a translator
 * that returns nothing, and -- the common case -- a translator that hands
 * the key straight back because the messages for this locale never arrived.
 */
export function translateOr(
  translate: Translate | null | undefined,
  key: string,
  english: string,
  named?: Record<string, unknown>,
): string {
  if (!translate) return english
  let value: string
  try {
    value = translate(key, named)
  } catch {
    return english
  }
  if (typeof value !== 'string' || value.trim() === '' || value === key) return english
  return value
}
