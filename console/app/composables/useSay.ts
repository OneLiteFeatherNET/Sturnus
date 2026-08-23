/**
 * The one place a decided sentence becomes words.
 *
 * `app/utils` returns {@link Message} trees and never text -- see
 * `utils/message.ts` for why. Something has to walk one of those trees with
 * a translator in hand, and it should be exactly one something: a second
 * implementation of "how is a count written, and which plural branch does
 * it choose" is a second answer, and the two would disagree first in
 * German.
 *
 * `say` is deliberately tolerant about what it is given, because the things
 * a template puts on screen are not all sentences. A figure can be a
 * message, a bare quantity, an instant, or nothing at all, and asking every
 * caller to narrow that before it can render is how a `v-if` ladder ends up
 * in a template with the em dash written out three times.
 */
import {
  NOT_MEASURED,
  isInstant,
  type Instant,
  type Message,
  type MessageValue,
} from '~/utils/message'

export function useSay() {
  const { t, n, d, locale, locales } = useI18n()

  /**
   * The BCP-47 tag behind the locale code, from the module's own
   * configuration.
   *
   * The code is `en`; the tag is `en-GB`, and the difference is visible:
   * `Intl` writes `August 21, 2026` for the first and `21 August 2026` for
   * the second. Read from `locales` rather than restated here, so a third
   * language stays a change in `nuxt.config.ts` alone.
   *
   * Cast because the generated type admits the two locale *codes* and this
   * is a language tag. `i18n.config.ts` registers every format under both
   * spellings for exactly that reason, so the value is one vue-i18n
   * resolves -- it is only the type that has never heard of it.
   */
  const tag = computed(
    () =>
      (locales.value.find((entry) => entry.code === locale.value)?.language
        ?? locale.value) as typeof locale.value,
  )

  function say(value: MessageValue | null | undefined): string {
    // Nothing to say is not an empty string: an empty cell in a row of
    // figures reads as a rendering fault, where an em dash reads as an
    // absence.
    if (value === null || value === undefined) return NOT_MEASURED
    if (typeof value === 'string') return value
    // A quantity, in the locale's own digits and grouping.
    if (typeof value === 'number') return n(value, { locale: tag.value })
    if (isInstant(value)) return date(value)
    return sentence(value)
  }

  function date(value: Instant): string {
    return d(value.at, { key: value.format, locale: tag.value })
  }

  function sentence(value: Message): string {
    const named: Record<string, string> = {}
    for (const [name, held] of Object.entries(value.params ?? {})) named[name] = say(held)

    // `count` is written into the sentence like any other value, and it
    // also decides which side of the `|` the sentence comes from. It has to
    // be handed over twice, because by the time it is in `named` it is a
    // formatted string -- `48,213` -- and vue-i18n chooses a plural branch
    // from numbers only.
    const count = value.params?.count
    return typeof count === 'number'
      ? t(value.key, named, { plural: count })
      : t(value.key, named)
  }

  return say
}
