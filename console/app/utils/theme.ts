/**
 * Which theme a person chose, and what that choice does to `<html>`.
 *
 * A module rather than a branch inside a component, for the reason every
 * other module in `app/utils` exists: the mapping from a stored value to an
 * attribute is a decision, and a decision embedded in a template can only be
 * tested by rendering one.
 *
 * **Three choices, not two.** `system` is a real answer and it is the
 * default: the console already honours `prefers-color-scheme`, so somebody
 * who never expressed a preference keeps the theme their operating system
 * changes for them at dusk. Storing `light` as the default would override
 * that for everybody who never asked -- the one behaviour nobody wants and
 * everybody notices.
 *
 * **The attribute is always written, `system` included.** The alternative
 * -- omitting it and letting the media query govern -- means the difference
 * between "no choice" and "chose system" is the absence of an attribute,
 * and removing an attribute on a change is the part of head management that
 * quietly does not happen. A value that is always present is a value the
 * stylesheet can be written against, and `[data-theme="system"]` is matched
 * by no rule in `main.css` on purpose: it means "let the media query
 * decide", which is exactly what it says.
 */

/**
 * The cookie the choice is kept in.
 *
 * A cookie rather than `localStorage`, and for the same reason the locale is
 * one (see `nuxt.config.ts`): this console renders on the server, and a
 * cookie travels with the request, so the very first HTML already carries
 * the right `data-theme`. A choice readable only after hydration would paint
 * the other theme first on every server-rendered navigation, in front of the
 * reader, every time.
 *
 * Named for the same shelf as `sturnus_locale`, because a later pull request
 * mirrors both into `user_preference` and a pair of cookies with two naming
 * schemes is a pair somebody will mirror only half of.
 */
export const THEME_COOKIE = 'sturnus_theme'

/** How long a browser is asked to keep the choice: a year, in seconds. */
export const THEME_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

export type ThemeChoice = 'system' | 'light' | 'dark'

/**
 * The choices, in the order they are offered.
 *
 * `system` first because it is the default, and because a reader scanning
 * the row should meet "the one I already have" before the two that override
 * it.
 */
export const THEME_CHOICES: readonly ThemeChoice[] = ['system', 'light', 'dark']

export const DEFAULT_THEME: ThemeChoice = 'system'

export function isThemeChoice(value: unknown): value is ThemeChoice {
  return typeof value === 'string' && (THEME_CHOICES as readonly string[]).includes(value)
}

/**
 * What a stored value means.
 *
 * Anything unrecognised reads as `system`: an empty cookie, a value written
 * by a version that offered a fourth theme, or a string somebody typed into
 * their browser's storage inspector. Falling back to the default is the only
 * answer that cannot leave the console in a theme nobody selected.
 */
export function readTheme(stored: unknown): ThemeChoice {
  return isThemeChoice(stored) ? stored : DEFAULT_THEME
}

/**
 * The value of `data-theme` on `<html>` for a choice.
 *
 * The identity function today, and it is still a function: it is the one
 * place where "what is stored" and "what the stylesheet is written against"
 * meet, and the day those two diverge -- a stored `auto` renamed to
 * `system`, a `dark-high-contrast` that maps onto `dark` -- this is where it
 * happens rather than in three templates.
 */
export function themeAttribute(choice: ThemeChoice): ThemeChoice {
  return choice
}

/** The label for a choice, as a key. See `i18n/README.md` on why a key. */
export function themeLabelKey(choice: ThemeChoice): string {
  return `settings.appearance.${choice}`
}
