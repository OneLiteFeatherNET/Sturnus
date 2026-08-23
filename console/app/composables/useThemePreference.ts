/**
 * Which theme this console is in, and how to change that.
 *
 * The shape deliberately matches `useLocalePreference`: `available`,
 * `current`, `choose`. They are the two halves of the same setting, they sit
 * next to each other on `/settings`, and a reader of one should be able to
 * guess the other.
 *
 * **One writer.** The cookie ref below is the only thing in this console
 * that writes `sturnus_theme`, exactly as `setLocale` is the only thing that
 * writes `sturnus_locale`. A second writer of one value is two pieces of
 * code that will eventually disagree about the path or the expiry, and the
 * disagreement shows up as a preference that survives a reload on one page
 * and not on another.
 *
 * The attribute is put on `<html>` by `plugins/theme.ts` rather than here,
 * so that the composable stays something a page can call without also
 * claiming the document.
 */
import {
  THEME_CHOICES,
  THEME_COOKIE,
  THEME_COOKIE_MAX_AGE,
  readTheme,
  themeAttribute,
  type ThemeChoice,
} from '~/utils/theme'

export function useThemePreference() {
  // Typed as a plain string rather than as `ThemeChoice`: what comes back
  // is whatever is in somebody's browser, and `readTheme` is what turns
  // that into one of three answers.
  const cookie = useCookie<string | null>(THEME_COOKIE, {
    // A year. The preference is a person's, not a session's -- being asked
    // to choose dark again every fortnight is how a setting teaches people
    // it does not work.
    maxAge: THEME_COOKIE_MAX_AGE,
    path: '/',
    // Readable by the server render, which is the entire point, and
    // therefore not `httpOnly`: the client has to be able to write it.
    sameSite: 'lax',
  })

  const current = computed<ThemeChoice>(() => readTheme(cookie.value))

  /** What `data-theme` on `<html>` should say. */
  const attribute = computed(() => themeAttribute(current.value))

  function choose(choice: ThemeChoice): void {
    if (choice === current.value) return
    cookie.value = choice
  }

  return { available: THEME_CHOICES, current, attribute, choose }
}
