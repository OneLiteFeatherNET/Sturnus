/**
 * Which tab is showing, where that fact is written down, and what is
 * allowed to have loaded.
 *
 * Three separate promises, and each of them is broken by a different kind
 * of carelessness.
 *
 * **A tab is a place.** The console already holds this line for the
 * recordings pager — every page of the list has an address, because a list
 * somebody has paged into is somewhere they can be. The same is true of a
 * tab: without the query parameter, the back button leaves the panel
 * somewhere the reader did not put it and no link can say "the Queue tab
 * of this guild".
 *
 * **A panel that exists has not necessarily been opened.** Rendering all
 * four panels and hiding three is how a tab bar quietly fires four
 * requests, three of which nobody asked for. The expensive one is usually
 * the one nobody clicks.
 *
 * **A panel that has been opened stays open underneath.** Unmounting on
 * every switch turns a tab bar into a reload button and throws away the
 * scroll position, the half-filled form and the answer that just arrived.
 */
import { describe, expect, it } from 'vitest'

import {
  TAB_QUERY,
  type UiTab,
  moveTab,
  panelDomId,
  panelsAfter,
  queryForTab,
  tabDomId,
  tabFromQuery,
} from '../app/utils/uiTabs'

const TABS: UiTab[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'queue', label: 'Queue' },
  { id: 'consents', label: 'Consents' },
]

const WITH_HOLE: UiTab[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'queue', label: 'Queue', disabled: true },
  { id: 'consents', label: 'Consents' },
]

describe('which tab the address names', () => {
  it('is the one in the query when it is a real tab', () => {
    expect(tabFromQuery('queue', TABS)).toBe('queue')
  })

  it('is the first one when the query says nothing', () => {
    expect(tabFromQuery(undefined, TABS)).toBe('overview')
    expect(tabFromQuery('', TABS)).toBe('overview')
  })

  it('is the first one when the query names a tab that does not exist', () => {
    // A hand-edited address, or a bookmark from before a tab was renamed.
    // Falling back is not an error page: somebody in the address bar has
    // made a typo, not found a fault.
    expect(tabFromQuery('nonsense', TABS)).toBe('overview')
  })

  it('is the first one when the query names a tab nobody may open', () => {
    expect(tabFromQuery('queue', WITH_HOLE)).toBe('overview')
  })

  it('takes the first of a repeated parameter rather than choking on the array', () => {
    // `?tab=queue&tab=consents` is what a duplicated link produces, and
    // `vue-router` hands it over as an array.
    expect(tabFromQuery(['queue', 'consents'], TABS)).toBe('queue')
  })

  it('is nothing at all when there is no tab to show', () => {
    expect(tabFromQuery('queue', [])).toBeNull()
    expect(tabFromQuery(undefined, [{ id: 'a', label: 'A', disabled: true }])).toBeNull()
  })
})

describe('the address a tab gets', () => {
  it('names the tab', () => {
    expect(queryForTab({}, TABS, 'queue')).toEqual({ [TAB_QUERY]: 'queue' })
  })

  it('drops the parameter for the first tab, so the page keeps its plain address', () => {
    // The same rule the recordings pager applies to `?page=1`: the default
    // state of a page is the page, not a synonym for it.
    expect(queryForTab({ [TAB_QUERY]: 'queue' }, TABS, 'overview')).toEqual({})
  })

  it('keeps everything else that was in the query', () => {
    // A tab switch that dropped `?guild=…` would send the reader to a
    // different server's panel, which is a data-shaped bug rather than a
    // navigation one.
    expect(queryForTab({ guild: '7', page: '3' }, TABS, 'queue')).toEqual({
      guild: '7',
      page: '3',
      [TAB_QUERY]: 'queue',
    })
  })

  it('does not modify the query it was given', () => {
    const query = { [TAB_QUERY]: 'queue' }
    queryForTab(query, TABS, 'overview')
    expect(query).toEqual({ [TAB_QUERY]: 'queue' })
  })
})

describe('the arrow keys', () => {
  it('walk the bar and wrap round both ends', () => {
    // Deliberately unlike the dropdown, which stops at its ends. A tab bar
    // is short and entirely on screen, so wrapping is visible as it
    // happens and saves a reader four presses; a two-hundred-row list is
    // not, and wrapping there is a silent teleport.
    expect(moveTab(TABS, 'overview', 'ArrowRight')).toBe('queue')
    expect(moveTab(TABS, 'consents', 'ArrowRight')).toBe('overview')
    expect(moveTab(TABS, 'overview', 'ArrowLeft')).toBe('consents')
  })

  it('step over a tab nobody may open', () => {
    expect(moveTab(WITH_HOLE, 'overview', 'ArrowRight')).toBe('consents')
    expect(moveTab(WITH_HOLE, 'consents', 'ArrowLeft')).toBe('overview')
  })

  it('reach the ends with Home and End', () => {
    expect(moveTab(TABS, 'queue', 'Home')).toBe('overview')
    expect(moveTab(TABS, 'queue', 'End')).toBe('consents')
  })

  it('have no opinion about any other key', () => {
    expect(moveTab(TABS, 'queue', 'Enter')).toBeNull()
    expect(moveTab(TABS, 'queue', 'ArrowDown')).toBeNull()
  })

  it('go nowhere in a bar of one', () => {
    const only: UiTab[] = [{ id: 'a', label: 'A' }]
    expect(moveTab(only, 'a', 'ArrowRight')).toBe('a')
  })

  it('go nowhere when there is nothing to walk', () => {
    expect(moveTab([], 'a', 'ArrowRight')).toBeNull()
    expect(moveTab(WITH_HOLE, 'nonsense', 'ArrowRight')).toBe('overview')
  })
})

describe('which panels have been built', () => {
  it('starts with only the one that is showing', () => {
    expect(panelsAfter([], 'overview')).toEqual(['overview'])
  })

  it('adds a panel the first time it is opened', () => {
    expect(panelsAfter(['overview'], 'queue')).toEqual(['overview', 'queue'])
  })

  it('does not rebuild one that has already been opened', () => {
    // Coming back to a tab must not re-run its fetch. A tab bar that
    // reloads on every switch is a reload button with three faces, and it
    // throws away whatever was half typed in the panel.
    const mounted = ['overview', 'queue']
    expect(panelsAfter(mounted, 'overview')).toBe(mounted)
  })

  it('never holds a panel that was never shown', () => {
    // The whole point. Four panels rendered and three hidden is four
    // requests, and the expensive one is the tab nobody clicks.
    expect(panelsAfter([], 'queue')).toEqual(['queue'])
  })
})

describe('the ids the tabs and panels point at', () => {
  it('are built from the position, so nothing depends on a tab id being valid markup', () => {
    expect(tabDomId('admin', 1)).toBe('admin-tab-1')
    expect(panelDomId('admin', 1)).toBe('admin-panel-1')
  })

  it('never collide between the tab and the panel it controls', () => {
    // `aria-controls` on the tab and `aria-labelledby` on the panel point
    // at each other, and one id serving both is a cycle a screen reader
    // reports as a broken relationship.
    expect(tabDomId('admin', 1)).not.toBe(panelDomId('admin', 1))
  })
})
