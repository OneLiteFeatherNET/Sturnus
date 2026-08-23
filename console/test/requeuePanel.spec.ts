/**
 * Who the Transcription panel reveals itself to, and how it watches a redo.
 *
 * Every property here was, or could be, broken in a way that a passing
 * build cannot show. The panel decides its own visibility from an HTTP
 * status; it holds a connection and a timer that outlive nothing but their
 * own component; and it has two ways of watching, only one of which will
 * work through a given administrator's proxy.
 *
 * `EventSource` is stubbed in every test rather than left to the
 * environment, and deliberately: which of the two paths runs is the thing
 * under test in half of this file, and a test that let the DOM
 * implementation decide would pass or fail on a dependency bump.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { ApiError } from '../app/utils/apiError'
import RequeuePanel from '../app/components/RequeuePanel.vue'

/** Nuxt auto-imports these; vitest runs without Nuxt. */
function stubAutoImports(api: (path: string, options?: unknown) => Promise<unknown>) {
  vi.stubGlobal('ref', ref)
  vi.stubGlobal('computed', computed)
  vi.stubGlobal('onMounted', onMounted)
  vi.stubGlobal('onBeforeUnmount', onBeforeUnmount)
  vi.stubGlobal('useApi', () => api)
  vi.stubGlobal('useRuntimeConfig', () => ({ public: { apiBase: '/api' } }))
}

/** A browser with no `EventSource`, which is what a server render and an
 *  old browser both look like — and the state in which the panel must fall
 *  back to its timer. */
function withoutEventSource() {
  vi.stubGlobal('EventSource', undefined)
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

function failWith(status: number) {
  return () => Promise.reject(new ApiError('/queue', { status, message: 'x' }))
}

const SNAPSHOT = {
  session_status: 'documented',
  document_url: null,
  speakers: [],
  can_requeue: true,
  refusal: null,
}

/** A session with a speaker a worker still has in hand — the state in
 *  which the panel watches rather than merely reads once. */
const BUSY = {
  ...SNAPSHOT,
  session_status: 'closed',
  speakers: [
    { discord_user_id: '2', display_name: 'Anna', status: 'running', attempts: 1, error: null },
  ],
}

/** The same session once the redo has finished: nothing left to watch,
 *  and a redo that may be asked for again. */
const FINISHED = {
  ...SNAPSHOT,
  speakers: [
    { discord_user_id: '2', display_name: 'Anna', status: 'done', attempts: 1, error: null },
  ],
}

const ACCEPTED = { accepted: true, requeued: ['2'], skipped_erased: [], refusal: null }

/** An API that answers the re-queue POST and the status read separately,
 *  with whichever snapshot the test is up to. */
function apiServing(snapshot: () => unknown) {
  return vi.fn((_path: string, options?: { method?: string }) =>
    Promise.resolve(options?.method === 'POST' ? ACCEPTED : snapshot()),
  )
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('who the panel shows itself to', () => {
  beforeEach(withoutEventSource)

  it('stays hidden when the queue cannot be read for any reason other than 404', async () => {
    // The bug this replaces: `visible` was set to true on every failure
    // that was not a 404, so one 500 revealed the Transcription section --
    // its heading, its explanatory text, the fact that the endpoint
    // exists at all -- to somebody who does not administer the guild.
    stubAutoImports(failWith(500))
    const panel = mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()

    expect(panel.text()).not.toContain('Transcription')
    // Nothing rendered at all -- not a disabled control, which would
    // itself be an answer to "does this session have a queue".
    expect(panel.find('section').exists()).toBe(false)
  })

  it('stays hidden on a 404, which is what a non-administrator gets', async () => {
    stubAutoImports(failWith(404))
    const panel = mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()
    expect(panel.text()).not.toContain('Transcription')
  })

  it('appears only after a 200, the one answer that proves administration', async () => {
    stubAutoImports(() => Promise.resolve(SNAPSHOT))
    const panel = mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()
    expect(panel.text()).toContain('Transcription')
  })
})

describe('when there is no live feed to be had', () => {
  beforeEach(withoutEventSource)

  it('falls back to the timer, so the redo is still watched', async () => {
    // The fallback is not optional. An event stream is the one response an
    // intermediary can break without breaking anything else, and somebody
    // who has just pressed "Transcribe again" behind such a proxy must
    // still watch it happen.
    const api = vi.fn().mockResolvedValue(BUSY)
    stubAutoImports(api as never)

    mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()
    const afterMount = api.mock.calls.length

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()

    expect(api.mock.calls.length).toBeGreaterThan(afterMount)
  })

  it('says which of the two ways it is watching', async () => {
    // "Live" and "checking every few seconds" look identical whenever the
    // speakers happen not to be changing state, and one of them is several
    // seconds behind.
    stubAutoImports(vi.fn().mockResolvedValue(BUSY) as never)

    const panel = mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()

    expect(panel.text()).toContain('checking every few seconds')
  })

  it('makes no further request after it is unmounted mid-poll', async () => {
    // `clearTimeout` cannot stop a timer that has already fired, and the
    // continuation after its `await` installed a fresh one. Navigating
    // away during a poll left twenty database reads a minute running for
    // the life of the tab.
    const api = vi.fn().mockResolvedValue(BUSY)
    stubAutoImports(api as never)

    const panel = mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()

    const beforeUnmount = api.mock.calls.length
    panel.unmount()
    await vi.advanceTimersByTimeAsync(30_000)
    await flushPromises()

    expect(api.mock.calls.length).toBe(beforeUnmount)
  })
})

describe('when the live feed works', () => {
  it('watches the stream instead of asking again and again', async () => {
    const sources = withEventSource()
    const api = vi.fn().mockResolvedValue(BUSY)
    stubAutoImports(api as never)

    mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()

    expect(sources.opened).toHaveLength(1)
    expect(sources.opened[0]!.url).toBe('/api/sessions/1/queue/stream')

    const afterMount = api.mock.calls.length
    // The server sends a snapshot the moment a stream connects, which is
    // what proves the path works and what keeps the panel from deciding,
    // eight seconds later, that it is talking to a proxy.
    sources.opened[0]!.emit('message', JSON.stringify(BUSY))
    await vi.advanceTimersByTimeAsync(30_000)
    await flushPromises()

    // Not one further request in thirty seconds, where the timer would
    // have made ten. This is the whole change, and counting is the only
    // way to see it.
    expect(api.mock.calls.length).toBe(afterMount)
  })

  it('renders what arrives on the stream without being asked', async () => {
    const sources = withEventSource()
    stubAutoImports(vi.fn().mockResolvedValue(BUSY) as never)

    const panel = mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()
    expect(panel.text()).toContain('transcribing')

    sources.opened[0]!.emit(
      'message',
      JSON.stringify({
        ...BUSY,
        speakers: [
          {
            discord_user_id: '2',
            display_name: 'Anna',
            status: 'running',
            attempts: 2,
            error: null,
          },
        ],
      }),
    )
    await flushPromises()

    expect(panel.text()).toContain('2 attempts')
    expect(panel.text()).toContain('watching live')
  })

  it('stops calling itself a watcher once the last speaker has finished', async () => {
    const sources = withEventSource()
    stubAutoImports(vi.fn().mockResolvedValue(BUSY) as never)

    const panel = mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()

    sources.opened[0]!.emit(
      'message',
      JSON.stringify({
        ...BUSY,
        session_status: 'documented',
        speakers: [
          { discord_user_id: '2', display_name: 'Anna', status: 'done', attempts: 1, error: null },
        ],
      }),
    )
    await flushPromises()

    expect(panel.text()).toContain('finished')
    // "Watching" beside a finished queue reads as a fault rather than as
    // the end of one.
    expect(panel.text()).not.toContain('watching live')
  })

  it('keeps the last good snapshot when a frame is unreadable', async () => {
    // An event arrives in a listener with no caller to throw at, so the
    // first sign of a malformed frame would otherwise be a render failing
    // inside `speakers.length`.
    const sources = withEventSource()
    stubAutoImports(vi.fn().mockResolvedValue(BUSY) as never)

    const panel = mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()

    sources.opened[0]!.emit('message', 'not json at all')
    await flushPromises()

    expect(panel.text()).toContain('transcribing')
  })

  it('closes the connection when the panel goes', async () => {
    // The same defect as the timer's, in another costume: something that
    // outlives its component and keeps reading for the life of the tab.
    const sources = withEventSource()
    stubAutoImports(vi.fn().mockResolvedValue(BUSY) as never)

    const panel = mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()
    panel.unmount()

    expect(sources.opened[0]!.closed).toBe(true)
  })

  it('opens nothing for a session whose work has already finished', async () => {
    // Nothing pending and nothing running. A connection held open for a
    // finished queue is the polling problem with an extra socket.
    const sources = withEventSource()
    stubAutoImports(vi.fn().mockResolvedValue(SNAPSHOT) as never)

    mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()

    expect(sources.opened).toHaveLength(0)
  })

  it('goes back to the timer once the feed has failed often enough', async () => {
    const sources = withEventSource()
    const api = vi.fn().mockResolvedValue(BUSY)
    stubAutoImports(api as never)

    const panel = mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()
    const afterMount = api.mock.calls.length

    const source = sources.opened[0]!
    source.emit('error')
    source.emit('error')
    source.emit('error')
    await flushPromises()

    expect(source.closed).toBe(true)
    expect(panel.text()).toContain('checking every few seconds')

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()

    expect(api.mock.calls.length).toBeGreaterThan(afterMount)
  })
})

describe('pressing "Transcribe again" a second time in one page visit', () => {
  it('watches the second redo exactly as closely as the first', async () => {
    // The failure this panel exists to prevent, committed by the panel
    // itself. `rest` is the *ordinary* end of a stream -- the server says
    // the queue has come to rest and hangs up -- and nothing then cleared
    // the one slot the panel checks before opening a feed. So the second
    // press found a finished handle sitting there, decided it was already
    // watching, and opened neither a stream nor a timer: the speakers read
    // `queued` until somebody refreshed the page by hand.
    const sources = withEventSource()
    let served: unknown = BUSY
    const api = apiServing(() => served)
    stubAutoImports(api as never)

    const panel = mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()
    expect(sources.opened).toHaveLength(1)

    // The redo finishes. The last snapshot arrives on the stream, and the
    // server hangs up with the terminal event that stops the browser
    // reconnecting.
    served = FINISHED
    sources.opened[0]!.emit('message', JSON.stringify(FINISHED))
    sources.opened[0]!.emit('rest', '{"reason":"at rest"}')
    await flushPromises()
    expect(sources.opened[0]!.closed).toBe(true)

    // Press it again.
    served = BUSY
    await panel.find('button').trigger('click')
    await flushPromises()

    expect(sources.opened).toHaveLength(2)
    expect(sources.opened[1]!.url).toBe('/api/sessions/1/queue/stream')

    // And it is a feed this panel is actually listening to, rather than a
    // socket it opened and forgot about.
    sources.opened[1]!.emit(
      'message',
      JSON.stringify({
        ...BUSY,
        speakers: [
          {
            discord_user_id: '2',
            display_name: 'Anna',
            status: 'running',
            attempts: 3,
            error: null,
          },
        ],
      }),
    )
    await flushPromises()

    expect(panel.text()).toContain('3 attempts')
    expect(panel.text()).toContain('watching live')
  })

  it('watches the second redo on the timer when that is all there is', async () => {
    // The same defect on the fallback path, where it is easier to miss:
    // the timer stops itself the moment there is nothing left to watch, so
    // after a finished redo neither the stream slot nor the timer is doing
    // anything -- and the slot still looked occupied.
    withoutEventSource()
    let served: unknown = BUSY
    const api = apiServing(() => served)
    stubAutoImports(api as never)

    const panel = mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()

    served = FINISHED
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()

    served = BUSY
    await panel.find('button').trigger('click')
    await flushPromises()
    const afterPress = api.mock.calls.length

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()

    expect(api.mock.calls.length).toBeGreaterThan(afterPress)
    expect(panel.text()).toContain('checking every few seconds')
  })
})
