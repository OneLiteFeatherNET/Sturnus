/**
 * What the profile control offers, and to whom it admits that two of the
 * four entries do not work yet.
 *
 * The list lives in a module precisely so that this file can ask those
 * questions without mounting anything -- and so that "the coming-soon rows
 * are inert" is a property somebody can read in a test rather than a
 * property that happens to hold in one template today.
 */
import { describe, expect, it } from 'vitest'

import {
  PROFILE_MENU_ITEMS,
  UNAVAILABLE_ITEMS,
  hasDisplayName,
  initialsFor,
  isActionable,
} from '../app/utils/profileMenu'

describe('the profile menu', () => {
  it('offers exactly the four entries the design names', () => {
    expect(PROFILE_MENU_ITEMS.map((item) => item.id)).toEqual([
      'settings',
      'signOut',
      'twoFactor',
      'multiFactor',
    ])
  })

  it('puts the two working entries first', () => {
    // Not decoration: the two rows that do nothing sit below the two that
    // do something, so the first thing a reader's eye and a screen reader's
    // cursor meet is a control that works.
    const kinds = PROFILE_MENU_ITEMS.map((item) => item.kind)
    expect(kinds).toEqual(['link', 'action', 'unavailable', 'unavailable'])
  })

  it('sends Settings to the address that is now a page again', () => {
    // `/settings` used to be a 301 to the bot's configuration. This entry
    // is the reason that redirect had to go.
    const settings = PROFILE_MENU_ITEMS.find((item) => item.id === 'settings')
    expect(settings?.to).toBe('/settings')
  })

  it('gives no destination and no note to the entries that act', () => {
    // A `to` on the sign-out row would be a row that a browser could
    // open in a new tab, which is not what signing out is.
    const signOut = PROFILE_MENU_ITEMS.find((item) => item.id === 'signOut')
    expect(signOut?.to).toBeUndefined()
    expect(signOut?.noteKey).toBeUndefined()
  })

  it('says of every unavailable entry that it is coming, rather than leaving it silent', () => {
    // The failure this pins: a row rendered as disabled with no note is a
    // control that refuses without a reason, which is worse than not
    // showing it at all.
    expect(UNAVAILABLE_ITEMS.map((item) => item.id)).toEqual(['twoFactor', 'multiFactor'])
    for (const item of UNAVAILABLE_ITEMS) {
      expect(item.noteKey, `${item.id} has no note`).toBe('profile.comingSoon')
      expect(item.to, `${item.id} has a destination`).toBeUndefined()
      expect(isActionable(item), `${item.id} is actionable`).toBe(false)
    }
  })

  it('names every label as a key rather than as a sentence', () => {
    // A sentence here would be a sentence in one language, and this module
    // is the one place where that cannot be noticed by reading the page.
    for (const item of PROFILE_MENU_ITEMS) {
      expect(item.labelKey, `${item.id} has no key`).toMatch(/^[a-z]+\.[a-zA-Z]+$/)
    }
  })
})

describe('the initials in the circle', () => {
  it('takes one letter from the first name and one from the last', () => {
    expect(initialsFor('Ada Lovelace')).toBe('AL')
  })

  it('takes one letter from a name that is one word', () => {
    // `AD` for "Ada" would be an abbreviation of nothing -- initials are
    // one letter per name, and a person with one name has one initial.
    expect(initialsFor('Ada')).toBe('A')
  })

  it('skips the middle of a longer name rather than filling the circle', () => {
    expect(initialsFor('Ada Byron King Lovelace')).toBe('AL')
  })

  it('finds the letters under whatever somebody decorated their name with', () => {
    // Discord display names carry emoji, brackets and clan tags. An
    // initial of "🦜" is a circle nobody can read at twelve pixels.
    expect(initialsFor('🦜 ada lovelace')).toBe('AL')
    expect(initialsFor('[OLF] Ada')).toBe('OA')
  })

  it('upper-cases what it finds, and keeps an initial one character long', () => {
    expect(initialsFor('ada lovelace')).toBe('AL')
    // `ß`.toUpperCase() is `SS`, which would make this circle three
    // characters wide.
    expect(initialsFor('ßeta Gamma')).toBe('SG')
  })

  it('keeps the letters German writes with', () => {
    expect(initialsFor('Ärztin Öhler')).toBe('ÄÖ')
  })

  it('answers with a question mark when there is no name at all', () => {
    // The state this console is actually in until `display_name` reaches
    // `/api/me`, and the state anybody linked before that stays in. A
    // blank circle would read as a control that failed to load.
    expect(initialsFor(null)).toBe('?')
    expect(initialsFor(undefined)).toBe('?')
    expect(initialsFor('')).toBe('?')
    expect(initialsFor('   ')).toBe('?')
    expect(initialsFor('🦜')).toBe('?')
  })
})

describe('whether there is a name to show', () => {
  it('treats a name of nothing but spaces as no name', () => {
    // `display_name` is copied from Outline, and Outline does not police
    // what somebody typed into it.
    expect(hasDisplayName('  ')).toBe(false)
    expect(hasDisplayName(null)).toBe(false)
    expect(hasDisplayName(undefined)).toBe(false)
    expect(hasDisplayName('Ada')).toBe(true)
  })
})
