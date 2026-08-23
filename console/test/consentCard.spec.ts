/**
 * That there is one consent write path, and that the dashboard uses it.
 *
 * The band on the dashboard exists because consent is what a participant
 * most likely came for, and the temptation it creates is a second, simpler
 * consent control written next to the first. That would be a second set of
 * consent bugs, in the one area of this console where that is not an
 * acceptable trade — and it would pass every existing test, because both
 * copies would render the same words.
 *
 * So the property worth pinning is not what the card looks like. It is which
 * requests it makes: `PUT /me/consents/{guild}/scope` and `POST
 * /me/consents/{guild}/revoke`, from the one component both pages mount. A
 * page that grew its own would not be caught by a snapshot, and is caught
 * here the moment it stops going through this component.
 *
 * `stubAutoImports` is `requeuePanel.spec.ts`'s convention: Nuxt provides
 * these globals, vitest does not, and stubbing them is cheaper than starting
 * a Nuxt runtime for a component whose only dependency on one is `useApi`.
 */
import { describe, expect, it, vi, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { computed, ref } from 'vue'

import ConsentCard from '../app/components/ConsentCard.vue'
import type { MyConsent } from '../app/utils/myConsents'

/** Nuxt auto-imports these; vitest runs without Nuxt. */
function stubAutoImports(api: (path: string, options?: unknown) => Promise<unknown>) {
  vi.stubGlobal('ref', ref)
  vi.stubGlobal('computed', computed)
  vi.stubGlobal('useApi', () => api)
}

/** The template calls `$t`; what it renders is not what this file is about,
 *  so the key comes back as itself. */
const MOCKS = { $t: (key: string) => key }

function consent(over: Partial<MyConsent> = {}): MyConsent {
  return {
    guild_id: '4711',
    state: 'active',
    active: true,
    scope: 'audio',
    policy_version: '3',
    guild_policy_version: '3',
    granted_at: '2026-01-02T10:00:00+00:00',
    revoked_at: null,
    video_consent_offered: true,
    ...over,
  }
}

afterEach(() => vi.unstubAllGlobals())

describe('the one component that writes a consent', () => {
  it('widens the scope through the endpoint the settings page uses', async () => {
    const api = vi.fn().mockResolvedValue({ scope: 'audio_video', changed: true, refusal: null })
    stubAutoImports(api as never)

    const card = mount(ConsentCard, {
      props: { row: consent(), confirming: null },
      global: { mocks: MOCKS },
    })
    // The second chip is `audio_video`; the guild offers video consent, so
    // there are two.
    const chips = card.findAll('input[type="radio"]')
    expect(chips).toHaveLength(2)
    await chips[1]!.trigger('change')
    await flushPromises()

    expect(api).toHaveBeenCalledWith('/me/consents/4711/scope', {
      method: 'PUT',
      body: { scope: 'audio_video' },
    })
  })

  it('withdraws through the endpoint the settings page uses', async () => {
    const api = vi.fn().mockResolvedValue({ revoked: true, refusal: null, role_stays: true })
    stubAutoImports(api as never)

    // Already confirming, which is the state the parent puts the card into:
    // the confirmation is the parent's to hold, because only one may be open
    // across a whole list.
    const card = mount(ConsentCard, {
      props: { row: consent(), confirming: '4711' },
      global: { mocks: MOCKS },
    })
    const confirm = card
      .findAll('button')
      .find((button) => button.text() === 'settings.consent.withdraw.confirm')
    expect(confirm).toBeDefined()
    await confirm!.trigger('click')
    await flushPromises()

    expect(api).toHaveBeenCalledWith('/me/consents/4711/revoke', { method: 'POST' })
  })

  it('tells the list to reload after a write, so a stale row cannot linger', async () => {
    // A refusal always means the record on screen was already out of date.
    const api = vi.fn().mockRejectedValue(new Error('nope'))
    stubAutoImports(api as never)

    const card = mount(ConsentCard, {
      props: { row: consent(), confirming: '4711' },
      global: { mocks: MOCKS },
    })
    const confirm = card
      .findAll('button')
      .find((button) => button.text() === 'settings.consent.withdraw.confirm')
    await confirm!.trigger('click')
    await flushPromises()

    expect(card.emitted('changed')).toHaveLength(1)
  })

  it('offers no video option where the server has no policy for one', async () => {
    // Absent, not disabled. A disabled control still says the thing exists
    // and is being kept from somebody, which is a different and untrue
    // statement.
    stubAutoImports(vi.fn() as never)
    const card = mount(ConsentCard, {
      props: { row: consent({ video_consent_offered: false }), confirming: null },
      global: { mocks: MOCKS },
    })
    expect(card.findAll('input[type="radio"]')).toHaveLength(1)
  })

  it('writes nothing at all for a consent already withdrawn', async () => {
    // The endpoint answers 409 every time, and an interface that offers an
    // action it knows will fail is worse than one that explains why it
    // cannot.
    const api = vi.fn()
    stubAutoImports(api as never)
    const card = mount(ConsentCard, {
      props: {
        row: consent({ state: 'revoked', active: false, revoked_at: '2026-02-01T00:00:00+00:00' }),
        confirming: null,
      },
      global: { mocks: MOCKS },
    })

    expect(card.findAll('input[type="radio"]')).toHaveLength(0)
    expect(card.findAll('button')).toHaveLength(0)
    expect(api).not.toHaveBeenCalled()
  })
})
