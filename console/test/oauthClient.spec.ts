/**
 * A guild's own sign-in link, decided without a browser.
 *
 * Two of the properties checked here are security decisions rather than
 * preferences, and both of them are invisible in a rendered frame:
 *
 * - **Nothing in this module can carry a client secret.** The type the form
 *   edits has no field for one, so the checks below assert an absence —
 *   which is the only way an absence stays true after somebody adds a
 *   field "just for the edit case".
 * - **Nothing in this module asks whether a slug is free.** A console that
 *   could answer that would be an oracle for which organisations use this
 *   service, which is the one fact §2.2 is built to withhold. The refusal
 *   the API gives for a taken name and for a reserved one is one refusal,
 *   and the sentence this console renders for it is one sentence.
 *
 * The slug and URL rules are a second copy of
 * `sturnus.domain.oauth_clients`, so the cases below are lifted from what
 * that module's own tests pin: a copy that has quietly stopped agreeing
 * with the original is worse than no copy, because it refuses registrations
 * the deployment would have accepted.
 */
import { describe, expect, it } from 'vitest'

import {
  MAX_SECRET_LENGTH,
  PROVIDER_OUTLINE,
  type ClientDraft,
  type GuildOAuthClient,
  canSubmitClientSecret,
  clientPath,
  clientSecretPath,
  describeClientError,
  clientDraftBody,
  clientDraftOf,
  emptyClientDraft,
  isClientDraftReady,
  isGuildSignInPath,
  isMissingRegistration,
  isProviderUrl,
  linkState,
  loginUrl,
  parseClient,
  clientProblemFor,
  clientDraftProblems,
  clientSecretState,
  signInPath,
  signInUrl,
  slugProblem,
} from '../app/utils/oauthClient'

const GUILD = '1289374650912837465'

const REGISTERED: GuildOAuthClient = {
  guildId: GUILD,
  slug: 'acme',
  provider: PROVIDER_OUTLINE,
  baseUrl: 'https://outline.acme.example',
  clientId: 'client-abc',
  redirectUri: null,
  hasSecret: true,
  createdAt: '2026-08-01T09:00:00+00:00',
  updatedAt: '2026-08-02T09:00:00+00:00',
}

/** A draft that passes every rule, so that each test below can break one
 *  thing and be about that thing. */
function ready(overrides: Partial<ClientDraft> = {}): ClientDraft {
  return {
    slug: 'acme',
    provider: PROVIDER_OUTLINE,
    baseUrl: 'https://outline.acme.example',
    clientId: 'client-abc',
    redirectMode: 'default',
    redirectUri: '',
    ...overrides,
  }
}

describe('reading what the API sent', () => {
  it('unwraps the registration from the envelope the API answers with', () => {
    const client = parseClient({
      guild_id: GUILD,
      oauth_client: {
        slug: 'acme',
        provider: 'outline',
        base_url: 'https://outline.acme.example',
        client_id: 'client-abc',
        redirect_uri: null,
        has_secret: true,
        created_at: '2026-08-01T09:00:00+00:00',
        updated_at: '2026-08-02T09:00:00+00:00',
      },
    })
    expect(client).toEqual(REGISTERED)
  })

  it('keeps the guild id a string', () => {
    // A snowflake through `Number` loses its last digits and becomes an id
    // that looks right and names nobody. Every path this value takes -- the
    // request URL of every write on the page -- is a path where being one
    // digit out is silent.
    const client = parseClient({ guild_id: GUILD, oauth_client: { slug: 'acme' } })
    expect(client?.guildId).toBe(GUILD)
    expect(typeof client?.guildId).toBe('string')
  })

  it('has nowhere to put a secret, even when one is sent', () => {
    // Nothing sends this. The check is that if anything ever did -- a
    // debugging endpoint, a mistaken echo in a handler -- it would not
    // survive parsing and reach a template.
    const client = parseClient({
      guild_id: GUILD,
      oauth_client: { slug: 'acme', has_secret: true, client_secret: 'sh-do-not-keep-this' },
    })
    expect(JSON.stringify(client)).not.toContain('do-not-keep-this')
    expect(Object.keys(client ?? {})).not.toContain('clientSecret')
  })

  it('reads a registration that has no secret yet as having none', () => {
    const client = parseClient({ oauth_client: { slug: 'acme' } })
    expect(client?.hasSecret).toBe(false)
  })

  it('treats an absent redirect_uri as this deployment’s own callback', () => {
    expect(parseClient({ oauth_client: { slug: 'acme' } })?.redirectUri).toBeNull()
    expect(
      parseClient({ oauth_client: { slug: 'acme', redirect_uri: null } })?.redirectUri,
    ).toBeNull()
  })

  it('keeps a redirect URI a guild set for itself', () => {
    const client = parseClient({
      oauth_client: { slug: 'acme', redirect_uri: 'https://console.acme.example/callback' },
    })
    expect(client?.redirectUri).toBe('https://console.acme.example/callback')
  })

  it('reads a bare registration as well as an enveloped one', () => {
    expect(parseClient({ slug: 'acme' })?.slug).toBe('acme')
  })

  it('is nothing at all when there is no slug to name it by', () => {
    // Every remedy for a registration with no name is reachable from the
    // state where there is no registration, so this is the honest reading
    // rather than a lossy one.
    expect(parseClient({ oauth_client: { slug: '' } })).toBeNull()
    expect(parseClient({ oauth_client: {} })).toBeNull()
    expect(parseClient(null)).toBeNull()
    expect(parseClient('acme')).toBeNull()
    expect(parseClient([])).toBeNull()
  })
})

describe('what a sign-in name may be', () => {
  it('accepts the shapes the deployment accepts', () => {
    for (const slug of ['acme', 'acme-corp', 'a1b', 'a-1-b', 'x'.repeat(32)]) {
      expect(slugProblem(slug), `${slug} was refused`).toBeNull()
    }
  })

  it('insists on a letter first, so a slug and a snowflake cannot be confused', () => {
    // `/g/1289374650912837465/sign-in` and a guild id in a path are the
    // same string to whoever reads the link.
    expect(slugProblem('1289374650912837465')).not.toBeNull()
    expect(slugProblem('9acme')).not.toBeNull()
    expect(slugProblem('-acme')).not.toBeNull()
  })

  it('refuses a capital rather than lowercasing it', () => {
    // A slug quietly rewritten on the way into the table is a slug the
    // administrator does not recognise in the link they handed out. The
    // complaint is about the shape, and the value is untouched.
    expect(slugProblem('Acme')).toEqual({ key: 'admin.signInLink.slugShape' })
  })

  it('refuses whitespace rather than trimming it', () => {
    for (const slug of [' acme', 'acme ', 'ac me']) {
      expect(slugProblem(slug), `${slug} was accepted`).not.toBeNull()
    }
  })

  it('refuses a leading, trailing or doubled hyphen', () => {
    for (const slug of ['acme-', 'ac--me', '-acme']) {
      expect(slugProblem(slug), `${slug} was accepted`).not.toBeNull()
    }
  })

  it('says which mistake was made rather than "invalid"', () => {
    // Three different typing mistakes, and one sentence for all three
    // tells nobody which of them they made.
    expect(slugProblem('')?.key).toBe('admin.signInLink.slugEmpty')
    expect(slugProblem('ab')?.key).toBe('admin.signInLink.slugLength')
    expect(slugProblem('x'.repeat(33))?.key).toBe('admin.signInLink.slugLength')
    expect(slugProblem('Acme')?.key).toBe('admin.signInLink.slugShape')
  })

  it('never says whether a name is free', () => {
    // The one property of this page that is a security decision. There is
    // no reserved list here, no availability call, and therefore no way for
    // the console to distinguish "another organisation holds this" from
    // "this deployment reserves it" -- which is exactly the distinction the
    // API collapsed into a single 409 on purpose.
    const source = slugProblem.toString() + clientDraftProblems.toString()
    for (const reserved of ['login', 'static', 'console', 'well-known']) {
      expect(source, `the console carries a copy of the reserved list`).not.toContain(reserved)
    }
    // A reserved name is spelled like a slug, so shape has nothing to say
    // about it and the API is left to answer.
    expect(slugProblem('login')).toBeNull()
    expect(slugProblem('static')).toBeNull()
  })
})

describe('what an identity provider’s address may be', () => {
  it('accepts an ordinary https deployment, with or without a path', () => {
    expect(isProviderUrl('https://outline.acme.example')).toBe(true)
    expect(isProviderUrl('https://wiki.example/outline')).toBe(true)
    expect(isProviderUrl('https://wiki.example:8443/outline')).toBe(true)
  })

  it('refuses http, because the authorization code travels over it', () => {
    expect(isProviderUrl('http://outline.acme.example')).toBe(false)
  })

  it('refuses a userinfo section', () => {
    // `https://console.example@evil.example/` names `evil.example` and
    // reads to a human as the first host. It is the one form where
    // refusing to parse is the difference between what an administrator
    // reviewing the value sees and what a browser does.
    expect(isProviderUrl('https://console.example@evil.example/')).toBe(false)
    expect(isProviderUrl('https://user:pass@evil.example/')).toBe(false)
  })

  it('refuses a query or a fragment', () => {
    // `authorize_url` builds its own query string; a fragment never
    // reaches a server at all.
    expect(isProviderUrl('https://outline.acme.example?a=1')).toBe(false)
    expect(isProviderUrl('https://outline.acme.example#top')).toBe(false)
  })

  it('refuses surrounding whitespace rather than trimming it', () => {
    expect(isProviderUrl(' https://outline.acme.example')).toBe(false)
    expect(isProviderUrl('https://outline.acme.example ')).toBe(false)
    expect(isProviderUrl('https://outline acme.example')).toBe(false)
  })

  it('refuses anything that is not a URL at all', () => {
    for (const value of ['', 'outline.acme.example', 'javascript:alert(1)', 'https://']) {
      expect(isProviderUrl(value), `${value} was accepted`).toBe(false)
    }
  })
})

describe('the link a guild hands out', () => {
  it('is /g/{slug}/sign-in', () => {
    expect(signInPath('acme')).toBe('/g/acme/sign-in')
  })

  it('joins onto an origin without doubling the slash', () => {
    expect(signInUrl('https://sturnus.example', 'acme'))
      .toBe('https://sturnus.example/g/acme/sign-in')
    expect(signInUrl('https://sturnus.example/', 'acme'))
      .toBe('https://sturnus.example/g/acme/sign-in')
  })

  it('sends the browser to the login endpoint with the guild in the query', () => {
    // `GET /api/auth/login` reads no cookie -- there is no session yet, that
    // is what login is for -- so the guild goes in the URL, which is the
    // only place it can be before the round trip starts.
    expect(loginUrl('acme')).toBe('/api/auth/login?guild=acme')
  })

  it('recognises its own shape back, so the allowlist and the builder agree', () => {
    // Two files with one route shape between them are two shapes waiting to
    // disagree, and the way they would disagree is that a link somebody
    // handed out stops being treated as public and bounces its followers to
    // a sign-in page they have no way to use.
    expect(isGuildSignInPath(signInPath('acme'))).toBe(true)
    expect(isGuildSignInPath('/g/acme/sign-in/')).toBe(true)
  })

  it('treats every slug as public, including the ones that are not slugs', () => {
    // Deliberately looser than `slugProblem`. A middleware that sent a
    // malformed name to the ordinary sign-in page and a registered one to
    // the guild page would be a one-request oracle for which organisations
    // use this service -- which is the disclosure the whole design refuses.
    for (const path of ['/g/ACME/sign-in', '/g/1289374650912837465/sign-in', '/g/x/sign-in']) {
      expect(isGuildSignInPath(path), `${path} would have needed a session`).toBe(true)
    }
  })

  it('is not a licence for anything else under /g', () => {
    for (const path of ['/g//sign-in', '/g/acme', '/g/acme/settings', '/sign-in', '/admin/queue']) {
      expect(isGuildSignInPath(path), `${path} was treated as public`).toBe(false)
    }
  })

  it('escapes a slug that somehow is not one', () => {
    // Nothing this console writes can produce such a slug, and the API
    // cannot store one. This is about the *read* path: a row that predates
    // a rule, or a hand-edited database, must not put an unescaped path
    // segment into an anchor.
    expect(signInPath('a/../b')).toBe('/g/a%2F..%2Fb/sign-in')
    expect(loginUrl('a&b=c')).toBe('/api/auth/login?guild=a%26b%3Dc')
  })
})

describe('what state a guild’s link is in', () => {
  it('says there is no link when nothing is registered', () => {
    const state = linkState(null)
    expect(state.tone).toBe('absent')
    expect(state.showLink).toBe(false)
  })

  it('says a registration without a secret is not yet a working link', () => {
    // The state between step 2 and step 3 of the runbook. The link is real
    // and published and answers exactly as a name nobody registered -- and
    // an interface that drew it as "configured" would have somebody hand it
    // out and lose an afternoon to it.
    const state = linkState({ ...REGISTERED, hasSecret: false })
    expect(state.tone).toBe('incomplete')
    expect(state.showLink).toBe(true)
    expect(state.detail.key).toBe('admin.signInLink.stateIncompleteDetail')
  })

  it('says a registration with a secret is live, and names the slug', () => {
    const state = linkState(REGISTERED)
    expect(state.tone).toBe('live')
    expect(state.detail.params?.slug).toBe('acme')
  })

  it('gives each state its own heading rather than one heading and a colour', () => {
    const headings = [linkState(null), linkState({ ...REGISTERED, hasSecret: false }), linkState(REGISTERED)]
      .map((state) => state.headingKey)
    expect(new Set(headings).size).toBe(3)
  })
})

describe('what the registration form edits', () => {
  it('has no field that could hold a credential', () => {
    // The load-bearing assertion of this file. A secret field added "just
    // for the add case" is how saving a change of base URL comes to clear a
    // working credential.
    const fields = Object.keys(emptyClientDraft())
    expect(fields).toEqual([
      'slug',
      'provider',
      'baseUrl',
      'clientId',
      'redirectMode',
      'redirectUri',
    ])
    for (const field of fields) {
      expect(field.toLowerCase()).not.toContain('secret')
    }
  })

  it('sends no credential in the body either', () => {
    expect(Object.keys(clientDraftBody(ready())).sort()).toEqual([
      'base_url',
      'client_id',
      'provider',
      'redirect_uri',
      'slug',
    ])
  })

  it('starts a guild on this deployment’s own callback', () => {
    // `redirect_uri: null` is what nearly every guild wants: the console
    // callback this deployment is already configured with.
    expect(emptyClientDraft().redirectMode).toBe('default')
    expect(clientDraftBody(emptyClientDraft()).redirect_uri).toBeNull()
  })

  it('sends a guild’s own callback when it has one', () => {
    const body = clientDraftBody(ready({ redirectMode: 'custom', redirectUri: 'https://acme.example/cb' }))
    expect(body.redirect_uri).toBe('https://acme.example/cb')
  })

  it('forgets a typed callback the moment the default is chosen again', () => {
    // Otherwise a reader who ticks the box back would save a redirect URI
    // that is no longer on screen.
    const body = clientDraftBody(ready({ redirectMode: 'default', redirectUri: 'https://acme.example/cb' }))
    expect(body.redirect_uri).toBeNull()
  })

  it('reads an existing registration back into a draft, both ways round', () => {
    expect(clientDraftOf(REGISTERED).redirectMode).toBe('default')
    expect(clientDraftOf({ ...REGISTERED, redirectUri: 'https://acme.example/cb' })).toMatchObject({
      redirectMode: 'custom',
      redirectUri: 'https://acme.example/cb',
    })
  })

  it('does not rewrite the provider of a registration it does not understand', () => {
    // The `directory.ts` rule about an unresolved snowflake, applied to a
    // word: a form that hard-coded the constant into the body would
    // silently re-register somebody else's provider as Outline.
    expect(clientDraftBody(clientDraftOf({ ...REGISTERED, provider: 'keycloak' })).provider).toBe('keycloak')
  })

  it('does not trim a slug or a base URL on the way out', () => {
    // Nothing on this path normalises. The value stored is the value typed,
    // because the link an administrator hands out carries whichever of them
    // they typed.
    const body = clientDraftBody(ready({ slug: ' acme ', baseUrl: ' https://a.example ' }))
    expect(body.slug).toBe(' acme ')
    expect(body.base_url).toBe(' https://a.example ')
  })

  it('trims the client id, which is pasted rather than read', () => {
    expect(clientDraftBody(ready({ clientId: '  client-abc\n' })).client_id).toBe('client-abc')
  })
})

describe('what is wrong with a draft', () => {
  it('finds nothing wrong with a complete one', () => {
    expect(clientDraftProblems(ready())).toEqual([])
    expect(isClientDraftReady(ready())).toBe(true)
  })

  it('puts each complaint on the field it is about', () => {
    // So the page can render it beside that field rather than as a list of
    // grievances at the bottom that nobody can match to an input.
    const problems = clientDraftProblems({
      slug: 'Acme',
      provider: 'keycloak',
      baseUrl: 'http://x.example',
      clientId: '',
      redirectMode: 'custom',
      redirectUri: 'nonsense',
    })
    expect(problems.map((problem) => problem.field)).toEqual([
      'slug',
      'provider',
      'baseUrl',
      'clientId',
      'redirectUri',
    ])
  })

  it('objects to a provider this deployment cannot exchange with', () => {
    // Storing one would produce a guild whose link is permanently and
    // silently broken, which is why the API refuses it rather than
    // accepting a value it might one day support.
    expect(clientProblemFor(clientDraftProblems(ready({ provider: 'keycloak' })), 'provider')?.key)
      .toBe('admin.signInLink.providerUnsupported')
  })

  it('says an empty base URL is empty rather than malformed', () => {
    expect(clientProblemFor(clientDraftProblems(ready({ baseUrl: '' })), 'baseUrl')?.key)
      .toBe('admin.signInLink.baseUrlEmpty')
    expect(clientProblemFor(clientDraftProblems(ready({ baseUrl: 'http://x.example' })), 'baseUrl')?.key)
      .toBe('admin.signInLink.urlShape')
  })

  it('checks a redirect URI only when the guild is supplying one', () => {
    // The default is `null`, and a text box left blank beside a ticked
    // "use this deployment's callback" is not a mistake to complain about.
    expect(clientDraftProblems(ready({ redirectMode: 'default', redirectUri: '' }))).toEqual([])
    expect(clientProblemFor(clientDraftProblems(ready({ redirectMode: 'custom' })), 'redirectUri')?.key)
      .toBe('admin.signInLink.redirectEmpty')
  })

  it('refuses values longer than the columns behind them', () => {
    expect(isClientDraftReady(ready({ clientId: 'x'.repeat(513) }))).toBe(false)
    expect(isClientDraftReady(ready({ baseUrl: `https://x.example/${'y'.repeat(2048)}` }))).toBe(false)
  })

  it('never complains that a name is taken', () => {
    // It cannot know, and asking would be the oracle. Every name that is
    // spelled like a slug passes here and is left to the API's 409.
    for (const slug of ['acme', 'login', 'api', 'sturnus']) {
      expect(isClientDraftReady(ready({ slug })), `${slug} was refused locally`).toBe(true)
    }
  })
})

describe('the credential', () => {
  it('says only whether one is stored', () => {
    const state = clientSecretState(REGISTERED)
    expect(state.stored).toBe(true)
    expect(Object.keys(state)).toEqual(['stored', 'statusKey', 'actionKey', 'canClear'])
  })

  it('offers nothing to clear when there is nothing stored', () => {
    expect(clientSecretState({ ...REGISTERED, hasSecret: false }).canClear).toBe(false)
  })

  it('calls it storing the first time and replacing afterwards', () => {
    expect(clientSecretState({ ...REGISTERED, hasSecret: false }).actionKey)
      .toBe('admin.signInLink.secretSet')
    expect(clientSecretState(REGISTERED).actionKey).toBe('admin.signInLink.secretReplace')
  })

  it('refuses an empty box, so that emptiness cannot read as clearing', () => {
    expect(canSubmitClientSecret('')).toBe(false)
    expect(canSubmitClientSecret('s')).toBe(true)
  })

  it('refuses more than the column holds, rather than sending it to be refused', () => {
    // `apiError` keeps only the status, so a 400 from the API is a refusal
    // this console cannot explain.
    expect(canSubmitClientSecret('x'.repeat(MAX_SECRET_LENGTH))).toBe(true)
    expect(canSubmitClientSecret('x'.repeat(MAX_SECRET_LENGTH + 1))).toBe(false)
  })
})

describe('where the requests go', () => {
  it('addresses the registration and its credential separately', () => {
    // Two routes rather than a field, which is what makes "save the
    // registration" a request that demonstrably cannot carry a credential.
    expect(clientPath(GUILD)).toBe(`/guilds/${GUILD}/oauth-client`)
    expect(clientSecretPath(GUILD)).toBe(`/guilds/${GUILD}/oauth-client/secret`)
  })

  it('escapes the guild id', () => {
    expect(clientPath('../1')).toBe('/guilds/..%2F1/oauth-client')
  })
})

describe('when a request does not work', () => {
  const failing = (status: number) => ({ status })

  it('reads a 404 as "nothing is registered yet"', () => {
    // The API gives one 404 to a guild with no client, a guild nobody
    // administers and a guild that does not exist. The page only ever asks
    // about guilds the viewer administers, so the reading it can act on is
    // the first -- which is a state, not an error.
    expect(isMissingRegistration(failing(404))).toBe(true)
    expect(isMissingRegistration(failing(500))).toBe(false)
    expect(isMissingRegistration(null)).toBe(false)
  })

  it('says a name is unavailable without saying why', () => {
    // Whether it is held by another guild or reserved by the deployment is
    // exactly what the API refuses to disclose, and a console that expanded
    // the sentence would undo that from the outside.
    const message = describeClientError(failing(409))
    expect(message).toEqual({ key: 'admin.signInLink.errorNameTaken' })
    expect(message.params).toBeUndefined()
  })

  it('tells an unreachable API apart from one that said no', () => {
    expect(describeClientError(failing(0)).key).toBe('admin.signInLink.errorUnreachable')
    expect(describeClientError(new Error('boom')).key).toBe('admin.signInLink.errorUnreachable')
  })

  it('carries an unexpected status as a string, not a quantity', () => {
    // `503` is not a number of anything, and `say` would write a quantity
    // with the locale's grouping.
    expect(describeClientError(failing(503))).toEqual({
      key: 'admin.signInLink.errorStatus',
      params: { status: '503' },
    })
  })

  it('has a sentence for every status these five routes can answer', () => {
    for (const status of [400, 401, 404, 409]) {
      expect(describeClientError(failing(status)).key)
        .not.toBe('admin.signInLink.errorStatus')
    }
  })
})
