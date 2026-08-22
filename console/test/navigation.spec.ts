/**
 * Which navigation entries exist, and who is offered which.
 *
 * The list lives in its own module rather than inside the component for
 * exactly this reason: what belongs in the navigation is a decision, and a
 * decision embedded in a template can only be tested by rendering one.
 */
import { describe, expect, it } from 'vitest'

import { NAV_ENTRIES, visibleEntries } from '../app/utils/navigation'

describe('the navigation', () => {
  it('offers the four sections the console has', () => {
    expect(NAV_ENTRIES.map((e) => e.label)).toEqual([
      'Dashboard',
      'Recordings',
      'Calendar',
      'Settings',
    ])
  })

  it('every entry has an accessible name, collapsed or not', () => {
    // Collapsed, the sidebar is icons alone. An icon rail whose entries
    // announce nothing is a rail only its author can navigate.
    for (const entry of NAV_ENTRIES) {
      expect(entry.label.trim()).not.toBe('')
    }
  })

  it('hides settings from somebody who administers nothing', () => {
    expect(visibleEntries({ is_admin: false }).map((e) => e.label)).not.toContain('Settings')
  })

  it('offers settings to an administrator', () => {
    expect(visibleEntries({ is_admin: true }).map((e) => e.label)).toContain('Settings')
  })

  it('hides settings when nobody is signed in', () => {
    expect(visibleEntries(null).map((e) => e.label)).not.toContain('Settings')
  })

  it('shows every non-administrative section to everyone', () => {
    // Hiding a section is a courtesy to the person looking at the screen,
    // never a control -- so it applies to exactly the one section whose
    // endpoints refuse a non-administrator, and to nothing else.
    const forEveryone = NAV_ENTRIES.filter((e) => !e.adminOnly).map((e) => e.label)
    expect(visibleEntries({ is_admin: false }).map((e) => e.label)).toEqual(forEveryone)
  })
})
