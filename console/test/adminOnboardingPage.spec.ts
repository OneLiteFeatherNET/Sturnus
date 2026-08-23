/**
 * What the onboarding page does that no unit of `~/utils/onboarding` can show.
 *
 * The decisions are all in that module and tested there. What is left in
 * the page is *when it asks*, *what it sends*, and *which of four empty
 * states it draws* — and every one of those is a property that a passing
 * build cannot demonstrate, because each of them looks identical on screen
 * to the state it must not be confused with.
 *
 * Four failures are worth a build breaking over:
 *
 * - an empty channel picker drawn for a guild nothing has been mirrored
 *   for, which reads as a server with no voice channels and sends somebody
 *   hunting for a bug that is not there;
 * - a superseded request drawn as a failure, which sends somebody to check
 *   a permission that was never tested;
 * - a request that names a stored channel the mirror cannot resolve, which
 *   the applier refuses and which fails the whole intent;
 * - a poll that outlives the page, which is `requeuePanel`'s old defect in
 *   another costume.
 *
 * The real locale files are loaded from disk, for the reason
 * `adminQueuePage.spec.ts` loads them: a template asking for
 * `admin.onboarding.headng.bad` renders the key at somebody, and nothing
 * but a render catches it.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
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

import UiSelect from '../app/components/ui/UiSelect.vue'
import { useSay } from '../app/composables/useSay'
import OnboardingPage from '../app/pages/admin/onboarding.vue'

/** The one datetime shape this page asks for. `i18n.config.ts` is a Nuxt
 *  macro and cannot be imported here, and an unregistered format renders
 *  as an empty string rather than as an error — which is exactly the kind
 *  of hole a page test exists to catch. */
const UTC_MOMENT = {
  utcMoment: {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: 'UTC',
    timeZoneName: 'short',
  },
} as const

function load(locale: string) {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

/** One guild, so the switcher has nothing to disambiguate and the page's
 *  own controls are the only ones on it. */
const GUILDS = { guilds: [{ guild_id: '1', name: 'Alpha' }] }

const INVITE = {
  client_id: '42',
  url: 'https://discord.com/oauth2/authorize?client_id=42&scope=bot',
  permissions: '269487104',
  scopes: ['bot', 'applications.commands'],
}

const DIRECTORY = {
  guild_id: '1',
  synced_at: '2026-08-23T10:00:00+00:00',
  channels: [
    { id: '10', name: 'Standup', kind: 'voice', position: 0 },
    { id: '11', name: 'Retro', kind: 'voice', position: 1 },
  ],
  roles: [],
  members: [{ discord_user_id: '99', display_name: 'Anna' }],
}

/** Nothing has ever been mirrored: no channels, no roles, and `has_arrived`
 *  false to say which of the two that is. */
const EMPTY_DIRECTORY = { guild_id: '1', synced_at: null, channels: [], roles: [], members: [] }

function settings(stored: string | null) {
  return {
    guild_id: '1',
    settings: [
      {
        key: 'voice_channel_ids',
        value: stored,
        default: null,
        required: false,
        may_clear: true,
        integer: false,
        invalidates_consent: false,
        takes_effect: 'next_reconcile',
        deferred_while_recording: false,
      },
    ],
  }
}

function setup(bot: { has_arrived: boolean }, request: Record<string, unknown> | null = null) {
  return {
    guild_id: '1',
    bot: { has_arrived: bot.has_arrived, seen_at: bot.has_arrived ? '2026-08-23T10:00:00Z' : null },
    request,
  }
}

const ASKED = {
  id: '7',
  status: 'pending',
  requested_by: '99',
  requested_at: '2026-08-23T10:05:00+00:00',
  channel_ids: ['10'],
  consent_role_name: 'Sturnus Consent',
  settled_at: null,
  error: null,
}

/**
 * `useAsyncData`, in as much detail as this page uses it.
 *
 * Written out rather than mocked to a fixed value, because two of the
 * properties under test are about it: the page assigns straight into `data`
 * when a 202 answers, and it presses `refresh()` from its own timer.
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
function stubAutoImports(api: (path: string, options?: unknown) => Promise<unknown>) {
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
  // The signed-in administrator, so "asked by you" has something to be
  // true of.
  vi.stubGlobal('useSession', () => ref({ discord_user_id: '99', is_admin: true }))
}

/** An API serving one fixed world, with each route answered from a
 *  function so a test can change what a later poll finds. */
function serving(world: {
  setup: () => unknown
  directory?: () => unknown
  stored?: string | null
  invite?: unknown
}) {
  return vi.fn((path: string, options?: { method?: string; body?: unknown }) => {
    if (path === '/guilds') return Promise.resolve(GUILDS)
    if (path === '/invite') return Promise.resolve(world.invite ?? INVITE)
    if (path === '/guilds/1/directory') {
      return Promise.resolve(world.directory ? world.directory() : DIRECTORY)
    }
    if (path === '/guilds/1/settings') {
      return Promise.resolve(settings(world.stored ?? null))
    }
    if (path === '/guilds/1/setup') {
      if (options?.method === 'POST') return Promise.resolve(world.setup())
      return Promise.resolve(world.setup())
    }
    return Promise.reject(new Error(`unexpected ${path}`))
  })
}

/** The page awaits its data in `setup`, which Vue only runs inside a
 *  `<Suspense>`. Nuxt provides one around every page; here it is written
 *  out, in a render function rather than a template because vitest resolves
 *  `vue` to the build without a runtime compiler. */
const Host = defineComponent({
  setup: () => () => h(Suspense, null, { default: () => h(OnboardingPage) }),
})

async function openPage(api: ReturnType<typeof serving>) {
  stubAutoImports(api as never)
  const i18n = createI18n({
    legacy: false,
    locale: 'en',
    fallbackLocale: 'en',
    messages: { en: load('en'), de: load('de') },
    // The one datetime shape this page asks for. `i18n.config.ts` is a
    // Nuxt macro and cannot be imported here, and an unregistered format
    // renders as an empty string rather than as an error -- which is
    // exactly the kind of hole a page test exists to catch.
    // Registered under the tag as well as the code, exactly as
    // `i18n.config.ts` does: `useSay` formats with `en-GB`, because the
    // tag and the code disagree about the order of a date.
    datetimeFormats: { en: UTC_MOMENT, 'en-GB': UTC_MOMENT },
  })
  const page = mount(Host, {
    global: {
      plugins: [i18n],
      components: { UiSelect },
      stubs: { NuxtLink: { template: '<a><slot /></a>' } },
    },
  })
  await flushPromises()
  await flushPromises()
  return page
}

/**
 * The page's own submit button.
 *
 * Found by what it says rather than by position: the guild switcher is a
 * `button` too, and it is the first one on the page — a test that clicked
 * index zero would open a dropdown and assert about a request nobody made.
 */
function submitButton(page: ReturnType<typeof mount>) {
  return page
    .findAll('button')
    .find((button) => button.text().startsWith('Ask the bot') || button.text() === 'Asking…')!
}

/** The body of the last POST this page made. */
function posted(api: ReturnType<typeof serving>): Record<string, unknown> {
  const call = [...api.mock.calls]
    .reverse()
    .find(([, options]) => (options as { method?: string } | undefined)?.method === 'POST')
  return (call?.[1] as { body: Record<string, unknown> }).body
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('a server the bot has not reached', () => {
  it('says nobody has looked yet, rather than that it has no voice channels', async () => {
    // The two states are one empty list on the wire and opposite
    // instructions on screen. Getting this wrong sends somebody into
    // Discord to create a channel that is already there.
    const page = await openPage(
      serving({ setup: () => setup({ has_arrived: false }), directory: () => EMPTY_DIRECTORY }),
    )

    expect(page.text()).toContain('Nobody has looked at this server yet')
    expect(page.text()).not.toContain('This server has no voice channels')
    // And no picker: an empty group of checkboxes reads as a list that
    // failed to load.
    expect(page.find('input[type="checkbox"]').exists()).toBe(false)
  })

  it('says the server has none once something has been mirrored', async () => {
    const page = await openPage(
      serving({ setup: () => setup({ has_arrived: true }), directory: () => EMPTY_DIRECTORY }),
    )

    expect(page.text()).toContain('This server has no voice channels')
    expect(page.text()).not.toContain('Nobody has looked at this server yet')
  })

  it('fills the picker in on its own when the bot arrives', async () => {
    // What the polling is for. Without it somebody watches an unchanging
    // page and has no way to know when pressing Refresh would help.
    let arrived = false
    const api = serving({
      setup: () => setup({ has_arrived: arrived }),
      directory: () => (arrived ? DIRECTORY : EMPTY_DIRECTORY),
    })
    const page = await openPage(api)
    expect(page.text()).toContain('Nobody has looked at this server yet')

    arrived = true
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    await flushPromises()

    expect(page.text()).toContain('Standup')
    expect(page.text()).not.toContain('Nobody has looked at this server yet')
  })
})

describe('what a request names', () => {
  it('sends the channels ticked', async () => {
    const api = serving({ setup: () => setup({ has_arrived: true }) })
    const page = await openPage(api)

    const boxes = page.findAll('input[type="checkbox"]')
    expect(boxes).toHaveLength(2)
    await boxes[1]!.setValue(true)
    await submitButton(page).trigger('click')
    await flushPromises()

    // Every id a string. A snowflake through a JavaScript number comes
    // back ending in other digits.
    expect(posted(api)).toEqual({ channel_ids: ['11'], consent_role_name: null })
  })

  it('carries what the server already records, and never a stale stored id', async () => {
    // The failure this prevents is total rather than partial: the applier
    // refuses a channel it cannot see, one refusal fails the whole intent,
    // and a guild with one deleted channel in `voice_channel_ids` would
    // have every request it ever made fail over a room nobody asked about.
    const api = serving({ setup: () => setup({ has_arrived: true }), stored: '10,404' })
    const page = await openPage(api)

    await submitButton(page).trigger('click')
    await flushPromises()

    expect(posted(api)).toEqual({ channel_ids: ['10'], consent_role_name: null })
    expect(page.text()).toContain('missing from the mirror')
  })

  it('does not offer to untick a channel this server already records', async () => {
    // Setting up adds to `voice_channel_ids` and never removes from it, so
    // that control would do nothing at all.
    const api = serving({ setup: () => setup({ has_arrived: true }), stored: '10' })
    const page = await openPage(api)

    const boxes = page.findAll('input[type="checkbox"]')
    expect((boxes[0]!.element as HTMLInputElement).checked).toBe(true)
    expect((boxes[0]!.element as HTMLInputElement).disabled).toBe(true)
    // Said in a word as well: a state carried only by a rendering style is
    // a state nobody has been told.
    expect(page.text()).toContain('already recorded')
  })

  it('refuses to ask for nothing, and says why beside the button', async () => {
    const api = serving({ setup: () => setup({ has_arrived: true }) })
    const page = await openPage(api)

    expect((submitButton(page).element as HTMLButtonElement).disabled).toBe(true)
    expect(page.text()).toContain('Tick at least one channel')
  })
})

describe('what came of it', () => {
  it('never draws a superseded request as a failure', async () => {
    // Nothing went wrong to a superseded request: it was replaced before
    // the bot reached it and never attempted.
    const page = await openPage(
      serving({
        setup: () => setup({ has_arrived: true }, { ...ASKED, status: 'superseded' }),
      }),
    )

    expect(page.text()).toContain('This request was replaced before anything was done to it')
    expect(page.text()).toContain('Nothing went wrong')
    expect(page.text()).not.toContain('The bot could not finish')
  })

  it('renders what the bot itself wrote, and says there is no retry', async () => {
    const message
      = 'I am missing the Manage Roles permission, so I could not create the `Sturnus Consent` role.'
    const page = await openPage(
      serving({
        setup: () =>
          setup({ has_arrived: true }, { ...ASKED, status: 'failed', error: message }),
      }),
    )

    expect(page.text()).toContain(message)
    expect(page.text()).toContain('nothing is retrying behind it')
    expect(page.text()).toContain('that is a new request')
  })

  it('says the bot is not there when a request is pending in an empty mirror', async () => {
    const page = await openPage(
      serving({
        setup: () => setup({ has_arrived: false }, ASKED),
        directory: () => EMPTY_DIRECTORY,
      }),
    )

    expect(page.text()).toContain('Nothing is going to attempt this yet')
    expect(page.text()).toContain('never attempted at all')
  })

  it('says when a colleague has replaced the request this browser sent', async () => {
    // The supersede rule as it is actually experienced: `GET` answers with
    // the guild's newest, which after somebody else asked is theirs.
    let current: Record<string, unknown> = ASKED
    const api = serving({ setup: () => setup({ has_arrived: true }, current) })
    const page = await openPage(api)

    await page.findAll('input[type="checkbox"]')[0]!.setValue(true)
    await submitButton(page).trigger('click')
    await flushPromises()
    expect(page.text()).not.toContain('This is not the request submitted from this browser')

    current = { ...ASKED, id: '8', requested_by: '55', status: 'applied', settled_at: '2026-08-23T10:06:00Z' }
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()

    expect(page.text()).toContain('This is not the request submitted from this browser')
    // And the outcome is still the guild's honest answer.
    expect(page.text()).toContain('This server is set up')
  })

  it('leaves the form live while a request is pending, and says what asking again does', async () => {
    // The API deliberately accepts a second request over a first: refusing
    // one would leave somebody who mistyped a channel waiting out a tick
    // before they could correct it.
    const api = serving({ setup: () => setup({ has_arrived: true }, ASKED), stored: '10' })
    const page = await openPage(api)

    expect((submitButton(page).element as HTMLButtonElement).disabled).toBe(false)
    expect(page.text()).toContain('replaces it rather than queueing behind it')
  })
})

describe('watching', () => {
  it('stops the moment the request settles', async () => {
    let status = 'pending'
    const api = serving({ setup: () => setup({ has_arrived: true }, { ...ASKED, status }) })
    await openPage(api)

    status = 'applied'
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    const afterSettling = api.mock.calls.length

    await vi.advanceTimersByTimeAsync(30_000)
    await flushPromises()

    expect(api.mock.calls.length).toBe(afterSettling)
  })

  it('makes no further request after the page has gone', async () => {
    // `clearTimeout` cannot stop a timer that has already fired, and the
    // continuation after its `await` installs a fresh one. The same defect
    // left twenty database reads a minute running for the life of a tab.
    const api = serving({ setup: () => setup({ has_arrived: true }, ASKED) })
    const page = await openPage(api)

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    const beforeUnmount = api.mock.calls.length

    page.unmount()
    await vi.advanceTimersByTimeAsync(30_000)
    await flushPromises()

    expect(api.mock.calls.length).toBe(beforeUnmount)
  })
})

describe('a deployment with no application id', () => {
  it('says so, and hands over what the URL generator in Discord needs instead', async () => {
    const page = await openPage(
      serving({
        setup: () => setup({ has_arrived: true }),
        invite: { client_id: null, url: null, permissions: '269487104', scopes: ['bot'] },
      }),
    )

    expect(page.text()).toContain('This deployment has no invitation link')
    expect(page.text()).toContain('STURNUS_DISCORD_CLIENT_ID')
    expect(page.text()).toContain('269487104')
    // No dead link offered in place of a working one.
    expect(page.findAll('a[target="_blank"]')).toHaveLength(0)
  })

  it('names the role position the invitation cannot ask for', async () => {
    // No bitmask expresses "my role must sit above that one", so the
    // invitation link cannot carry it and only prose can -- said where it
    // can still be acted on, in the same visit to Discord.
    const page = await openPage(serving({ setup: () => setup({ has_arrived: true }) }))
    expect(page.text()).toContain('Server Settings → Roles')
  })
})
