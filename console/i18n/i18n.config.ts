/**
 * The Vue I18n runtime, as opposed to the module wiring in `nuxt.config.ts`.
 *
 * The one decision here: **a missing German string falls back to English,
 * never to the key.** A reader who has chosen German and meets a sentence
 * nobody has translated yet should read an English sentence -- which is
 * information -- rather than `admin.queue.emptyNote`, which is the console
 * admitting a bug in a place the reader cannot act on.
 *
 * The warnings are silenced in production for the same reason they are kept
 * in development: a missing key is a thing to fix while writing the page,
 * and a thing to survive quietly once it is in front of somebody.
 */
export default defineI18nConfig(() => ({
  fallbackLocale: 'en',
  missingWarn: import.meta.dev,
  fallbackWarn: import.meta.dev,
}))
