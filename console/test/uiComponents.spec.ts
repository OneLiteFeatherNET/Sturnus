/**
 * That the markup keeps the promises the modules made.
 *
 * The decisions are all elsewhere and all tested elsewhere: which row is
 * active, what a key did, which panels have been built, how much of the
 * selection is off the page. What is left for a render to prove is the
 * half a pure module cannot reach — that the ARIA wiring actually points
 * at the elements it claims to, that focus goes where the reducer asked
 * for it to go, that a click outside really does dismiss, and that what
 * the module decided is what appears on screen.
 *
 * The locale files are the real ones, loaded from disk. A control whose
 * template asks for `ui.selct.placeholder` renders the key at somebody,
 * and nothing but a render catches it — which is the same reason
 * `appHeader.spec.ts` mounts with the real translations rather than with a
 * `$t` that echoes.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { mount, type VueWrapper } from '@vue/test-utils'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, useId, watch } from 'vue'
import { createI18n, useI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import UiChipInput from '../app/components/ui/UiChipInput.vue'
import UiCombobox from '../app/components/ui/UiCombobox.vue'
import UiDatePicker from '../app/components/ui/UiDatePicker.vue'
import UiDisclosureList from '../app/components/ui/UiDisclosureList.vue'
import UiPagination from '../app/components/ui/UiPagination.vue'
import UiSelect from '../app/components/ui/UiSelect.vue'
import UiTabs from '../app/components/ui/UiTabs.vue'
import { useSay } from '../app/composables/useSay'
import type { UiOption } from '../app/utils/uiOption'

function load(locale: string) {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

/** The two shapes the calendar renders through. Restated rather than
 *  imported for the reason `message.spec.ts` gives: `i18n.config.ts` is a
 *  Nuxt module wrapped in `defineI18nConfig`, which vitest cannot import. */
const DATETIME = {
  monthYear: { year: 'numeric', month: 'long', timeZone: 'UTC' },
  weekdayShort: { weekday: 'short', timeZone: 'UTC' },
} as const

/** Where a fake router keeps the page. Mutated in place, so a component
 *  watching `route.query` sees the change the way it would in the app. */
const route = reactive({ path: '/dev/ui', query: {} as Record<string, unknown> })

const router = {
  replace(to: { path: string, query: Record<string, unknown> }) {
    route.query = to.query
    return Promise.resolve()
  },
}

/** Nuxt auto-imports these; vitest runs without Nuxt. */
function stubAutoImports() {
  vi.stubGlobal('ref', ref)
  vi.stubGlobal('computed', computed)
  vi.stubGlobal('watch', watch)
  vi.stubGlobal('nextTick', nextTick)
  vi.stubGlobal('onMounted', onMounted)
  vi.stubGlobal('onBeforeUnmount', onBeforeUnmount)
  vi.stubGlobal('useId', useId)
  vi.stubGlobal('useI18n', () => ({
    ...useI18n(),
    locales: computed(() => [{ code: 'en', language: 'en-GB' }]),
  }))
  vi.stubGlobal('useSay', useSay)
  vi.stubGlobal('useRoute', () => route)
  vi.stubGlobal('useRouter', () => router)
}

function render(component: unknown, props: Record<string, unknown> = {}) {
  stubAutoImports()
  const i18n = createI18n({
    legacy: false,
    locale: 'en',
    fallbackLocale: 'en',
    messages: { en: load('en'), de: load('de') },
    datetimeFormats: { 'en': DATETIME, 'en-GB': DATETIME },
  })
  return mount(component as never, {
    // Attached to the document, because half of what a popup has to get
    // right is where focus is, and focus is a property of a document
    // rather than of a detached fragment.
    attachTo: document.body,
    props,
    global: { plugins: [i18n] },
  })
}

/** A press outside the control, in the phase the dismissal listens on. */
function pressElsewhere() {
  document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
}

afterEach(() => {
  vi.unstubAllGlobals()
  route.query = {}
  document.body.innerHTML = ''
})

const GUILDS: UiOption[] = [
  { value: '1', label: 'Alpha', detail: '100000000000000001' },
  { value: '2', label: 'Beta', detail: '100000000000000002' },
  { value: '3', label: 'Gamma', detail: '100000000000000003', disabled: true },
]

/* ==================================================================== */

describe('UiSelect', () => {
  const open = async (select: VueWrapper) => {
    await select.get('button[role="combobox"]').trigger('click')
    return select.get('[role="listbox"]')
  }

  it('says it is a closed dropdown before anything has happened', () => {
    const select = render(UiSelect, { options: GUILDS, label: 'Server' })
    const trigger = select.get('button[role="combobox"]')
    expect(trigger.attributes('aria-expanded')).toBe('false')
    expect(select.find('[role="listbox"]').exists()).toBe(false)
    // The real translation, not the key. A template asking for a key that
    // does not exist renders the key at somebody.
    expect(select.text()).toContain('Choose an option')
  })

  it('renders the row the module chose, subtext and all', async () => {
    const select = render(UiSelect, { options: GUILDS, modelValue: '2' })
    expect(select.text()).toContain('Beta')
    expect(select.text()).toContain('100000000000000002')
    const list = await open(select)
    expect(list.findAll('[role="option"]')).toHaveLength(3)
  })

  it('points aria-activedescendant at the row the module made active', async () => {
    const select = render(UiSelect, { options: GUILDS, modelValue: '2' })
    const list = await open(select)
    const active = select.get('button[role="combobox"]').attributes('aria-activedescendant')
    expect(active).toBe(list.findAll('[role="option"]')[1]!.attributes('id'))
  })

  it('marks the chosen row as selected and the refused row as disabled', async () => {
    const select = render(UiSelect, { options: GUILDS, modelValue: '2' })
    const rows = (await open(select)).findAll('[role="option"]')
    expect(rows[1]!.attributes('aria-selected')).toBe('true')
    expect(rows[2]!.attributes('aria-disabled')).toBe('true')
  })

  it('emits the value a click settled on', async () => {
    const select = render(UiSelect, { options: GUILDS })
    const rows = (await open(select)).findAll('[role="option"]')
    await rows[1]!.trigger('click')
    expect(select.emitted('update:modelValue')).toEqual([['2']])
  })

  it('emits nothing for a row nobody may choose, and stays open', async () => {
    const select = render(UiSelect, { options: GUILDS })
    const rows = (await open(select)).findAll('[role="option"]')
    await rows[2]!.trigger('click')
    expect(select.emitted('update:modelValue')).toBeUndefined()
    expect(select.find('[role="listbox"]').exists()).toBe(true)
  })

  it('puts focus back on the trigger after Escape', async () => {
    const select = render(UiSelect, { options: GUILDS })
    const trigger = select.get('button[role="combobox"]')
    await open(select)
    await trigger.trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(document.activeElement).toBe(trigger.element)
  })

  it('closes when something outside it is pressed', async () => {
    const select = render(UiSelect, { options: GUILDS })
    await open(select)
    pressElsewhere()
    await nextTick()
    expect(select.find('[role="listbox"]').exists()).toBe(false)
  })

  it('says so when there is nothing to choose from', async () => {
    const select = render(UiSelect, { options: [] })
    const list = await open(select)
    expect(list.text()).toContain('nothing to choose from')
    expect(list.findAll('[role="option"]')).toHaveLength(0)
  })

  it('is inert and says so when it is disabled', () => {
    const select = render(UiSelect, { options: GUILDS, disabled: true })
    expect(select.get('button[role="combobox"]').attributes('disabled')).toBeDefined()
  })

  it('carries aria-invalid when the caller has marked it wrong', () => {
    const select = render(UiSelect, { options: GUILDS, invalid: true })
    expect(select.get('button[role="combobox"]').attributes('aria-invalid')).toBe('true')
  })
})

/* ==================================================================== */

describe('UiCombobox', () => {
  const MANY: UiOption[] = [
    { value: '10', label: 'general' },
    { value: '11', label: 'standup' },
    { value: '12', label: 'standup-notes' },
  ]

  const open = async (box: VueWrapper) => {
    await box.get('button[aria-haspopup="listbox"]').trigger('click')
    await nextTick()
    return box.get('input[role="combobox"]')
  }

  it('opens a filter field and moves focus into it', async () => {
    const box = render(UiCombobox, { options: MANY, label: 'Channel' })
    const field = await open(box)
    expect(document.activeElement).toBe(field.element)
  })

  it('shows only what the filter left behind', async () => {
    const box = render(UiCombobox, { options: MANY })
    const field = await open(box)
    await field.setValue('standup')
    expect(box.findAll('[role="option"]')).toHaveLength(2)
  })

  it('says how many are left, where a screen reader will hear it', async () => {
    const box = render(UiCombobox, { options: MANY })
    const field = await open(box)
    await field.setValue('standup')
    expect(box.get('[role="status"]').text()).toBe('2 options match.')
  })

  it('names the query when nothing matches', async () => {
    const box = render(UiCombobox, { options: MANY })
    const field = await open(box)
    await field.setValue('zzz')
    expect(box.text()).toContain('Nothing matches')
    expect(box.text()).toContain('zzz')
  })

  it('emits the row Enter settled on', async () => {
    const box = render(UiCombobox, { options: MANY })
    const field = await open(box)
    await field.setValue('standup-notes')
    await field.trigger('keydown', { key: 'Enter' })
    expect(box.emitted('update:modelValue')).toEqual([['12']])
  })

  it('closes when something outside it is pressed', async () => {
    const box = render(UiCombobox, { options: MANY })
    await open(box)
    pressElsewhere()
    await nextTick()
    expect(box.find('input[role="combobox"]').exists()).toBe(false)
  })
})

/* ==================================================================== */

describe('UiDatePicker', () => {
  it('emits an instant with an offset, never a naive one', async () => {
    const picker = render(UiDatePicker, { label: 'Effective at' })
    await picker.get('input[type="datetime-local"]').setValue('2026-08-23T14:30')
    const emitted = picker.emitted('update:modelValue')!.at(-1)![0] as string
    expect(emitted).toMatch(/[+-]\d{2}:\d{2}$/)
    expect(emitted).not.toContain('Z')
  })

  it('shows the instant it is about to send', async () => {
    const picker = render(UiDatePicker)
    await picker.get('input[type="datetime-local"]').setValue('2026-08-23T14:30')
    expect(picker.text()).toContain('Sent as 2026-08-23T14:30:00')
  })

  it('emits nothing at all rather than a naive string when the field is cleared', async () => {
    const picker = render(UiDatePicker, { modelValue: '2026-08-23T14:30:00+02:00' })
    await picker.get('input[type="datetime-local"]').setValue('')
    expect(picker.emitted('update:modelValue')!.at(-1)).toEqual([null])
  })

  it('opens a grid with a month heading and seven weekday columns', async () => {
    const picker = render(UiDatePicker)
    await picker.get('button[aria-expanded]').trigger('click')
    await nextTick()
    const grid = picker.get('[role="grid"]')
    expect(grid.findAll('th')).toHaveLength(7)
    // `Intl`'s weekday, Monday first — not a table of English words.
    expect(grid.findAll('th')[0]!.text()).toBe('Mon')
    expect(grid.findAll('[role="gridcell"]')).toHaveLength(42)
  })

  it('walks the grid with the arrow keys and commits on Enter', async () => {
    const picker = render(UiDatePicker, { modelValue: '2026-08-23T14:30:00+02:00' })
    await picker.get('button[aria-expanded]').trigger('click')
    await nextTick()
    const body = picker.get('tbody')
    await body.trigger('keydown', { key: 'ArrowRight' })
    await nextTick()
    await body.trigger('keydown', { key: 'Enter' })
    const emitted = picker.emitted('update:modelValue')!.at(-1)![0] as string
    // One day on from whatever wall clock the instant reads as here, with
    // the time kept: clicking a date is not setting a clock.
    expect(emitted.slice(0, 10)).not.toBe('')
    expect(picker.find('[role="grid"]').exists()).toBe(false)
  })

  it('refuses the days outside the bounds it was given', async () => {
    const picker = render(UiDatePicker, {
      modelValue: '2026-08-23T14:30:00+02:00',
      min: '2026-08-10',
      max: '2026-08-20',
    })
    await picker.get('button[aria-expanded]').trigger('click')
    await nextTick()
    const usable = picker
      .get('[role="grid"]')
      .findAll('button')
      .filter((button) => button.attributes('disabled') === undefined)
    expect(usable).toHaveLength(11)
  })
})

/* ==================================================================== */

describe('UiChipInput', () => {
  it('renders a chip apiece and a field for the rest', () => {
    const input = render(UiChipInput, {
      modelValue: { chips: ['standup', 'migration'], text: 'about' },
    })
    expect(input.findAll('button[aria-label^="Remove"]')).toHaveLength(2)
    expect(input.get('input').element.value).toBe('about')
  })

  it('says what is a chip and what is still free text, out loud', () => {
    const input = render(UiChipInput, {
      modelValue: { chips: ['standup', 'migration'], text: 'about' },
    })
    expect(input.get('[role="status"]').text()).toBe(
      '2 tags, and searching for “about”.',
    )
  })

  it('takes a chip away when its button is pressed', async () => {
    const input = render(UiChipInput, { modelValue: { chips: ['standup'], text: '' } })
    await input.get('button[aria-label^="Remove"]').trigger('click')
    expect(input.emitted('update:modelValue')!.at(-1)).toEqual([{ chips: [], text: '' }])
  })

  it('offers suggestions once the field has focus, and adds the one pressed', async () => {
    const input = render(UiChipInput, {
      modelValue: { chips: [], text: 'st' },
      suggestions: ['standup', 'retro'],
    })
    expect(input.find('[role="listbox"]').exists()).toBe(false)
    await input.get('input').trigger('focus')
    const options = input.findAll('[role="option"] button')
    expect(options).toHaveLength(1)
    await options[0]!.trigger('click')
    expect(input.emitted('update:modelValue')!.at(-1)).toEqual([
      { chips: ['standup'], text: '' },
    ])
  })
})

/* ==================================================================== */

describe('UiTabs', () => {
  const TABS = [
    { id: 'overview', label: 'Overview' },
    { id: 'queue', label: 'Queue' },
    { id: 'locked', label: 'Locked', disabled: true },
  ]

  it('wires each tab to its own panel and back again', () => {
    const tabs = render(UiTabs, { tabs: TABS, label: 'Sections' })
    const first = tabs.findAll('[role="tab"]')[0]!
    const panel = tabs.get('[role="tabpanel"]')
    expect(first.attributes('aria-selected')).toBe('true')
    expect(first.attributes('aria-controls')).toBe(panel.attributes('id'))
    expect(panel.attributes('aria-labelledby')).toBe(first.attributes('id'))
  })

  it('builds no panel for a tab nobody has opened', () => {
    const tabs = render(UiTabs, { tabs: TABS })
    // One panel, not three. This is the whole of the lazy mounting: a
    // panel whose data is expensive must not load because it exists.
    expect(tabs.findAll('[role="tabpanel"]')).toHaveLength(1)
  })

  it('writes the tab into the address, so it can be linked and reloaded', async () => {
    const tabs = render(UiTabs, { tabs: TABS })
    await tabs.findAll('[role="tab"]')[1]!.trigger('click')
    expect(route.query).toEqual({ tab: 'queue' })
    await nextTick()
    expect(tabs.findAll('[role="tabpanel"]')).toHaveLength(2)
  })

  it('reads the tab back out of the address', () => {
    route.query = { tab: 'queue' }
    const tabs = render(UiTabs, { tabs: TABS })
    expect(tabs.findAll('[role="tab"]')[1]!.attributes('aria-selected')).toBe('true')
  })

  it('drops the parameter again for the first tab', async () => {
    route.query = { tab: 'queue' }
    const tabs = render(UiTabs, { tabs: TABS })
    await tabs.findAll('[role="tab"]')[0]!.trigger('click')
    expect(route.query).toEqual({})
  })

  it('moves with the arrow keys and steps over the tab nobody may open', async () => {
    route.query = { tab: 'queue' }
    const tabs = render(UiTabs, { tabs: TABS })
    await tabs.get('[role="tablist"]').trigger('keydown', { key: 'ArrowRight' })
    expect(route.query).toEqual({})
  })

  it('keeps only the selected panel visible', async () => {
    const tabs = render(UiTabs, { tabs: TABS })
    await tabs.findAll('[role="tab"]')[1]!.trigger('click')
    await nextTick()
    const panels = tabs.findAll('[role="tabpanel"]')
    expect(panels[0]!.attributes('hidden')).toBeDefined()
    expect(panels[1]!.attributes('hidden')).toBeUndefined()
  })
})

/* ==================================================================== */

describe('UiDisclosureList', () => {
  const ROWS = [{ id: 'a' }, { id: 'b' }, { id: 'c', selectable: false }]

  it('opens a row and points the trigger at what it revealed', async () => {
    const list = render(UiDisclosureList, { rows: ROWS })
    const trigger = list.findAll('button[aria-expanded]')[0]!
    expect(trigger.attributes('aria-expanded')).toBe('false')
    await trigger.trigger('click')
    expect(trigger.attributes('aria-expanded')).toBe('true')
    expect(document.getElementById(trigger.attributes('aria-controls')!)).not.toBeNull()
  })

  it('has no checkboxes at all when it was given no selection', () => {
    const list = render(UiDisclosureList, { rows: ROWS })
    expect(list.findAll('input[type="checkbox"]')).toHaveLength(0)
  })

  it('half-ticks the header box, which is a property no attribute can carry', async () => {
    const list = render(UiDisclosureList, { rows: ROWS, selected: ['a'] })
    const header = list.get('input[type="checkbox"]').element as HTMLInputElement
    await nextTick()
    expect(header.indeterminate).toBe(true)
    expect(header.checked).toBe(false)
  })

  it('fills the header box once every tickable row on the page is ticked', async () => {
    const list = render(UiDisclosureList, { rows: ROWS, selected: ['a', 'b'] })
    const header = list.get('input[type="checkbox"]').element as HTMLInputElement
    await nextTick()
    expect(header.indeterminate).toBe(false)
    expect(header.checked).toBe(true)
  })

  it('says how much of the selection is not on this page', () => {
    const list = render(UiDisclosureList, { rows: ROWS, selected: ['a', 'elsewhere'] })
    expect(list.text()).toContain('2 rows selected, 1 of them not on this page.')
  })

  it('names the action a bulk press would carry out', () => {
    const list = render(UiDisclosureList, {
      rows: ROWS,
      selected: ['a', 'b'],
      bulkAction: 'Erase',
    })
    expect(list.text()).toContain('“Erase” would apply to 2 rows.')
  })

  it('hands the whole selection over when the bulk action is pressed', async () => {
    const list = render(UiDisclosureList, {
      rows: ROWS,
      selected: ['a', 'elsewhere'],
      bulkAction: 'Erase',
    })
    const buttons = list.findAll('button')
    await buttons.find((button) => button.text() === 'Erase')!.trigger('click')
    expect(list.emitted('bulk')).toEqual([[['a', 'elsewhere']]])
  })

  it('refuses to tick a row that may not take part', () => {
    const list = render(UiDisclosureList, { rows: ROWS, selected: [] })
    const boxes = list.findAll('input[type="checkbox"]')
    // The header, then one per row; the last row refuses.
    expect(boxes.at(-1)!.attributes('disabled')).toBeDefined()
  })

  it('says so when there is nothing in it', () => {
    const list = render(UiDisclosureList, { rows: [] })
    expect(list.text()).toContain('nothing in this list')
  })
})

/* ==================================================================== */

describe('UiPagination', () => {
  it('renders the numbers the module chose, gaps included', () => {
    const pager = render(UiPagination, { page: 10, total: 400 })
    const labels = pager.findAll('button').map((button) => button.text())
    expect(labels).toEqual(['Previous', '1', '9', '10', '11', '20', 'Next'])
    expect(pager.text()).toContain('…')
  })

  it('marks the page you are on for a reader who cannot see the border', () => {
    const pager = render(UiPagination, { page: 10, total: 400 })
    const current = pager.findAll('[aria-current="page"]')
    expect(current).toHaveLength(1)
    expect(current[0]!.text()).toBe('10')
  })

  it('says where you are in words as well', () => {
    expect(render(UiPagination, { page: 10, total: 400 }).text()).toContain('Page 10 of 20')
  })

  it('refuses to step off either end', () => {
    const first = render(UiPagination, { page: 1, total: 40 })
    expect(first.findAll('button')[0]!.attributes('disabled')).toBeDefined()
    const last = render(UiPagination, { page: 2, total: 40 })
    expect(last.findAll('button').at(-1)!.attributes('disabled')).toBeDefined()
  })

  it('emits the page that was pressed, and nothing for the page it is on', async () => {
    const pager = render(UiPagination, { page: 10, total: 400 })
    const buttons = pager.findAll('button')
    await buttons.find((button) => button.text() === '11')!.trigger('click')
    await buttons.find((button) => button.text() === '10')!.trigger('click')
    expect(pager.emitted('update:page')).toEqual([[11]])
  })
})
