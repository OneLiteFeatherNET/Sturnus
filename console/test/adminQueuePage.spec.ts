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
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { Suspense, computed, defineComponent, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import QueuePage from '../app/pages/admin/queue.vue'

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
  const page = mount(Host, { global: { stubs: { NuxtLink: true } } })
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
