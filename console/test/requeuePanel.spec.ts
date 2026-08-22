/**
 * Who the Transcription panel reveals itself to, and when it stops polling.
 *
 * Both properties were broken in ways that a passing build cannot show.
 * The panel decides its own visibility from an HTTP status, and it drives
 * a timer that outlives nothing but its own component — neither is
 * visible in a type check, a lint pass or a render.
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

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('who the panel shows itself to', () => {
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

describe('when the panel stops reading the queue', () => {
  it('makes no further request after it is unmounted mid-poll', async () => {
    // `clearTimeout` cannot stop a timer that has already fired, and the
    // continuation after its `await` installed a fresh one. Navigating
    // away during a poll left twenty database reads a minute running for
    // the life of the tab.
    const busy = {
      ...SNAPSHOT,
      speakers: [
        { discord_user_id: '2', display_name: 'Anna', status: 'running', attempts: 1, error: null },
      ],
    }
    const api = vi.fn().mockResolvedValue(busy)
    stubAutoImports(api as never)

    const panel = mount(RequeuePanel, { props: { sessionId: '1' } })
    await flushPromises()
    const afterMount = api.mock.calls.length

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(api.mock.calls.length).toBeGreaterThan(afterMount)

    const beforeUnmount = api.mock.calls.length
    panel.unmount()
    await vi.advanceTimersByTimeAsync(30_000)
    await flushPromises()

    expect(api.mock.calls.length).toBe(beforeUnmount)
  })
})
