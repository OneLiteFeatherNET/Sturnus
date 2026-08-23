/**
 * Whether the shell actually changes language, and whether the account menu
 * in the corner behaves like a menu.
 *
 * Every part of the first question can be correct on its own and wrong
 * together: the German file can be complete, the keys can match,
 * `nuxt.config.ts` can name both locales -- and the header can still render
 * `auth.signOut` at somebody because the template kept a hard-coded string or
 * asked for a key that is spelled slightly differently. Nothing but a render
 * catches that, so this renders.
 *
 * The second question is there because a menu is a contract, not a
 * decoration. Closed until it is asked for, honest about its own state in
 * `aria-expanded`, dismissible by the key everybody presses to dismiss
 * things, and visibly inert where it does nothing. Which entries exist and
 * what the initials of a person are is decided in `~/utils/profileMenu` and
 * tested there; what is checked here is that the rendering keeps the
 * promises the markup makes.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { mount, type VueWrapper } from '@vue/test-utils'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import AppHeader from '../app/components/AppHeader.vue'
import ProfileMenu from '../app/components/ProfileMenu.vue'
import type { ConsoleUser } from '../app/composables/useSession'

function load(locale: string) {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

const SOMEBODY: ConsoleUser = { discord_user_id: '1', is_admin: false, display_name: 'Ada Lovelace' }

/** Nuxt auto-imports these; vitest runs without Nuxt. */
function stubAutoImports(user: ConsoleUser | null = SOMEBODY) {
  vi.stubGlobal('ref', ref)
  vi.stubGlobal('computed', computed)
  vi.stubGlobal('nextTick', nextTick)
  vi.stubGlobal('onMounted', onMounted)
  vi.stubGlobal('onBeforeUnmount', onBeforeUnmount)
  vi.stubGlobal('useSidebar', () => ({ collapsed: ref(false), toggle: () => {} }))
  vi.stubGlobal('useSession', () => ref(user))
  vi.stubGlobal('useApi', () => () => Promise.resolve(null))
}

function renderIn(locale: 'en' | 'de', user: ConsoleUser | null = SOMEBODY) {
  stubAutoImports(user)
  const i18n = createI18n({
    legacy: false,
    locale,
    fallbackLocale: 'en',
    messages: { en: load('en'), de: load('de') },
  })
  return mount(AppHeader, {
    // Attached to the document, because half of what a menu has to get
    // right is where focus is, and focus is a property of a document
    // rather than of a detached fragment.
    attachTo: document.body,
    global: {
      plugins: [i18n],
      components: { ProfileMenu },
      stubs: {
        NuxtLink: { template: '<a><slot /></a>' },
        SturnusMark: { template: '<svg />' },
      },
    },
  })
}

const triggerOf = (header: VueWrapper) => header.get('button[aria-haspopup="menu"]')

async function openMenu(header: VueWrapper) {
  await triggerOf(header).trigger('click')
  return header.get('[role="menu"]')
}

afterEach(() => vi.unstubAllGlobals())

describe('the header', () => {
  it('speaks English by default', () => {
    const header = renderIn('en')
    expect(header.get('button[aria-controls="sidebar"]').attributes('aria-label')).toBe(
      'Toggle navigation labels',
    )
  })

  it('speaks German to a reader whose locale is German', async () => {
    const header = renderIn('de')
    const menu = await openMenu(header)
    expect(menu.text()).toContain('Abmelden')
    expect(menu.text()).not.toContain('Sign out')
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

  it('renders no raw key in either language', async () => {
    // The failure this is really watching for: a key that is spelled one
    // way in the template and another in the locale file renders as itself,
    // and reads as a label right up until somebody looks at it.
    for (const locale of ['en', 'de'] as const) {
      const header = renderIn(locale)
      await openMenu(header)
      expect(header.html()).not.toMatch(
        /\b(?:nav|auth|common|error|profile|settings)\.[a-zA-Z]+\b/,
      )
    }
  })
})

describe('the account menu in the corner', () => {
  it('is closed until somebody asks for it', () => {
    // A menu that renders open is a menu that covers the page it was
    // opened over, for everybody, on every navigation.
    const header = renderIn('en')
    expect(header.find('[role="menu"]').exists()).toBe(false)
    expect(triggerOf(header).attributes('aria-expanded')).toBe('false')
  })

  it('says it opens a menu, and says whether it is open', async () => {
    // `aria-expanded` is the entire state of this control for somebody who
    // cannot see that a panel appeared. A trigger that never updates it is
    // a trigger that reads as permanently shut.
    const header = renderIn('en')
    expect(triggerOf(header).attributes('aria-haspopup')).toBe('menu')
    await openMenu(header)
    expect(triggerOf(header).attributes('aria-expanded')).toBe('true')
  })

  it('shows the person their own name and initials rather than a snowflake', () => {
    const header = renderIn('en')
    expect(triggerOf(header).text()).toContain('Ada Lovelace')
    expect(triggerOf(header).text()).toContain('AL')
    expect(header.text()).not.toContain('1')
  })

  it('renders nothing at all for nobody', () => {
    // An anonymous render reaches the header before the sign-in redirect
    // does. An account menu for no account would be a control offering to
    // sign out of nothing.
    const header = renderIn('en', null)
    expect(header.find('button[aria-haspopup="menu"]').exists()).toBe(false)
  })

  it('offers the four entries the design names, once opened', async () => {
    const header = renderIn('en')
    const menu = await openMenu(header)
    const rows = menu.findAll('[role="menuitem"]')
    expect(rows.map((row) => row.text())).toEqual([
      'Settings',
      'Sign out',
      'Two-factor authenticationComing soon',
      'Multi-factor authenticationComing soon',
    ])
  })

  it('renders the two unbuilt entries as inert rows rather than controls', async () => {
    // The point of the whole exercise: they are neither links nor buttons,
    // they say why, and they are marked disabled for a reader who cannot
    // see that they are grey.
    const header = renderIn('en')
    const menu = await openMenu(header)
    const inert = menu.findAll('[aria-disabled="true"]')
    expect(inert).toHaveLength(2)
    for (const row of inert) {
      expect(row.text()).toContain('Coming soon')
      expect(row.element.tagName).toBe('DIV')
      // Out of the tab order, like every row in a menu: focus is moved by
      // the arrow keys, not by Tab.
      expect(row.attributes('tabindex')).toBe('-1')
    }
  })

  it('closes on Escape, and gives focus back to the control that opened it', async () => {
    // A menu that can be opened from the keyboard and not closed from it
    // is a trap, and the person in it has no way of knowing where focus
    // went.
    const header = renderIn('en')
    const menu = await openMenu(header)
    await menu.trigger('keydown', { key: 'Escape' })

    expect(header.find('[role="menu"]').exists()).toBe(false)
    expect(triggerOf(header).attributes('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(triggerOf(header).element)
  })

  it('walks the rows with the arrow keys, the unbuilt ones included', async () => {
    // Skipping the disabled rows would mean the one reader who never sees
    // them is the reader moving by keyboard -- which is the reader the
    // promise was written for.
    const header = renderIn('en')
    const menu = await openMenu(header)
    const rows = menu.findAll('[role="menuitem"]')

    await menu.trigger('keydown', { key: 'ArrowDown' })
    await nextTick()
    expect(document.activeElement).toBe(rows[0]?.element)

    await menu.trigger('keydown', { key: 'End' })
    await nextTick()
    expect(document.activeElement).toBe(rows[3]?.element)
  })

  it('closes when the click lands anywhere else', async () => {
    const header = renderIn('en')
    await openMenu(header)
    document.body.click()
    await nextTick()
    expect(header.find('[role="menu"]').exists()).toBe(false)
  })
})
