/**
 * Whether the title editor can lose a description.
 *
 * `~/utils/sessionNaming` proves that no *shape* it offers can build a
 * half body. What a module cannot prove is that the component uses it —
 * a form that assembled `{ title }` inline would type-check, read like
 * care, and clear four colleagues' paragraph on every rename. That is the
 * bug this file exists for, and only a mount that watches the request
 * leave can see it.
 *
 * The other half is the interaction rules `RecordingTags` established and
 * this component was asked to match rather than fork: a button disabled
 * rather than removed while it works, a live region that is in the DOM
 * before it has anything to say, and the server's answer replacing what
 * was typed.
 *
 * The locale files are the real ones, for the reason `uiComponents.spec`
 * gives: a template asking for `recordings.nameHeadng` renders the key at
 * somebody, and nothing but a render catches it.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { flushPromises, mount } from '@vue/test-utils'
import { computed, ref, watch } from 'vue'
import { createI18n, useI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import RecordingName from '../app/components/RecordingName.vue'
import { useSay } from '../app/composables/useSay'
import { ApiError } from '../app/utils/apiError'
import type { SessionName } from '../app/utils/sessionNaming'

function load(locale: string) {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

/** Every call the component made, so a test can read the body it sent. */
interface Sent {
  path: string
  body: unknown
}

function render(name: SessionName, answer: (body: unknown) => Promise<SessionName>) {
  const sent: Sent[] = []
  vi.stubGlobal('ref', ref)
  vi.stubGlobal('computed', computed)
  vi.stubGlobal('watch', watch)
  vi.stubGlobal('useId', () => 'name-field')
  vi.stubGlobal('useI18n', () => ({
    ...useI18n(),
    locales: computed(() => [{ code: 'en', language: 'en-GB' }]),
  }))
  vi.stubGlobal('useSay', useSay)
  vi.stubGlobal('useApi', () => (path: string, options: { body?: unknown } = {}) => {
    sent.push({ path, body: options.body })
    return answer(options.body)
  })

  const i18n = createI18n({
    legacy: false,
    locale: 'en',
    fallbackLocale: 'en',
    messages: { en: load('en'), de: load('de') },
  })
  const form = mount(RecordingName, {
    props: { sessionId: '4711', name },
    global: { plugins: [i18n] },
  })
  return { form, sent }
}

const stores = (name: SessionName) => () => Promise.resolve(name)

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('saving a title', () => {
  it('sends the description alongside it, always', () => {
    // The bug. `PUT` replaces, so a body without a description stores
    // null over it — for everybody who was in the meeting, with no
    // history to get it back from.
    const stored: SessionName = { title: 'Sprint 34', description: 'we agreed to split it' }
    const { form, sent } = render(stored, stores({ ...stored, title: 'Sprint 35' }))

    return form
      .get('input[type="text"]')
      .setValue('Sprint 35')
      .then(() => form.get('form').trigger('submit'))
      .then(flushPromises)
      .then(() => {
        expect(sent).toHaveLength(1)
        expect(sent[0]!.path).toBe('/sessions/4711/name')
        expect(sent[0]!.body).toEqual({
          title: 'Sprint 35',
          description: 'we agreed to split it',
        })
      })
  })

  it('sends the title alongside a description that changed', async () => {
    // The same failure the other way round, which is the one a "save only
    // what changed" optimisation would introduce second.
    const stored: SessionName = { title: 'Sprint 34', description: 'old' }
    const { form, sent } = render(stored, stores({ ...stored, description: 'new' }))

    await form.get('textarea').setValue('new')
    await form.get('form').trigger('submit')
    await flushPromises()

    expect(sent[0]!.body).toEqual({ title: 'Sprint 34', description: 'new' })
  })

  it('clears a field on purpose when somebody empties it', async () => {
    // Clearing has to be expressible, or whatever the first person wrote
    // stands for good. `null` and not `''`: one spelling for "nothing".
    const stored: SessionName = { title: 'Sprint 34', description: 'old' }
    const { form, sent } = render(stored, stores({ title: 'Sprint 34', description: null }))

    await form.get('textarea').setValue('   ')
    await form.get('form').trigger('submit')
    await flushPromises()

    expect(sent[0]!.body).toEqual({ title: 'Sprint 34', description: null })
  })

  it('shows what the server stored rather than what was typed', async () => {
    // Normalisation may have changed the text. A form showing its own
    // input back keeps displaying a title the database does not have.
    const { form } = render(
      { title: null, description: null },
      stores({ title: 'Sprint 34 planning', description: null }),
    )

    await form.get('input[type="text"]').setValue('  Sprint\n34   planning ')
    await form.get('form').trigger('submit')
    await flushPromises()

    expect((form.get('input[type="text"]').element as HTMLInputElement).value).toBe(
      'Sprint 34 planning',
    )
  })
})

describe('the control itself', () => {
  it('offers one button for the two fields', () => {
    // Two buttons would be two writes, and either of them would clear the
    // other field. There is no interface in which somebody wants that.
    const { form } = render({ title: 'Sprint 34', description: 'x' }, stores({
      title: 'Sprint 34',
      description: 'x',
    }))
    expect(form.findAll('button')).toHaveLength(1)
  })

  it('is unavailable while nothing has changed', () => {
    // So that an available Save means there is something to save.
    const { form } = render({ title: 'Sprint 34', description: 'x' }, stores({
      title: 'Sprint 34',
      description: 'x',
    }))
    expect(form.get('button').attributes('disabled')).toBeDefined()
  })

  it('stays unavailable for a stray space the keyboard added', async () => {
    const { form } = render({ title: 'Sprint 34', description: 'x' }, stores({
      title: 'Sprint 34',
      description: 'x',
    }))
    await form.get('input[type="text"]').setValue(' Sprint  34 ')
    expect(form.get('button').attributes('disabled')).toBeDefined()
  })

  it('becomes available the moment the text differs', async () => {
    const { form } = render({ title: 'Sprint 34', description: 'x' }, stores({
      title: 'Sprint 35',
      description: 'x',
    }))
    await form.get('input[type="text"]').setValue('Sprint 35')
    expect(form.get('button').attributes('disabled')).toBeUndefined()
  })

  it('says out loud that the write replaces and that it is shared', () => {
    // Both halves are surprises, and finding either of them out
    // afterwards is not a reasonable way to learn it.
    const { form } = render({ title: null, description: null }, stores({
      title: null,
      description: null,
    }))
    const note = form.text()
    expect(note).toContain('Saving writes both fields at once')
    expect(note).toContain('Everybody who was in it reads this')
  })

  it('carries a live region before it has anything to say', () => {
    // A live region added at the moment of the announcement announces
    // nothing.
    const { form } = render({ title: null, description: null }, stores({
      title: null,
      description: null,
    }))
    expect(form.get('[role="status"]').attributes('aria-live')).toBe('polite')
  })

  it('keeps the button on screen and disabled while a save is refused', async () => {
    // Never removed while it works: a control that unmounts itself when
    // pressed drops the keyboard to the top of the document.
    const { form } = render({ title: null, description: null }, () =>
      Promise.reject(new ApiError('/sessions/4711/name', { status: 400 })),
    )
    await form.get('input[type="text"]').setValue('Sprint 34')
    await form.get('form').trigger('submit')
    await flushPromises()

    expect(form.findAll('button')).toHaveLength(1)
    expect(form.get('[role="status"]').text()).toContain('The server refused this text')
  })

  it('renders sentences rather than translation keys', () => {
    const { form } = render({ title: null, description: null }, stores({
      title: null,
      description: null,
    }))
    expect(form.text()).not.toMatch(/recordings\.[a-zA-Z]+/)
  })
})
