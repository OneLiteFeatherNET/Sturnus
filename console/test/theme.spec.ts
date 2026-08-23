/**
 * Which themes can be chosen, what is kept, and what lands on `<html>`.
 *
 * The whole of the theme switch is one string travelling from a cookie to an
 * attribute, and every interesting failure is a mistranslation somewhere on
 * that path: a stored value nobody recognises, a default that overrides the
 * operating system for people who never asked, an attribute that is written
 * for two of the three choices. None of those is visible in a render, and
 * all of them are visible here.
 */
import { describe, expect, it } from 'vitest'

import {
  DEFAULT_THEME,
  THEME_CHOICES,
  THEME_COOKIE,
  isThemeChoice,
  readTheme,
  themeAttribute,
  themeLabelKey,
} from '../app/utils/theme'

describe('the choices on offer', () => {
  it('offers system, light and dark, in that order', () => {
    // `system` first because it is what somebody already has, and the two
    // that override it read as overrides only when they come after it.
    expect(THEME_CHOICES).toEqual(['system', 'light', 'dark'])
  })

  it('defaults to following the operating system', () => {
    // A stored `light` default would take the theme away from everybody
    // whose machine turns dark at dusk and who never asked this console
    // for anything -- the one behaviour nobody wants and everybody
    // notices.
    expect(DEFAULT_THEME).toBe('system')
  })

  it('recognises the three choices and nothing else', () => {
    for (const choice of THEME_CHOICES) expect(isThemeChoice(choice)).toBe(true)
    for (const other of ['', 'System', 'auto', 'DARK', null, undefined, 7, {}]) {
      expect(isThemeChoice(other), `${String(other)} was accepted`).toBe(false)
    }
  })
})

describe('what a stored value means', () => {
  it('keeps a choice somebody actually made', () => {
    expect(readTheme('dark')).toBe('dark')
    expect(readTheme('light')).toBe('light')
    expect(readTheme('system')).toBe('system')
  })

  it('falls back to the system theme for anything it does not recognise', () => {
    // An empty cookie, a value written by a version that offered a fourth
    // theme, a string somebody typed into a storage inspector. Falling
    // back to the default is the only answer that cannot leave the console
    // in a theme nobody selected.
    for (const stored of [null, undefined, '', 'sepia', 'DARK', 42]) {
      expect(readTheme(stored), `${String(stored)} was kept`).toBe('system')
    }
  })
})

describe('what reaches the document', () => {
  it('writes an attribute for every choice, the system one included', () => {
    // Omitting the attribute for `system` would make "no choice" and
    // "chose system" the same state, and would make switching back from
    // dark a *removal* -- the part of head management that quietly does
    // not happen.
    expect(themeAttribute('system')).toBe('system')
    expect(themeAttribute('light')).toBe('light')
    expect(themeAttribute('dark')).toBe('dark')
  })

  it('keeps the cookie on the same shelf as the locale cookie', () => {
    // A later pull request mirrors both into `user_preference`. Two
    // cookies with two naming schemes is a pair somebody mirrors only half
    // of.
    expect(THEME_COOKIE).toBe('sturnus_theme')
  })
})

describe('the labels', () => {
  it('returns a key for every choice rather than a word in one language', () => {
    expect(THEME_CHOICES.map(themeLabelKey)).toEqual([
      'settings.appearance.system',
      'settings.appearance.light',
      'settings.appearance.dark',
    ])
  })
})
