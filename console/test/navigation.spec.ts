/**
 * Which navigation entries exist, who is offered which, and in which group.
 *
 * The list lives in its own module rather than inside the component for
 * exactly this reason: what belongs in the navigation is a decision, and a
 * decision embedded in a template can only be tested by rendering one.
 */
import { describe, expect, it } from 'vitest'

import {
  ADMIN_VIEW,
  NAV_ENTRIES,
  NAV_SECTIONS,
  USER_VIEW,
  visibleEntries,
  visibleSections,
} from '../app/utils/navigation'

const ADMIN = { is_admin: true }
const PARTICIPANT = { is_admin: false }

describe('the navigation', () => {
  it('separates what a person does with their own recordings from what an administrator does', () => {
    expect(NAV_SECTIONS.map((s) => s.label)).toEqual(['User View', 'Admin View'])
  })

  it('puts the personal sections first', () => {
    // A participant who administers nothing is the common case, and their
    // sections should not be below a heading they never see.
    expect(USER_VIEW.entries.map((e) => e.label)).toEqual([
      'Dashboard',
      'Recordings',
      'Calendar',
    ])
  })

  it('every entry has an accessible name, collapsed or not', () => {
    // Collapsed, the sidebar is icons alone. An icon rail whose entries
    // announce nothing is a rail only its author can navigate.
    for (const entry of NAV_ENTRIES) {
      expect(entry.label.trim()).not.toBe('')
      expect(entry.icon.trim()).not.toBe('')
    }
  })

  it('addresses every entry with a path of its own', () => {
    const paths = NAV_ENTRIES.map((e) => e.to)
    expect(new Set(paths).size).toBe(paths.length)
  })
})

describe('who is offered the Admin View', () => {
  it('marks every entry under it administrative, not just the section', () => {
    // The section flag hides the heading; the entry flags are what
    // `visibleEntries` filters on. A section marked administrative whose
    // entries are not would leak its entries to any caller that flattens
    // first and filters second.
    for (const entry of ADMIN_VIEW.entries) {
      expect(entry.adminOnly).toBe(true)
    }
  })

  it('offers the bot settings to an administrator', () => {
    expect(visibleEntries(ADMIN).map((e) => e.label)).toContain('Bot Settings')
  })

  it('hides it from somebody who administers nothing', () => {
    expect(visibleSections(PARTICIPANT).map((s) => s.label)).toEqual(['User View'])
  })

  it('hides it when nobody is signed in', () => {
    expect(visibleSections(null).map((s) => s.label)).toEqual(['User View'])
  })

  it('never renders a heading over an empty section', () => {
    // A visible "Admin View" with nothing under it would announce the
    // existence of a section to exactly the person who may not have it.
    for (const viewer of [ADMIN, PARTICIPANT, null]) {
      for (const section of visibleSections(viewer)) {
        expect(section.entries.length).toBeGreaterThan(0)
      }
    }
  })

  it('shows every non-administrative section to everyone', () => {
    // Hiding a section is a courtesy to the person looking at the screen,
    // never a control -- so it applies to exactly the entries whose
    // endpoints refuse a non-administrator, and to nothing else.
    const forEveryone = NAV_ENTRIES.filter((e) => !e.adminOnly).map((e) => e.label)
    expect(visibleEntries(PARTICIPANT).map((e) => e.label)).toEqual(forEveryone)
  })

  it('offers an administrator everything a participant is offered, and more', () => {
    const asParticipant = visibleEntries(PARTICIPANT).map((e) => e.label)
    const asAdmin = visibleEntries(ADMIN).map((e) => e.label)
    for (const label of asParticipant) {
      expect(asAdmin).toContain(label)
    }
    expect(asAdmin.length).toBeGreaterThan(asParticipant.length)
  })
})
