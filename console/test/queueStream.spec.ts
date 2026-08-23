/**
 * When the live feed is working, when it has failed, and how the page finds out.
 *
 * Every property here is one no build, type check or render can show. A
 * stream that is open and silent looks exactly like a stream that is open
 * and up to date; a stream that reconnected once looks exactly like a
 * stream that is about to give up; and a page whose feed died reports the
 * same figures as a page whose feed is live, right up until they change.
 *
 * The fallback is the reason this file is long. Sturnus is served through
 * a Cloudflare Tunnel and a reverse proxy, and an event stream is the one
 * response an intermediary can break silently: a proxy that buffers holds
 * every event until the response ends. From the browser that is an open
 * connection with nothing on it — no error, no data, no clue — which is
 * why "nothing has arrived yet" is on a deadline here.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

import {
  describeQueueMode,
  openQueueStream,
  parseStreamPayload,
  shouldFallBack,
  type EventSourceLike,
  type QueueStreamMode,
} from '../app/utils/queueStream'

/** A source a test drives by hand. Nothing here is a browser; the module
 *  under test never inspects a source beyond the three members below. */
class FakeSource implements EventSourceLike {
  listeners = new Map<string, ((event: { data?: unknown }) => void)[]>()
  closed = false
  readyState = 1

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

/** The module's own defaults are the ones that ship, so a test that means
 *  to exercise the defaults says nothing and a test that means to exercise
 *  a threshold names it. */
function watch(over: Partial<Parameters<typeof openQueueStream>[0]> = {}) {
  const source = new FakeSource()
  const snapshots: unknown[] = []
  const modes: QueueStreamMode[] = []
  const handle = openQueueStream({
    url: '/api/guilds/1/queue/stream',
    onSnapshot: (payload) => snapshots.push(payload),
    onMode: (mode) => modes.push(mode),
    open: () => source,
    ...over,
  })
  return { source, snapshots, modes, handle }
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('reading what arrived', () => {
  it('reads a data line as the JSON it is', () => {
    expect(parseStreamPayload('{"counts":{"pending":2}}')).toEqual({ counts: { pending: 2 } })
  })

  it('answers null for a line that is not JSON rather than throwing', () => {
    // A malformed frame is a defect upstream, and the useful response is
    // to keep the last good snapshot on screen. A stream that tore itself
    // down over one bad line would turn a cosmetic fault into a page that
    // stopped updating.
    expect(parseStreamPayload('not json')).toBeNull()
    expect(parseStreamPayload(undefined)).toBeNull()
    expect(parseStreamPayload(42)).toBeNull()
  })
})

describe('a stream that is working', () => {
  it('hands every snapshot to the caller and calls itself live', () => {
    const { source, snapshots, modes, handle } = watch()

    source.emit('message', '{"counts":{"pending":1}}')
    source.emit('message', '{"counts":{"pending":0}}')

    expect(snapshots).toEqual([{ counts: { pending: 1 } }, { counts: { pending: 0 } }])
    expect(handle.mode).toBe('live')
    expect(modes).toEqual(['live'])
  })

  it('keeps going after a frame it could not read', () => {
    const { source, snapshots, handle } = watch()

    source.emit('message', 'garbage')
    source.emit('message', '{"counts":{"pending":1}}')

    expect(snapshots).toEqual([{ counts: { pending: 1 } }])
    expect(handle.mode).toBe('live')
    expect(source.closed).toBe(false)
  })
})

describe('the two ends the server announces', () => {
  it('closes for good when the server says the queue has come to rest', () => {
    // The terminal event is the whole difference between a stream that
    // ends and a tab that reopens one for ever: a browser cannot tell a
    // deliberate close from a dropped one and reconnects after both.
    const { source, handle } = watch()

    source.emit('message', '{"counts":{"pending":1}}')
    source.emit('rest', '{"reason":"at rest"}')

    expect(handle.mode).toBe('rested')
    expect(source.closed).toBe(true)
  })

  it('closes for good when the queue stops being readable', () => {
    const { source, handle } = watch()

    source.emit('gone', '{"reason":"no longer readable"}')

    expect(handle.mode).toBe('gone')
    expect(source.closed).toBe(true)
  })

  it('sends nothing further after a terminal event', () => {
    const { source, snapshots } = watch()

    source.emit('rest', '{"reason":"at rest"}')
    source.emit('message', '{"counts":{"pending":9}}')

    expect(snapshots).toEqual([])
  })
})

describe('deciding that the live feed has failed', () => {
  it('does not give up on one drop, because that is what reconnecting is for', () => {
    // The server closes a healthy stream every ten minutes on purpose, so
    // that it keeps no task for a browser that has gone. A client that
    // treated that closure as a failure would fall back to polling on a
    // perfectly working connection, ten minutes in, every time.
    const { source, handle } = watch()

    source.emit('error')

    expect(handle.mode).toBe('connecting')
    expect(source.closed).toBe(false)
  })

  it('falls back to polling after enough consecutive failures', () => {
    const { source, modes, handle } = watch({ maxFailures: 3 })

    source.emit('error')
    source.emit('error')
    source.emit('error')

    expect(handle.mode).toBe('polling')
    expect(source.closed).toBe(true)
    expect(modes.at(-1)).toBe('polling')
  })

  it('counts only consecutive failures, so an event that arrives clears the tally', () => {
    const { source, handle } = watch({ maxFailures: 3 })

    source.emit('error')
    source.emit('error')
    source.emit('message', '{"counts":{"pending":1}}')
    source.emit('error')
    source.emit('error')

    expect(handle.mode).toBe('connecting')
    expect(source.closed).toBe(false)
  })

  it('gives up at once when the browser has stopped retrying', () => {
    // `readyState === CLOSED` is what a non-2xx answer produces, and 404
    // is what somebody who does not administer the guild gets. Waiting for
    // two more failures that will never come would leave the page
    // watching nothing at all.
    const { source, handle } = watch({ maxFailures: 3 })

    source.readyState = 2
    source.emit('error')

    expect(handle.mode).toBe('polling')
  })

  it('falls back when a connection is open and says nothing at all', async () => {
    // The buffering proxy. No error is raised and nothing is delivered,
    // so silence has to be given a deadline or the page waits for ever.
    const { handle, modes } = watch({ firstEventMs: 8000 })

    await vi.advanceTimersByTimeAsync(8000)

    expect(handle.mode).toBe('polling')
    expect(modes.at(-1)).toBe('polling')
  })

  it('does not fall back on a connection that spoke inside the deadline', async () => {
    const { source, handle } = watch({ firstEventMs: 8000 })

    source.emit('message', '{"counts":{"pending":1}}')
    await vi.advanceTimersByTimeAsync(60_000)

    expect(handle.mode).toBe('live')
  })

  it('re-arms the deadline after a drop, so a reconnection that never lands is not silence for ever', async () => {
    const { source, handle } = watch({ firstEventMs: 8000, maxFailures: 3 })

    source.emit('message', '{"counts":{"pending":1}}')
    source.emit('error')
    await vi.advanceTimersByTimeAsync(8000)

    expect(handle.mode).toBe('polling')
  })

  it('falls back immediately where there is no EventSource to open', () => {
    // A server render, or a browser too old for one. Reported as polling
    // rather than as an error: the caller has a working way to read the
    // queue and should use it.
    const modes: QueueStreamMode[] = []
    const handle = openQueueStream({
      url: '/api/guilds/1/queue/stream',
      onSnapshot: () => {},
      onMode: (mode) => modes.push(mode),
      open: () => null,
    })

    expect(handle.mode).toBe('polling')
    expect(modes).toEqual(['polling'])
  })

  it('falls back when making a source throws', () => {
    const handle = openQueueStream({
      url: '/api/guilds/1/queue/stream',
      onSnapshot: () => {},
      onMode: () => {},
      open: () => {
        throw new Error('refused')
      },
    })

    expect(handle.mode).toBe('polling')
  })
})

describe('the threshold on its own', () => {
  it('is reached at the limit and not before it', () => {
    expect(shouldFallBack(2, 3)).toBe(false)
    expect(shouldFallBack(3, 3)).toBe(true)
    expect(shouldFallBack(4, 3)).toBe(true)
  })
})

describe('letting go', () => {
  it('closes the source and delivers nothing afterwards', () => {
    const { source, snapshots, handle } = watch()

    handle.stop()
    source.emit('message', '{"counts":{"pending":1}}')

    expect(source.closed).toBe(true)
    expect(snapshots).toEqual([])
    expect(handle.mode).toBe('stopped')
  })

  it('cancels the silence deadline, so an unmounted page cannot report a fallback', async () => {
    // The defect this guards is `startQueuePolling`'s in another costume:
    // a timer that fires after the component has gone, calling back into
    // something that is no longer there.
    const { modes, handle } = watch({ firstEventMs: 8000 })

    handle.stop()
    await vi.advanceTimersByTimeAsync(60_000)

    expect(modes).toEqual(['stopped'])
    expect(handle.mode).toBe('stopped')
  })

  it('leaves a finished stream reporting why it finished', () => {
    // "The queue came to rest" stays the true account of why this stream
    // ended, even though the component that held it has since gone.
    const { source, handle } = watch()

    source.emit('rest', '{"reason":"at rest"}')
    handle.stop()

    expect(handle.mode).toBe('rested')
  })

  it('is safe to call more than once', () => {
    const { handle } = watch()

    handle.stop()
    expect(() => handle.stop()).not.toThrow()
  })
})

describe('telling the reader which of the two it is', () => {
  it('says something different for live and for polling', () => {
    // The requirement in one assertion: a reader must be able to tell
    // "live" from "checking every few seconds" without opening developer
    // tools, because the two look identical whenever the figures happen
    // not to be changing.
    const live = describeQueueMode('live')
    const polling = describeQueueMode('polling')

    expect(live).not.toBe(polling)
    expect(polling).toMatch(/few seconds/)
    expect(live).toMatch(/as it happens/)
  })

  it('says a rested feed was closed on purpose rather than lost', () => {
    expect(describeQueueMode('rested')).toMatch(/closed the live feed/)
  })

  it('has a sentence for every mode there is', () => {
    const every: QueueStreamMode[] = [
      'connecting',
      'live',
      'rested',
      'gone',
      'polling',
      'stopped',
    ]
    for (const mode of every) expect(describeQueueMode(mode).length).toBeGreaterThan(0)
  })
})
