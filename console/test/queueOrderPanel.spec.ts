/**
 * What the reordering controls do to a real queue, once there are
 * elements.
 *
 * The arithmetic is `~/utils/queueReorder` and the wording is
 * `~/utils/queueOrder`, both tested without a render. What is left for a
 * mount to prove is the half neither module can reach, and it is the half
 * that decides whether this is a control at all:
 *
 * - **A session can be moved with a keyboard, and the move that lands is
 *   the same one a drag would have sent.** A reorder reachable only by
 *   dragging is not a reorder for anybody who does not drag, and one
 *   reachable by arrow keys nobody mentions is barely better — so the
 *   handle's own description is asserted here too.
 * - **One request per gesture.** Four arrow presses are one write, not
 *   four, and the three intermediate orders are ones nobody asked the
 *   queue to be in.
 * - **A 409 reads as somebody else having moved something**, is not
 *   spoken of as a failure, and redraws the list from the order it
 *   carries rather than replaying a drag it has just been told is stale.
 * - **A quick action says what it will do before it does it**, and does
 *   nothing at all until it is confirmed.
 *
 * The locale files are the real ones, loaded from disk, for the reason
 * `uiComponents.spec.ts` loads them: a template asking for a key that does
 * not exist renders the key at somebody, and nothing but a render catches
 * that.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  useId,
  watch,
} from 'vue'
import { createI18n, useI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import QueueOrderPanel from '../app/components/QueueOrderPanel.vue'
import QueueSessionRow from '../app/components/QueueSessionRow.vue'
import UiPagination from '../app/components/ui/UiPagination.vue'
import { useSay } from '../app/composables/useSay'
import type { QueuedSession } from '../app/utils/queue'

function load(locale: string) {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

function session(id: string, priority: number): QueuedSession {
  return {
    id,
    channel_id: `9${id}`,
    channel_name: `room-${id}`,
    started_at: '2026-08-21T12:00:00+00:00',
    ended_at: '2026-08-21T13:00:00+00:00',
    status: 'closed',
    document_url: null,
    counts: { pending: 1, running: 0, done: 0, dead: 0 },
    priority,
  }
}

const QUEUE = [session('1', 0), session('2', 1), session('3', 2), session('4', 3)]

/** What the API answered, and what it was asked. */
interface Call {
  path: string
  body: Record<string, string>
}

function order(accepted: boolean, changed: string[]) {
  return {
    accepted,
    refusal: accepted ? null : 'the queue moved',
    changed,
    order: QUEUE.map((row, index) => ({ session_id: row.id, priority: index })),
  }
}

/** Nuxt auto-imports these; vitest runs without Nuxt. */
function stubAutoImports(api: unknown) {
  vi.stubGlobal('ref', ref)
  vi.stubGlobal('computed', computed)
  vi.stubGlobal('watch', watch)
  vi.stubGlobal('nextTick', nextTick)
  vi.stubGlobal('onMounted', onMounted)
  vi.stubGlobal('onBeforeUnmount', onBeforeUnmount)
  vi.stubGlobal('useId', useId)
  vi.stubGlobal('useApi', () => api)
  vi.stubGlobal('useSay', useSay)
  vi.stubGlobal('useI18n', () => ({
    ...useI18n(),
    locales: computed(() => [{ code: 'en', language: 'en-GB' }]),
  }))
}

function open(answer: (call: Call) => unknown, sessions: QueuedSession[] = QUEUE) {
  const calls: Call[] = []
  const api = vi.fn((path: string, options: Record<string, unknown>) => {
    const call = { path, body: options.body as Record<string, string> }
    calls.push(call)
    const reply = answer(call)
    // The real `useApi` hands `ofetch` an `onResponse`, which is how a 409
    // reaches the page with its body intact. A reply carrying a status
    // says so the same way here.
    const status
      = typeof reply === 'object' && reply !== null && 'accepted' in reply
        ? (reply as { accepted: boolean }).accepted
          ? 200
          : 409
        : 404
    ;(options.onResponse as ((context: unknown) => void) | undefined)?.({ response: { status } })
    return Promise.resolve(reply)
  })
  stubAutoImports(api)
  const i18n = createI18n({
    legacy: false,
    locale: 'en',
    fallbackLocale: 'en',
    messages: { en: load('en'), de: load('de') },
  })
  const panel = mount(QueueOrderPanel, {
    props: { guildId: '4711', sessions },
    global: {
      plugins: [i18n],
      components: { QueueSessionRow, UiPagination },
      stubs: { NuxtLink: true },
    },
    attachTo: document.body,
  })
  return { panel, calls, api }
}

function handles(panel: VueWrapper) {
  return panel.findAll('ol button')
}

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('moving a session with a keyboard', () => {
  it('says on the page what the keys do, rather than leaving it to be found', async () => {
    const { panel } = open(() => order(true, ['1']))
    expect(panel.text()).toContain('Press Enter or Space to pick a session up')
    // And the handle points at that sentence, so it is read out when the
    // handle is focused rather than only seen.
    const described = handles(panel)[0]!.attributes('aria-describedby')
    expect(described).toBeTruthy()
    expect(panel.find(`#${described}`).text()).toContain('Home to send it to the front')
  })

  it('picks a row up, says where it is, and puts it down as one request', async () => {
    const { panel, calls } = open(() => order(true, ['1']))
    const handle = handles(panel)[0]!

    await handle.trigger('keydown', { key: 'Enter' })
    expect(panel.text()).toContain('#room-1 picked up, position 1 of 4')
    expect(handle.attributes('aria-pressed')).toBe('true')

    await handle.trigger('keydown', { key: 'ArrowDown' })
    await handle.trigger('keydown', { key: 'ArrowDown' })
    // Still nothing written. Four arrow presses would otherwise be four
    // writes, and the orders in between are ones nobody asked for.
    expect(calls).toHaveLength(0)
    expect(panel.text()).toContain('Position 3 of 4')

    await handle.trigger('keydown', { key: 'Enter' })
    await flushPromises()

    expect(calls).toHaveLength(1)
    expect(calls[0]).toEqual({
      path: '/sessions/1/queue/priority',
      // Relative to a neighbour, never an index: an index is an absolute
      // claim about a list the browser was showing a moment ago.
      body: { place: 'after', session: '3' },
    })
  })

  it('sends a row to the front and to the back, which a page-bound drag cannot', async () => {
    const { panel, calls } = open(() => order(true, ['4']))
    const handle = handles(panel)[3]!

    await handle.trigger('keydown', { key: 'Enter' })
    await handle.trigger('keydown', { key: 'Home' })
    await handle.trigger('keydown', { key: 'Enter' })
    await flushPromises()

    // The ends are named as ends, because they stay true if the queue
    // changes between the drop and the write.
    expect(calls[0]!.body).toEqual({ place: 'first' })
  })

  it('puts a row back where it was, and asks for nothing', async () => {
    const { panel, calls } = open(() => order(true, []))
    const handle = handles(panel)[1]!

    await handle.trigger('keydown', { key: 'Enter' })
    await handle.trigger('keydown', { key: 'ArrowDown' })
    await handle.trigger('keydown', { key: 'Escape' })
    await flushPromises()

    expect(calls).toHaveLength(0)
    expect(panel.text()).toContain('#room-2 was put back where it was')
  })

  it('asks for nothing when the row was dropped where it started', async () => {
    // The server would answer `changed: []` and the page would have to
    // word "nothing to do" after the fact. Not asking is truer and cheaper.
    const { panel, calls } = open(() => order(true, []))
    const handle = handles(panel)[1]!

    await handle.trigger('keydown', { key: 'Enter' })
    await handle.trigger('keydown', { key: 'ArrowUp' })
    await handle.trigger('keydown', { key: 'ArrowDown' })
    await handle.trigger('keydown', { key: 'Enter' })
    await flushPromises()

    expect(calls).toHaveLength(0)
  })
})

describe('when the queue moved under a drag', () => {
  it('says somebody else moved something, and does not call it a failure', async () => {
    const { panel } = open(() => order(false, []))
    const handle = handles(panel)[0]!

    await handle.trigger('keydown', { key: 'Enter' })
    await handle.trigger('keydown', { key: 'ArrowDown' })
    await handle.trigger('keydown', { key: 'Enter' })
    await flushPromises()

    expect(panel.text()).toContain('Somebody else moved something in this queue')
    expect(panel.text()).toContain('nothing was written')
    // Never the word for a fault. This is the endpoint working.
    expect(panel.text()).not.toContain('failed')
    // And the list below is offered as the real order, which is what makes
    // trying again a sensible act rather than a guess.
    expect(panel.text()).toContain('the order as it now stands')
  })

  it('hands the order it carried to the page, refusal included', async () => {
    const { panel } = open(() => order(false, []))
    const handle = handles(panel)[0]!

    await handle.trigger('keydown', { key: 'Enter' })
    await handle.trigger('keydown', { key: 'ArrowDown' })
    await handle.trigger('keydown', { key: 'Enter' })
    await flushPromises()

    // A 409's body is the queue as it now stands, which is exactly what a
    // page whose picture has just gone stale needs.
    expect(panel.emitted('order')).toHaveLength(1)
    expect(panel.emitted('reload')).toHaveLength(1)
  })
})

describe('when the API refuses outright', () => {
  it('says which refusal it was, from the status alone', async () => {
    const { panel } = open(() => ({ error: 'no such session' }))
    const handle = handles(panel)[0]!

    await handle.trigger('keydown', { key: 'Enter' })
    await handle.trigger('keydown', { key: 'ArrowDown' })
    await handle.trigger('keydown', { key: 'Enter' })
    await flushPromises()

    expect(panel.text()).toContain('no longer administer this server')
    // Nothing was renumbered from a reply that carried no order.
    expect(panel.emitted('order')).toBeUndefined()
  })
})

describe('the quick actions', () => {
  it('does nothing at all until it has said what it will do', async () => {
    const { panel, calls } = open(() => order(true, ['1', '2']))

    const button = panel
      .findAll('button')
      .find((candidate) => candidate.text() === 'Shortest recordings first')!
    await button.trigger('click')

    expect(calls).toHaveLength(0)
    expect(panel.text()).toContain('Run the shortest recordings first?')
    // The three things a button cannot show.
    expect(panel.text()).toContain('including any the list above was cut short of showing')
    expect(panel.text()).toContain('Nothing is moved forward')
    expect(panel.text()).toContain('does not undo this')
    // And the caveat that decides whether this rule is worth choosing at
    // all: null is not zero, so an unmeasured recording ranks last.
    expect(panel.text()).toContain('has no length to rank by')
    expect(panel.text()).toContain('on a queue of fresh recordings this changes very little')
  })

  it('names the rule as one of the API’s own literals once confirmed', async () => {
    const { panel, calls } = open(() => order(true, ['1', '2']))

    await panel
      .findAll('button')
      .find((candidate) => candidate.text() === 'Shortest recordings first')!
      .trigger('click')
    await panel
      .findAll('button')
      .find((candidate) => candidate.text() === 'Reorder by length')!
      .trigger('click')
    await flushPromises()

    expect(calls).toEqual([
      { path: '/guilds/4711/queue/priority', body: { rule: 'short-recordings-first' } },
    ])
    expect(panel.text()).toContain('sessions changed place')
    // Said out loud, because it is the property that makes a reorder look
    // broken to somebody who does not know it.
    expect(panel.text()).toContain('rows nobody touched carry new numbers')
  })

  it('leaves the order alone when the confirmation is dismissed', async () => {
    const { panel, calls } = open(() => order(true, ['1']))

    await panel
      .findAll('button')
      .find((candidate) => candidate.text() === 'Biggest meetings first')!
      .trigger('click')
    await panel
      .findAll('button')
      .find((candidate) => candidate.text() === 'Leave the order alone')!
      .trigger('click')
    await flushPromises()

    expect(calls).toHaveLength(0)
    expect(panel.text()).not.toContain('Run the biggest meetings first?')
  })
})

describe('a queue longer than one page', () => {
  const LONG = Array.from({ length: 12 }, (_, index) => session(String(index + 1), index))

  it('shows a window on it and says which window', async () => {
    const { panel } = open(() => order(true, []), LONG)

    expect(handles(panel).length).toBeLessThan(LONG.length)
    expect(panel.text()).toContain('Showing 1–5 of 12')
    // The position is the row's place in the whole queue, not in the page
    // of it on screen: a row that says "runs 2nd" on page three has told
    // the reader nothing.
    expect(panel.text()).toContain('Runs 1 of 12')
  })

  it('follows a held row across a page boundary rather than stopping at it', async () => {
    // Otherwise "move this to the front" is the one move the keyboard
    // cannot make, for a reason that is about pagination and nothing to do
    // with the queue.
    const { panel, calls } = open(() => order(true, ['12']), LONG)
    await panel.findAll('nav button').find((button) => button.text() === '3')!.trigger('click')
    await nextTick()

    const handle = handles(panel)[1]!
    await handle.trigger('keydown', { key: 'Enter' })
    await handle.trigger('keydown', { key: 'Home' })
    expect(panel.text()).toContain('Position 1 of 12')
    expect(panel.text()).toContain('Showing 1–5 of 12')

    await handle.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(calls[0]!.body).toEqual({ place: 'first' })
  })
})
