/**
 * Reading and writing the console's per-browser preferences.
 *
 * Pulled out of the composable that uses it so the interesting part -- what
 * happens when the storage backend refuses to cooperate -- can be tested
 * without a Nuxt application around it. A `Storage` is passed in rather
 * than reached for globally, which is the same reason: a function that
 * looks up `localStorage` itself can only be tested where one exists.
 *
 * Every accessor is guarded. `localStorage` does not merely return null in
 * a private window with site data blocked -- it throws on access, and so
 * does an embedded preview. A console that failed to render because it
 * could not remember a sidebar width would be a poor trade for a
 * convenience.
 */

export const SIDEBAR_COLLAPSED_KEY = 'sturnus.sidebar.collapsed'

/** A narrower shape than `Storage`: exactly what is used, and no more. */
export interface KeyValueStore {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

/**
 * Whether the sidebar was left collapsed.
 *
 * Anything other than a stored `"1"` reads as expanded -- an unset
 * preference, a value written by an older version, or a backend that
 * threw. Expanded is the safe default: it is the mode that says what every
 * entry means.
 */
export function readCollapsed(storage: KeyValueStore | null | undefined): boolean {
  if (!storage) return false
  try {
    return storage.getItem(SIDEBAR_COLLAPSED_KEY) === '1'
  } catch {
    return false
  }
}

/**
 * Records the preference, and reports whether it was actually stored.
 *
 * The boolean is not decoration: a caller that wants to know whether the
 * choice will survive a reload can ask, and one that does not care can
 * ignore it. Either way a preference that could not be saved is still a
 * preference that applies for this visit, so this never throws.
 */
export function writeCollapsed(
  storage: KeyValueStore | null | undefined,
  collapsed: boolean,
): boolean {
  if (!storage) return false
  try {
    storage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? '1' : '0')
    return true
  } catch {
    return false
  }
}
