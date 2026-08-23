/**
 * Whether the shell actually changes language, rather than merely having
 * been wired up to.
 *
 * Every part of this can be correct on its own and wrong together: the
 * German file can be complete, the keys can match, `nuxt.config.ts` can name
 * both locales -- and the header can still render `auth.signOut` at somebody
 * because the template kept a hard-coded string or asked for a key that is
 * spelled slightly differently. Nothing but a render catches that, so this
 * renders.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { mount } from '@vue/test-utils'
import { computed, ref } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import AppHeader from '../app/components/AppHeader.vue'

function load(locale: string) {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

/** Nuxt auto-imports these; vitest runs without Nuxt. */
function stubAutoImports() {
  vi.stubGlobal('ref', ref)
  vi.stubGlobal('computed', computed)
  vi.stubGlobal('useSidebar', () => ({ collapsed: ref(false), toggle: () => {} }))
  vi.stubGlobal('useSession', () => ref({ discord_user_id: '1', is_admin: false }))
  vi.stubGlobal('useApi', () => () => Promise.resolve(null))
}

function renderIn(locale: 'en' | 'de') {
  stubAutoImports()
  const i18n = createI18n({
    legacy: false,
    locale,
    fallbackLocale: 'en',
    messages: { en: load('en'), de: load('de') },
  })
  return mount(AppHeader, {
    global: {
      plugins: [i18n],
      stubs: {
        NuxtLink: { template: '<a><slot /></a>' },
        SturnusMark: { template: '<svg />' },
      },
    },
  })
}

afterEach(() => vi.unstubAllGlobals())

describe('the header', () => {
  it('speaks English by default', () => {
    const header = renderIn('en')
    expect(header.text()).toContain('Sign out')
    expect(header.get('button[aria-controls="sidebar"]').attributes('aria-label')).toBe(
      'Toggle navigation labels',
    )
  })

  it('speaks German to a reader whose locale is German', () => {
    const header = renderIn('de')
    expect(header.text()).toContain('Abmelden')
    expect(header.text()).not.toContain('Sign out')
  })

  it('translates the burger the screen reader hears, not only the words on screen', () => {
    // The control that collapses the navigation has no visible label at
    // all: its entire name is its `aria-label`. A translation that stopped
    // at the text nodes would leave the one person who cannot see the icon
    // with an English name for it.
    const header = renderIn('de')
    expect(header.get('button[aria-controls="sidebar"]').attributes('aria-label')).toBe(
      'Navigationsbeschriftungen umschalten',
    )
  })

  it('leaves the product name alone in both languages', () => {
    // Sturnus is a name. A "translation" of it would be somebody's mistake
    // shipped, and the key exists only so that the wordmark goes through
    // the same path as everything else rather than being the one string
    // nobody thought about.
    for (const locale of ['en', 'de'] as const) {
      expect(renderIn(locale).text()).toContain('Sturnus')
    }
  })

  it('renders no raw key in either language', () => {
    // The failure this is really watching for: a key that is spelled one
    // way in the template and another in the locale file renders as itself,
    // and reads as a label right up until somebody looks at it.
    for (const locale of ['en', 'de'] as const) {
      expect(renderIn(locale).html()).not.toMatch(/\b(?:nav|auth|common|error)\.[a-zA-Z]+\b/)
    }
  })
})
