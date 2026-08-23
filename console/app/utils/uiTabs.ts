/**
 * A tab bar, and the three promises it makes.
 *
 * **A tab is a place.** `paging.ts` already argued this for the recordings
 * list: a list somebody has paged into needs an address, or the back
 * button leaves them somewhere they did not put themselves and no link can
 * express where they were. A tab is the same thing with fewer numbers in
 * it. So the selected tab lives in the query string, and this module is
 * where the query string and the tab meet — nowhere else has to know the
 * parameter's name.
 *
 * **A panel that exists has not necessarily been opened.** Rendering four
 * panels and hiding three is how a tab bar quietly fires four requests,
 * and the expensive one is reliably the tab nobody clicks. `panelsAfter`
 * is the whole of the lazy-mounting rule.
 *
 * **A panel that has been opened stays open underneath.** The other
 * extreme — unmounting on every switch — turns a tab bar into a reload
 * button with three faces and throws away the scroll position, the
 * half-filled form, and the answer that had just arrived.
 */

/**
 * One tab.
 *
 * `label` is text rather than a key: a tab bar's labels come from the page
 * that owns it, which knows whether they are translated sentences or a
 * guild's own names, and `$t` belongs in that page's template.
 */
export interface UiTab {
  id: string
  label: string
  disabled?: boolean
}

/** The query parameter a tab is written in. One name, in one place. */
export const TAB_QUERY = 'tab'

/**
 * A query string as `vue-router` models one.
 *
 * Restated rather than imported from the router: this module is arithmetic
 * over an address and has no other reason to depend on a routing library,
 * and `Record<string, unknown>` would not be assignable to what
 * `router.replace` wants — so the honest narrow type is the one that
 * type-checks at the call site.
 */
export type TabQuery = Record<string, string | null | (string | null)[]>

const openable = (tab: UiTab | undefined): boolean => tab !== undefined && tab.disabled !== true

function firstOpenable(tabs: readonly UiTab[]): string | null {
  return tabs.find((tab) => openable(tab))?.id ?? null
}

/**
 * The tab an address names, or the first one.
 *
 * Anything that is not an openable tab falls back rather than failing:
 * `?tab=nonsense` is a typo in an address bar or a bookmark from before a
 * rename, and answering either with an error page turns a slip into a dead
 * end. `paging.pageFromQuery` takes exactly the same line with `?page=0`.
 *
 * `null` only when there is no tab to show at all, which is a real state —
 * a bar whose every tab is gated behind a permission the reader does not
 * have.
 */
export function tabFromQuery(raw: unknown, tabs: readonly UiTab[]): string | null {
  // `?tab=a&tab=b` reaches a router as an array. The first is the answer,
  // the way it is for every other repeated parameter.
  const first = Array.isArray(raw) ? raw[0] : raw
  const wanted = String(first ?? '')
  const found = tabs.find((tab) => tab.id === wanted)
  return openable(found) ? found!.id : firstOpenable(tabs)
}

/**
 * The query a tab's link carries.
 *
 * The rest of the query survives — a tab switch that dropped `?guild=…`
 * would move the reader to another server's panel, which is a
 * data-shaped bug wearing a navigation costume — and the first tab drops
 * the parameter entirely, so the plain address of the page stays the
 * address of the page rather than becoming a synonym for `?tab=overview`.
 */
export function queryForTab(
  query: Readonly<TabQuery>,
  tabs: readonly UiTab[],
  id: string,
): TabQuery {
  // Rebuilt rather than copied-and-deleted: the parameter is absent for
  // the first tab, and an absent key is not the same as one holding
  // `undefined` — a router writes the second one into the address as
  // `?tab=`.
  const next: TabQuery = {}
  for (const [name, value] of Object.entries(query)) {
    if (name !== TAB_QUERY) next[name] = value
  }
  if (id !== firstOpenable(tabs)) next[TAB_QUERY] = id
  return next
}

/**
 * The tab a key moves to, or `null` for a key the bar has no opinion
 * about.
 *
 * **This wraps, and the dropdown deliberately does not.** A tab bar is
 * short and entirely on screen, so the wrap is visible as it happens and
 * saves four presses; a two-hundred-row channel list is neither, and
 * wrapping there is a silent teleport past everything the reader was
 * scanning.
 */
export function moveTab(tabs: readonly UiTab[], current: string, key: string): string | null {
  if (tabs.length === 0) return null
  if (key === 'Home') return firstOpenable(tabs)
  if (key === 'End') {
    return [...tabs].reverse().find((tab) => openable(tab))?.id ?? null
  }
  if (key !== 'ArrowRight' && key !== 'ArrowLeft') return null

  const from = tabs.findIndex((tab) => tab.id === current)
  if (from < 0) return firstOpenable(tabs)
  const step = key === 'ArrowRight' ? 1 : -1
  for (let hop = 1; hop <= tabs.length; hop += 1) {
    const at = (((from + step * hop) % tabs.length) + tabs.length) % tabs.length
    if (openable(tabs[at])) return tabs[at]!.id
  }
  return null
}

/**
 * The panels that have been built, once this one has been shown.
 *
 * The same array back when nothing changed, so a component watching it
 * does not re-render a set of panels because somebody clicked the tab they
 * were already on.
 */
export function panelsAfter(mounted: readonly string[], id: string): readonly string[] {
  return mounted.includes(id) ? mounted : [...mounted, id]
}

/**
 * The ids a tab and its panel point at each other with.
 *
 * Built from the position rather than from the tab's own id, for the same
 * reason `optionDomId` is: an id here is whatever the owning page called
 * the tab, and that is not guaranteed to be a valid DOM id. Two functions
 * rather than one with a suffix, because `aria-controls` and
 * `aria-labelledby` point at each other and a single id serving both would
 * be a cycle rather than a relationship.
 */
export function tabDomId(base: string, index: number): string {
  return `${base}-tab-${index}`
}

export function panelDomId(base: string, index: number): string {
  return `${base}-panel-${index}`
}
