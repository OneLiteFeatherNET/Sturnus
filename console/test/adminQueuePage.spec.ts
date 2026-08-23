/**
 * What the Queue page does when its live feed ends, and what Refresh can undo.
 *
 * The page's decisions -- which row comes first, what a state reads like as
 * a sentence, which caveat travels with which figure -- live in
 * `~/utils/queue` and are tested there. What is left in the page, and what
 * is tested here, is the one thing no unit of either module can show: *when
 * it opens a feed and when it believes it already has one*.
 *
 * That belief was wrong in exactly one direction. The page kept a single
 * slot for the handle and asked only whether the slot was full, never
 * whether what was in it could still deliver anything. A feed that ended on
 * its own left a finished handle behind, and two of the three ways a feed
 * ends were recovered by accident -- `rest` drives the "nothing is moving"
 * watcher, `polling` starts the fallback loop -- while the third,
 * **`gone`**, was recovered by nothing at all. The queue stayed "moving",
 * the slot stayed full, and the page kept "This queue is no longer
 * readable … Refresh the page" on screen for ever, Refresh included.
 *
 * `EventSource` is stubbed rather than left to the environment, for the
 * same reason it is in `requeuePanel.spec.ts`: which of the two ways of
 * watching runs is the thing under test.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import {
  Suspense,
  computed,
  defineComponent,
  h,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  useId,
  watch,
} from 'vue'
import { createI18n, useI18n } from 'vue-i18n'

import QueueOrderPanel from '../app/components/QueueOrderPanel.vue'
import QueueSessionRow from '../app/components/QueueSessionRow.vue'
import UiPagination from '../app/components/ui/UiPagination.vue'
import { useSay } from '../app/composables/useSay'
import QueuePage from '../app/pages/admin/queue.vue'

/** The real locale files, loaded from disk, for the reason
 *  `uiComponents.spec.ts` loads them: a template asking for
 *  `admin.queue.list.queuedHeadng` renders the key at somebody, and
 *  nothing but a render catches it. */
function load(locale: string) {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

/** One guild, so the page renders its name rather than a switcher, and the
 *  only `<button>` on it is Refresh. */
const GUILDS = { guilds: [{ guild_id: '1', name: 'Alpha' }] }

/** A guild with work in flight, which is the state in which the page
 *  watches rather than merely reads once. */
const MOVING = {
  guild_id: '1',
  counts: { pending: 2, running: 1, done: 4, dead: 0 },
  running_past_lease: 0,
  oldest_pending_session_ended_at: null,
  closed_undocumented: 0,
  lease_seconds: 900,
  truncated: false,
  sessions: [],
}

/** A source the test drives by hand, standing in for the browser's. */
class FakeEventSource {
  static opened: FakeEventSource[] = []
  listeners = new Map<string, ((event: { data?: unknown }) => void)[]>()
  closed = false
  readyState = 1

  constructor(readonly url: string) {
    FakeEventSource.opened.push(this)
  }

  addEventListener(type: string, listener: (event: { data?: unknown }) => void) {
    const existing = this.listeners.get(type) ?? []
    existing.push(listener)
    this.listeners.set(type, existing)
  }

  close() {
    this.closed = true
    this.readyState = 2
  }

  emit(type: string, data?: unknown) {
    for (const listener of this.listeners.get(type) ?? []) listener({ data })
  }
}

function withEventSource() {
  FakeEventSource.opened = []
  vi.stubGlobal('EventSource', FakeEventSource)
  return FakeEventSource
}

/**
 * `useAsyncData`, in as much detail as this page uses it.
 *
 * The real one is Nuxt's, and vitest runs without Nuxt. Written out rather
 * than mocked to return a fixed value, because two of the properties under
 * test are about it: the page assigns straight into `data` when a snapshot
 * arrives on the stream, and it presses `refresh()` from its own Refresh
 * button.
 */
function fakeUseAsyncData() {
  return async (
    _key: string,
    handler: () => Promise<unknown>,
    options?: { watch?: unknown[] },
  ) => {
    const data = ref<unknown>(null)
    const error = ref<unknown>(null)
    const status = ref('idle')

    async function refresh() {
      status.value = 'pending'
      try {
        data.value = await handler()
        error.value = null
        status.value = 'success'
      } catch (thrown) {
        error.value = thrown
        status.value = 'error'
      }
    }

    if (options?.watch) watch(options.watch as never, () => void refresh())
    await refresh()
    return { data, error, status, refresh }
  }
}

/** Nuxt auto-imports these; vitest runs without Nuxt. */
function stubAutoImports(api: (path: string) => Promise<unknown>) {
  vi.stubGlobal('ref', ref)
  vi.stubGlobal('computed', computed)
  vi.stubGlobal('watch', watch)
  vi.stubGlobal('onMounted', onMounted)
  vi.stubGlobal('onBeforeUnmount', onBeforeUnmount)
  vi.stubGlobal('nextTick', nextTick)
  vi.stubGlobal('useId', useId)
  vi.stubGlobal('useI18n', () => ({
    ...useI18n(),
    locales: computed(() => [{ code: 'en', language: 'en-GB' }]),
  }))
  vi.stubGlobal('useSay', useSay)
  vi.stubGlobal('useHead', () => {})
  vi.stubGlobal('useApi', () => api)
  vi.stubGlobal('useAsyncData', fakeUseAsyncData())
  vi.stubGlobal('useRuntimeConfig', () => ({ public: { apiBase: '/api' } }))
}

function servingQueue(queue: () => unknown) {
  return vi.fn((path: string) => Promise.resolve(path === '/guilds' ? GUILDS : queue()))
}

/** The page awaits its data in `setup`, which Vue will only run inside a
 *  `<Suspense>`. Nuxt provides one around every page; here it is written
 *  out, in a render function rather than a template because vitest resolves
 *  `vue` to the build without a runtime compiler. */
const Host = defineComponent({
  setup: () => () => h(Suspense, null, { default: () => h(QueuePage) }),
})

async function openPage(api: ReturnType<typeof servingQueue>) {
  stubAutoImports(api as never)
  const i18n = createI18n({
    legacy: false,
    locale: 'en',
    fallbackLocale: 'en',
    messages: { en: load('en'), de: load('de') },
  })
  const page = mount(Host, {
    global: {
      plugins: [i18n],
      // The three components Nuxt would auto-import. Registered rather
      // than stubbed: which section a row lands in is the thing under
      // test, and a stub would answer that question by not asking it.
      components: { QueueOrderPanel, QueueSessionRow, UiPagination },
      stubs: { NuxtLink: true },
    },
  })
  await flushPromises()
  await flushPromises()
  return page
}

// No `localStorage` is provided, and none is needed: `readSelectedGuild`
// answers `null` for a storage it cannot have -- the same path a private
// window with site data blocked takes -- and one guild is chosen for that
// reason rather than a remembered one.
beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('watching a guild whose queue is moving', () => {
  it('opens one feed, and one only, for the guild on screen', async () => {
    const sources = withEventSource()
    const page = await openPage(servingQueue(() => MOVING))

    expect(sources.opened).toHaveLength(1)
    expect(sources.opened[0]!.url).toBe('/api/guilds/1/queue/stream')
    expect(page.text()).toContain('Connecting to the live feed')
  })

  it('renders what arrives on the feed without being asked', async () => {
    const sources = withEventSource()
    const api = servingQueue(() => MOVING)
    const page = await openPage(api)
    const afterMount = api.mock.calls.length

    sources.opened[0]!.emit(
      'message',
      JSON.stringify({ ...MOVING, counts: { pending: 7, running: 1, done: 4, dead: 0 } }),
    )
    await flushPromises()

    expect(page.text()).toContain('7')
    expect(page.text()).toContain('as it happens')
    // Not one further request. This is the whole change, and counting is
    // the only way to see it.
    expect(api.mock.calls.length).toBe(afterMount)
  })
})

describe('when the feed says the queue stopped being readable', () => {
  it('says so, in the sentence that names what to do next', async () => {
    const sources = withEventSource()
    const page = await openPage(servingQueue(() => MOVING))

    sources.opened[0]!.emit('gone', '{"reason":"no longer readable"}')
    await flushPromises()

    expect(page.text()).toContain('no longer readable')
    expect(sources.opened[0]!.closed).toBe(true)
  })

  it('lets Refresh revive it, rather than leaving that sentence up for ever', async () => {
    // The defect in one test. `gone` is the one ending that changes
    // neither `moving` nor the fallback loop, so nothing released the
    // finished handle -- and Refresh, which is the page's own answer to
    // "this is no longer readable", updated the figures once and then left
    // nothing watching them. The stale sentence stayed on screen beside
    // the fresh numbers, which is worse than either alone.
    const sources = withEventSource()
    const page = await openPage(servingQueue(() => MOVING))

    sources.opened[0]!.emit('gone', '{"reason":"no longer readable"}')
    await flushPromises()

    await page.find('button').trigger('click')
    await flushPromises()

    expect(sources.opened).toHaveLength(2)
    expect(sources.opened[1]!.url).toBe('/api/guilds/1/queue/stream')
    expect(page.text()).not.toContain('no longer readable')

    // And it is a feed the page is actually listening to, rather than a
    // socket it opened and forgot about.
    sources.opened[1]!.emit(
      'message',
      JSON.stringify({ ...MOVING, counts: { pending: 9, running: 1, done: 4, dead: 0 } }),
    )
    await flushPromises()

    expect(page.text()).toContain('9')
    expect(page.text()).toContain('as it happens')
  })
})

describe('when the queue comes to rest', () => {
  it('lets go of the feed rather than holding a socket for finished work', async () => {
    const sources = withEventSource()
    const page = await openPage(servingQueue(() => MOVING))

    sources.opened[0]!.emit(
      'message',
      JSON.stringify({ ...MOVING, counts: { pending: 0, running: 0, done: 7, dead: 0 } }),
    )
    sources.opened[0]!.emit('rest', '{"reason":"at rest"}')
    await flushPromises()

    expect(sources.opened[0]!.closed).toBe(true)
    expect(page.text()).toContain('stopped reading')
  })
})

/**
 * The page's other job, which is where a row is put.
 *
 * A session either has a place in the queue or it does not, and the API
 * says which by sending `priority` present-and-null. Everything the
 * reordering controls can do hangs on that one field being read the right
 * way round, and the failure it prevents is silent: a handle offered on a
 * meeting that is still being recorded is an offer of a control the server
 * refuses, on the one row where a reader is least likely to expect one.
 */
function queueWith(sessions: unknown[]) {
  return {
    ...MOVING,
    // Nothing in flight, so the page stops watching and the test is about
    // the list rather than about a feed.
    counts: { pending: 0, running: 0, done: 4, dead: 0 },
    sessions,
  }
}

function row(id: string, priority: number | null, extra: Record<string, unknown> = {}) {
  return {
    id,
    channel_id: '555',
    channel_name: `room-${id}`,
    started_at: '2026-08-21T12:00:00+00:00',
    ended_at: '2026-08-21T13:00:00+00:00',
    status: 'closed',
    document_url: null,
    counts: { pending: 1, running: 0, done: 0, dead: 0 },
    priority,
    ...extra,
  }
}

describe('which section a session is listed in', () => {
  it('puts a row with a place in the queue where it can be moved', async () => {
    withEventSource()
    const page = await openPage(servingQueue(() => queueWith([row('7', 0)])))

    expect(page.text()).toContain('The order this server’s work will run in')
    // One handle, for the one row that has something to move.
    expect(page.findAll('[aria-describedby]')).toHaveLength(1)
    expect(page.text()).toContain('Runs 1 of 1')
  })

  it('gives a row with no place no handle at all, and says why', async () => {
    withEventSource()
    const page = await openPage(
      servingQueue(() =>
        queueWith([
          row('9', null, {
            status: 'open',
            ended_at: null,
            counts: { pending: 0, running: 0, done: 0, dead: 0 },
          }),
        ]),
      ),
    )

    expect(page.text()).toContain('Listed, but not in the queue')
    // Never a disabled handle. A control that cannot exist is not offered
    // greyed out; the section says in prose that there is nothing to move.
    expect(page.findAll('[aria-describedby]')).toHaveLength(0)
    expect(page.text()).toContain('no handle to move them by')
  })

  it('keeps the ids secondary when a channel has lost its name', async () => {
    // #141: an unresolved channel reads as an absence, not as a name.
    // Eighteen digits in the heading slot read as what the meeting is
    // called, and nobody has a meeting called `Channel 1129384756123456789`.
    withEventSource()
    const page = await openPage(
      servingQueue(() =>
        queueWith([row('7', 0, { channel_name: null, channel_id: '1129384756123456789' })]),
      ),
    )

    const heading = page.find('article h3')
    expect(heading.text()).toBe('Unnamed channel')
    expect(heading.classes()).toContain('italic')
    expect(page.text()).toContain('1129384756123456789')
    expect(page.text()).toContain('Sturnus has no name for this channel')
  })
})
