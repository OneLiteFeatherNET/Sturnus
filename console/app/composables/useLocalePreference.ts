/**
 * Which languages the console speaks, which one it is speaking, and how to
 * change that.
 *
 * **Nothing renders this yet.** It exists so that the pull request which
 * adds the language control to the profile menu is a pull request about a
 * menu, and not one that also has to decide where the choice is kept and
 * what the list of languages is. Those are two different arguments, and
 * bundling them is how the smaller one gets settled by whoever was in a
 * hurry.
 *
 * The list comes from the module's own configuration rather than a constant
 * here, so adding a third language stays a one-line change in
 * `nuxt.config.ts` and cannot leave a locale that exists but is never
 * offered.
 */

export interface LocaleChoice {
  /** What `setLocale` takes, and what the cookie stores -- `en`, `de`. */
  code: string
  /** The language's name in that language. A German speaker looking for
   *  German is looking for "Deutsch", not for "German" -- which they can
   *  only recognise if they already read enough English to not need the
   *  setting. */
  name: string
}

export function useLocalePreference() {
  const { locale, locales, setLocale } = useI18n()

  const available = computed<LocaleChoice[]>(() =>
    locales.value.map((entry) => ({
      code: entry.code,
      name: entry.name ?? entry.code,
    })),
  )

  const current = computed(() => locale.value)

  /**
   * Switch to `code` and remember it.
   *
   * The remembering is `setLocale`'s: with `detectBrowserLanguage.useCookie`
   * on, it writes the `sturnus_locale` cookie itself. Doing it here as well
   * would be a second writer of one value, and the two would eventually
   * disagree about the domain or the expiry.
   */
  async function choose(code: string): Promise<void> {
    if (code === locale.value) return
    await setLocale(code as typeof locale.value)
  }

  return { available, current, choose }
}
