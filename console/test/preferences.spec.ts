/**
 * The per-browser preferences, and what happens when the browser refuses
 * to keep them.
 */
import { describe, expect, it } from 'vitest'

import {
  SIDEBAR_COLLAPSED_KEY,
  readCollapsed,
  writeCollapsed,
  type KeyValueStore,
} from '../app/utils/preferences'

function memoryStore(initial: Record<string, string> = {}): KeyValueStore {
  const data = { ...initial }
  return {
    getItem: (key) => (key in data ? data[key]! : null),
    setItem: (key, value) => {
      data[key] = value
    },
  }
}

const throwingStore: KeyValueStore = {
  getItem() {
    throw new Error('site data is blocked')
  },
  setItem() {
    throw new Error('site data is blocked')
  },
}

describe('reading the sidebar preference', () => {
  it('reads a stored collapse', () => {
    expect(readCollapsed(memoryStore({ [SIDEBAR_COLLAPSED_KEY]: '1' }))).toBe(true)
  })

  it('defaults to expanded when nothing was ever stored', () => {
    // Expanded is the mode that says what every entry means, which is the
    // right thing to show somebody who has expressed no preference.
    expect(readCollapsed(memoryStore())).toBe(false)
  })

  it('defaults to expanded for a value it does not recognise', () => {
    // An older version, a hand-edited value, a half-written one. None of
    // them should decide how the console looks.
    expect(readCollapsed(memoryStore({ [SIDEBAR_COLLAPSED_KEY]: 'yes' }))).toBe(false)
  })

  it('defaults to expanded when the storage backend throws', () => {
    // A private window with site data blocked throws on access rather than
    // returning null.
    expect(readCollapsed(throwingStore)).toBe(false)
  })

  it('defaults to expanded when there is no storage at all', () => {
    // Server-side render: there is no browser to have a preference.
    expect(readCollapsed(null)).toBe(false)
    expect(readCollapsed(undefined)).toBe(false)
  })
})

describe('writing the sidebar preference', () => {
  it('stores the collapse and reports that it did', () => {
    const store = memoryStore()
    expect(writeCollapsed(store, true)).toBe(true)
    expect(readCollapsed(store)).toBe(true)
  })

  it('stores an expansion as an explicit value rather than an absence', () => {
    // Deleting the key would be indistinguishable from never having
    // chosen, and somebody who deliberately expanded the sidebar has
    // chosen.
    const store = memoryStore({ [SIDEBAR_COLLAPSED_KEY]: '1' })
    writeCollapsed(store, false)
    expect(store.getItem(SIDEBAR_COLLAPSED_KEY)).toBe('0')
  })

  it('never throws when the backend refuses, and says it did not store', () => {
    // A preference that could not be saved is still a preference that
    // applies for this visit. The caller may care whether it will survive
    // a reload; it must never be an error.
    expect(() => writeCollapsed(throwingStore, true)).not.toThrow()
    expect(writeCollapsed(throwingStore, true)).toBe(false)
  })

  it('reports honestly when there is no storage', () => {
    expect(writeCollapsed(null, true)).toBe(false)
  })
})
