/**
 * The two controls that stand between an administrator and a credential
 * that decides who gets a session.
 *
 * The decisions themselves are pinned in `oauthClient.spec.ts`, which does
 * not mount anything. What is left here is exactly the part that only a
 * rendered component can be wrong about, and all of it is invisible in a
 * screenshot:
 *
 * - **There is no input anywhere on the registration form that could carry
 *   a client secret**, and no request it emits could contain one. A
 *   password box added to that form later would render identically to a
 *   reviewer and would silently clear a working credential every time
 *   somebody corrected a client id.
 * - **The secret control shows no value and no mask**, its box does not
 *   exist until somebody presses for it, and what was typed does not
 *   outlive the panel. A masked placeholder is a value — it says how long
 *   the credential is — and a value would have had to come from somewhere.
 * - **Clearing is a separate, confirmed act**, never something an empty box
 *   can do.
 *
 * `stubAutoImports` is `requeuePanel.spec.ts`'s convention: Nuxt provides
 * these globals, vitest does not, and stubbing them is cheaper than
 * starting a Nuxt runtime for two components whose only dependency on one
 * is `useId` and `useSay`.
 */
import { describe, expect, it, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { computed, nextTick, ref, watch } from 'vue'

import GuildSignInPage from '../app/pages/g/[slug]/sign-in.vue'
import SignInClientForm from '../app/components/SignInClientForm.vue'
import SignInClientSecret from '../app/components/SignInClientSecret.vue'
import {
  type ClientDraft,
  type GuildOAuthClient,
  emptyClientDraft,
} from '../app/utils/oauthClient'

/** Nuxt auto-imports these; vitest runs without Nuxt. */
function stubAutoImports() {
  vi.stubGlobal('ref', ref)
  vi.stubGlobal('computed', computed)
  vi.stubGlobal('watch', watch)
  vi.stubGlobal('nextTick', nextTick)
  let ids = 0
  vi.stubGlobal('useId', () => `id-${++ids}`)
  // `useSay` turns a decided sentence into words. What it renders is not
  // what this file is about, so a message comes back as its key.
  vi.stubGlobal('useSay', () => (value: unknown) =>
    value === null || value === undefined ? '—' : String((value as { key?: string }).key ?? value),
  )
}

/** The templates call `$t`; the key comes back as itself. */
const MOCKS = { $t: (key: string) => key }

const CLIENT: GuildOAuthClient = {
  guildId: '1289374650912837465',
  slug: 'acme',
  provider: 'outline',
  baseUrl: 'https://outline.acme.example',
  clientId: 'client-abc',
  redirectUri: null,
  hasSecret: false,
  createdAt: null,
  updatedAt: null,
}

function ready(over: Partial<ClientDraft> = {}): ClientDraft {
  return {
    slug: 'acme',
    provider: 'outline',
    baseUrl: 'https://outline.acme.example',
    clientId: 'client-abc',
    redirectMode: 'default',
    redirectUri: '',
    ...over,
  }
}

afterEach(() => vi.unstubAllGlobals())

describe('the registration form', () => {
  it('has no password field, and no field named for a secret', () => {
    // The load-bearing assertion. `ClientDraft` has nowhere to put a
    // credential, and this is the check that the template did not grow a
    // box that binds to something else and sends it anyway.
    //
    // Asserted over the inputs rather than over `html()`, because the
    // template's own comments explain at length why there is no credential
    // field here -- and a check that fails on being described is a check
    // people delete.
    stubAutoImports()
    const form = mount(SignInClientForm, {
      props: { mode: 'register', initial: emptyClientDraft() },
      global: { mocks: MOCKS },
    })
    expect(form.findAll('input[type="password"]')).toHaveLength(0)
    for (const input of form.findAll('input')) {
      const named = `${input.attributes('id') ?? ''} ${input.attributes('name') ?? ''}`
      expect(named.toLowerCase(), `${named} looks like a credential field`)
        .not.toContain('secret')
    }
  })

  it('submits a body with no credential in it', () => {
    stubAutoImports()
    const form = mount(SignInClientForm, {
      props: { mode: 'register', initial: ready() },
      global: { mocks: MOCKS },
    })
    form.find('form').trigger('submit')
    const draft = form.emitted('submit')?.[0]?.[0] as ClientDraft
    expect(Object.keys(draft)).not.toContain('secret')
    expect(Object.keys(draft)).not.toContain('clientSecret')
  })

  it('does not open shouting at a form nobody has typed in', () => {
    // A blank registration is every complaint at once. Rendering them
    // before anybody has touched a field is a form that reads as broken on
    // arrival.
    stubAutoImports()
    const form = mount(SignInClientForm, {
      props: { mode: 'register', initial: emptyClientDraft() },
      global: { mocks: MOCKS },
    })
    expect(form.html()).not.toContain('admin.signInLink.slugEmpty')
    expect(form.findAll('[aria-invalid="true"]')).toHaveLength(0)
  })

  it('refuses to submit a draft it has complaints about, and then shows them', async () => {
    stubAutoImports()
    const form = mount(SignInClientForm, {
      props: { mode: 'register', initial: emptyClientDraft() },
      global: { mocks: MOCKS },
    })
    await form.find('form').trigger('submit')
    expect(form.emitted('submit')).toBeUndefined()
    expect(form.html()).toContain('admin.signInLink.slugEmpty')
  })

  it('complains about a capital in a sign-in name as soon as it is typed', async () => {
    // Beside the field, while the reader is still in it -- because
    // `apiError` keeps only a status, so a rule only the API checks is a
    // rule whose reason nobody ever reads.
    stubAutoImports()
    const form = mount(SignInClientForm, {
      props: { mode: 'register', initial: emptyClientDraft() },
      global: { mocks: MOCKS },
    })
    const slug = form.findAll('input[type="text"]')[0]!
    await slug.setValue('Acme')
    expect(form.html()).toContain('admin.signInLink.slugShape')
  })

  it('never offers to check whether a name is free', async () => {
    // The one property here that is a security decision rather than a
    // preference: a console that could answer this would be an oracle for
    // which organisations use the service, reachable by anybody who
    // administers any guild anywhere.
    //
    // `useApi` is deliberately **not** stubbed in this file. A form that
    // grew a lookup -- of a slug, of a guild, of anything -- would call it
    // and throw on mount, so "this component makes no request" is asserted
    // by every test here and stated by this one.
    stubAutoImports()
    const form = mount(SignInClientForm, {
      props: { mode: 'register', initial: emptyClientDraft() },
      global: { mocks: MOCKS },
    })
    // Two buttons, and they are Save and Cancel. A third control on this
    // form is where an availability check would have to live.
    const buttons = form.findAll('button')
    expect(buttons).toHaveLength(2)
    expect(buttons.map((button) => button.attributes('type'))).toEqual(['submit', 'button'])

    // Nothing it says is about a name being free. The rendered text is
    // translation keys, so this is a check on the vocabulary this
    // namespace has -- there is no sentence here to render one.
    await form.findAll('input[type="text"]')[0]!.setValue('acme')
    for (const word of ['available', 'taken', 'free', 'exists']) {
      expect(form.text().toLowerCase(), `the form talks about a name being ${word}`)
        .not.toContain(word)
    }
    expect(form.emitted()).not.toHaveProperty('lookup')
  })

  it('has no dropdown for a provider with one legal value', () => {
    stubAutoImports()
    const form = mount(SignInClientForm, {
      props: { mode: 'register', initial: emptyClientDraft() },
      global: { mocks: MOCKS },
    })
    expect(form.findAll('select')).toHaveLength(0)
    expect(form.html()).toContain('outline')
  })

  it('hides the callback box until a guild says it has one of its own', async () => {
    // "" and null are the same value in a text box and different values in
    // this API, so the default is a state rather than an empty string.
    stubAutoImports()
    const form = mount(SignInClientForm, {
      props: { mode: 'register', initial: ready() },
      global: { mocks: MOCKS },
    })
    expect(form.find('input[type="url"][id$="-redirect"]').exists()).toBe(false)
    await form.find('input[type="checkbox"]').setValue(true)
    expect(form.find('input[type="url"][id$="-redirect"]').exists()).toBe(true)
  })

  it('forgets a typed callback when the default is chosen again', async () => {
    // Otherwise the box is gone from the screen and its value is still in
    // the request, which is an interface disagreeing with itself.
    stubAutoImports()
    const form = mount(SignInClientForm, {
      props: { mode: 'register', initial: ready() },
      global: { mocks: MOCKS },
    })
    const box = form.find('input[type="checkbox"]')
    await box.setValue(true)
    await form.find('input[type="url"][id$="-redirect"]').setValue('https://acme.example/cb')
    await box.setValue(false)
    await form.find('form').trigger('submit')
    const draft = form.emitted('submit')?.[0]?.[0] as ClientDraft
    expect(draft.redirectMode).toBe('default')
    expect(draft.redirectUri).toBe('')
  })

  it('starts over when it is reopened on another guild’s registration', async () => {
    // Without this, switching servers with the panel open edits the first
    // guild's values under the second guild's heading.
    stubAutoImports()
    const form = mount(SignInClientForm, {
      props: { mode: 'change', initial: ready({ slug: 'acme' }) },
      global: { mocks: MOCKS },
    })
    await form.setProps({ initial: ready({ slug: 'other' }) })
    expect((form.findAll('input[type="text"]')[0]!.element as HTMLInputElement).value)
      .toBe('other')
  })

  it('disables its buttons while a request is running rather than removing them', async () => {
    // A control that unmounts itself when pressed drops the keyboard to the
    // top of the document.
    stubAutoImports()
    const form = mount(SignInClientForm, {
      props: { mode: 'register', initial: ready(), busy: true },
      global: { mocks: MOCKS },
    })
    for (const button of form.findAll('button')) {
      expect(button.attributes('disabled')).toBeDefined()
    }
  })
})

describe('the client secret', () => {
  it('shows no value, and no mask standing in for one', () => {
    // A mask is a value: it says how long the credential is and what its
    // first characters are, and it promises a "show" button this API cannot
    // serve.
    stubAutoImports()
    const secret = mount(SignInClientSecret, {
      props: { client: { ...CLIENT, hasSecret: true } },
      global: { mocks: MOCKS },
    })
    expect(secret.findAll('input')).toHaveLength(0)
    expect(secret.html()).not.toContain('•')
    expect(secret.html()).not.toContain('*****')
    expect(secret.html()).toContain('admin.signInLink.secretNeverShown')
  })

  it('has no box at all until somebody presses for one', async () => {
    // An empty password box rendered beside a stored credential is an
    // invitation to save the form and silently clear it, which is the exact
    // failure the API split this onto its own route to prevent.
    stubAutoImports()
    const secret = mount(SignInClientSecret, {
      props: { client: { ...CLIENT, hasSecret: true } },
      global: { mocks: MOCKS },
    })
    expect(secret.find('input[type="password"]').exists()).toBe(false)
    await secret.findAll('button')[0]!.trigger('click')
    expect(secret.find('input[type="password"]').exists()).toBe(true)
  })

  it('drops what was typed when the panel is closed', async () => {
    // There is nothing to come back to -- the value cannot be read back
    // from anywhere -- so a half-typed credential in a hidden input is only
    // a credential sitting in the page for longer than anybody meant.
    stubAutoImports()
    const secret = mount(SignInClientSecret, {
      props: { client: CLIENT },
      global: { mocks: MOCKS },
    })
    await secret.findAll('button')[0]!.trigger('click')
    await secret.find('input[type="password"]').setValue('sh-typed')
    // Cancel is the second button inside the open form.
    await secret.findAll('form button')[1]!.trigger('click')
    expect(secret.html()).not.toContain('sh-typed')

    await secret.findAll('button')[0]!.trigger('click')
    expect((secret.find('input[type="password"]').element as HTMLInputElement).value).toBe('')
  })

  it('drops what was typed when the answer comes back', async () => {
    stubAutoImports()
    const secret = mount(SignInClientSecret, {
      props: { client: CLIENT },
      global: { mocks: MOCKS },
    })
    await secret.findAll('button')[0]!.trigger('click')
    await secret.find('input[type="password"]').setValue('sh-typed')
    await secret.setProps({ client: { ...CLIENT, hasSecret: true } })
    expect(secret.html()).not.toContain('sh-typed')
  })

  it('drops what was typed when the guild changes under it', async () => {
    stubAutoImports()
    const secret = mount(SignInClientSecret, {
      props: { client: CLIENT },
      global: { mocks: MOCKS },
    })
    await secret.findAll('button')[0]!.trigger('click')
    await secret.find('input[type="password"]').setValue('sh-typed')
    await secret.setProps({ client: { ...CLIENT, guildId: '999' } })
    expect(secret.html()).not.toContain('sh-typed')
  })

  it('refuses to submit an empty box, so emptiness cannot read as clearing', async () => {
    stubAutoImports()
    const secret = mount(SignInClientSecret, {
      props: { client: { ...CLIENT, hasSecret: true } },
      global: { mocks: MOCKS },
    })
    await secret.findAll('button')[0]!.trigger('click')
    expect(secret.find('form button[type="submit"]').attributes('disabled')).toBeDefined()
    await secret.find('input[type="password"]').setValue('sh-1')
    expect(secret.find('form button[type="submit"]').attributes('disabled')).toBeUndefined()
    await secret.find('form').trigger('submit')
    expect(secret.emitted('store')?.[0]).toEqual(['sh-1'])
  })

  it('offers nothing to clear when there is nothing stored', () => {
    stubAutoImports()
    const secret = mount(SignInClientSecret, {
      props: { client: CLIENT },
      global: { mocks: MOCKS },
    })
    expect(secret.html()).not.toContain('admin.signInLink.secretClear')
    expect(secret.html()).toContain('admin.signInLink.secretSet')
  })

  it('makes clearing a second, deliberate press', async () => {
    // Irreversible in the strongest sense available: nothing anywhere can
    // read back what was there to put it back.
    stubAutoImports()
    const secret = mount(SignInClientSecret, {
      props: { client: { ...CLIENT, hasSecret: true } },
      global: { mocks: MOCKS },
    })
    await secret.findAll('button')[1]!.trigger('click')
    expect(secret.emitted('clear')).toBeUndefined()
    expect(secret.html()).toContain('admin.signInLink.secretClearConfirmBody')
    await secret.findAll('button')[0]!.trigger('click')
    expect(secret.emitted('clear')).toHaveLength(1)
  })

  it('never has both panels open at once', async () => {
    // "Type a new credential" and "throw the old one away" are opposite
    // intentions, and a reader who has one of them in front of them should
    // not be one mis-click from the other.
    stubAutoImports()
    const secret = mount(SignInClientSecret, {
      props: { client: { ...CLIENT, hasSecret: true } },
      global: { mocks: MOCKS },
    })
    await secret.findAll('button')[1]!.trigger('click')
    expect(secret.find('input[type="password"]').exists()).toBe(false)
  })
})

describe('the page a guild’s link points at', () => {
  /** Nuxt's page-level auto-imports, plus stubs for the three components it
   *  renders. `useApi` stays unstubbed here too, and that is the assertion:
   *  this page performs no lookup at all. */
  function mountPage(slug: string) {
    stubAutoImports()
    vi.stubGlobal('definePageMeta', () => undefined)
    vi.stubGlobal('useRoute', () => ({ params: { slug } }))
    vi.stubGlobal('useHead', () => undefined)
    vi.stubGlobal('useI18n', () => ({ t: (key: string) => key }))
    return mount(GuildSignInPage, {
      global: {
        mocks: MOCKS,
        stubs: {
          SturnusMark: { template: '<span />' },
          NuxtLink: { props: ['to'], template: '<a :href="to"><slot /></a>' },
          // The real one puts a value inside a sentence; what matters here
          // is that the value reaches the page, so it renders the slot.
          'i18n-t': { props: ['keypath'], template: '<p>{{ keypath }}<slot name="slug" /></p>' },
        },
      },
    })
  }

  it('sends the browser to the login endpoint with the slug in the query', () => {
    // A plain anchor rather than a fetch: the OAuth flow is a navigation to
    // another origin and back, and an XHR cannot follow it.
    const page = mountPage('acme')
    expect(page.find('a[href^="/api/auth/login"]').attributes('href'))
      .toBe('/api/auth/login?guild=acme')
  })

  it('renders identically for a name nobody has registered', () => {
    // **The security property of this page.** `/api/auth/login?guild=…`
    // answers the same 404 with the same body to a name nobody holds, a
    // name that is not a name, and a guild whose secret was never supplied
    // -- so that an attacker walking a list of organisation names cannot
    // tell "no such organisation here" from "one, half-configured". A page
    // that rendered differently for a name it recognised would put that
    // oracle back, in HTML, in front of anybody with no session at all.
    const one = mountPage('acme').html().replaceAll('acme', 'SLUG')
    const other = mountPage('zzzzzzzz').html().replaceAll('zzzzzzzz', 'SLUG')
    expect(other).toBe(one)
  })

  it('renders identically for a name that is not spelled like one', () => {
    const one = mountPage('acme').html().replaceAll('acme', 'SLUG')
    const other = mountPage('ACME').html().replaceAll('ACME', 'SLUG')
    expect(other).toBe(one)
  })

  it('escapes what it was handed rather than trusting it', () => {
    // Nothing this console writes can produce such a slug and the API
    // cannot store one. This is the read path: whatever is in the address
    // bar reaches an anchor, so it is escaped on the way.
    const page = mountPage('a&b')
    expect(page.find('a[href^="/api/auth/login"]').attributes('href'))
      .toBe('/api/auth/login?guild=a%26b')
  })

  it('offers the way back to this deployment’s own sign-in', () => {
    // For somebody who followed a link they were not the intended reader
    // of, and whose account lives in the deployment's own Outline.
    expect(mountPage('acme').find('a[href="/sign-in"]').exists()).toBe(true)
  })
})
