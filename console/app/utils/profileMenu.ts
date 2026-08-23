/**
 * What the profile control offers, and what it deliberately offers as
 * unavailable.
 *
 * A module rather than a list inside the component, for the same reason
 * `navigation.ts` is one: which entries exist, which of them can be acted
 * on, and what the initials of a person are, are all decisions -- and a
 * decision embedded in a template can only be tested by rendering one.
 *
 * **The two coming-soon rows are a promise, and promises are rendered.**
 * Two-factor and multi-factor authentication do not exist in this system
 * yet. They are listed anyway, as visibly inert rows that say so -- not as
 * links, not as buttons that do nothing, and not as a tooltip somebody has
 * to hover to discover. An interface that shows a control which silently
 * does nothing teaches people not to trust its controls, and that lesson is
 * learned once and applied to every control afterwards. A row that says "not
 * yet" costs one line and teaches the opposite.
 *
 * The same two rows are rendered again on `/settings`, from this same list,
 * so that the menu's promise has somewhere to land and the two places cannot
 * drift into promising different things.
 *
 * **Names are keys, not words** -- see `i18n/README.md`. Turning a key into
 * a sentence is the template's job; a pure function returns data.
 */

/**
 * How a row behaves, which is a different question from what it says.
 *
 * - `link` navigates within the console.
 * - `action` runs something in this tab and is a `button`.
 * - `unavailable` is a row and nothing else: no href, no handler, no
 *   activation. It exists to be read.
 */
export type ProfileItemKind = 'link' | 'action' | 'unavailable'

/** What an unavailable row says about itself. One constant rather than a
 *  string per row: the two rows are unavailable for the same reason, and
 *  saying it twice is how they end up saying it differently. */
export const COMING_SOON_KEY = 'profile.comingSoon'

export interface ProfileMenuItem {
  /** Stable, and the handle a test or a template addresses a row by. Not
   *  derived from the label, which is a translated sentence. */
  id: 'settings' | 'signOut' | 'twoFactor' | 'multiFactor'
  /** A translation key, resolved by whoever renders this. Named `…Key` so
   *  that nothing puts it on screen by mistake. */
  labelKey: string
  kind: ProfileItemKind
  /** Where a `link` goes. Absent on every other kind. */
  to?: string
  /** What an `unavailable` row says about itself, as a key. Absent on the
   *  kinds that work. */
  noteKey?: string
}

export const PROFILE_MENU_ITEMS: readonly ProfileMenuItem[] = [
  { id: 'settings', labelKey: 'profile.settings', kind: 'link', to: '/settings' },
  // The existing sign-out, unchanged: it POSTs `/auth/logout` and then
  // performs a full navigation. That is deliberate -- see `AppHeader.vue`.
  { id: 'signOut', labelKey: 'auth.signOut', kind: 'action' },
  {
    id: 'twoFactor',
    labelKey: 'profile.twoFactor',
    kind: 'unavailable',
    noteKey: COMING_SOON_KEY,
  },
  {
    id: 'multiFactor',
    labelKey: 'profile.multiFactor',
    kind: 'unavailable',
    noteKey: COMING_SOON_KEY,
  },
]

/** Whether this row can be acted on at all. */
export function isActionable(item: ProfileMenuItem): boolean {
  return item.kind !== 'unavailable'
}

/**
 * The rows that say "not yet", which the Security section of `/settings`
 * renders as well.
 *
 * Derived rather than declared a second time: the promise the menu makes and
 * the place it lands are the same two rows, and two lists would eventually
 * be two different lists.
 */
export const UNAVAILABLE_ITEMS: readonly ProfileMenuItem[] = PROFILE_MENU_ITEMS.filter(
  (item) => item.kind === 'unavailable',
)

/**
 * What goes in the circle at the top right.
 *
 * **Initials, and never an avatar.** An avatar would have to come from
 * Discord, this API holds no Discord token, and mirroring every linked
 * person's picture into this database to decorate a menu is not a trade this
 * system should make.
 *
 * One letter from the first word and one from the last: "Ada Lovelace" is
 * `AL`, "Ada" is `A`. Letters and digits only, so a name that opens with an
 * emoji or a bracket -- which Discord display names frequently do -- yields
 * the initial of the name rather than a mark nobody can read at 12 pixels.
 *
 * The fallback is a question mark, and it is chosen rather than settled for.
 * The alternatives were the first characters of the Discord snowflake, which
 * are somebody's initials only by accident, and a blank circle, which reads
 * as a control that failed to load. `?` says the console does not know the
 * name yet -- which is the truth while `display_name` is being added to
 * `/api/me` in a different pull request, and stays the truth for anybody
 * whose link predates it.
 */
export function initialsFor(displayName?: string | null): string {
  const words = (displayName ?? '')
    .split(/\s+/)
    .map(firstLetter)
    .filter((letter) => letter !== '')
  if (words.length === 0) return '?'
  const first = words[0] ?? ''
  const last = words.length > 1 ? (words[words.length - 1] ?? '') : ''
  return `${first}${last}`
}

/** The first letter or digit of a word, upper-cased, or `''` if it has none. */
function firstLetter(word: string): string {
  const found = word.match(/[\p{L}\p{N}]/u)?.[0] ?? ''
  // `slice` after upper-casing, because `ß`.toUpperCase() is two characters
  // and an initial is one.
  return found.toUpperCase().slice(0, 1)
}

/**
 * Whether there is a name to show at all.
 *
 * A name of nothing but spaces is no name: `display_name` is copied from
 * Outline, and Outline does not police what somebody typed there.
 */
export function hasDisplayName(displayName?: string | null): boolean {
  return (displayName ?? '').trim() !== ''
}
