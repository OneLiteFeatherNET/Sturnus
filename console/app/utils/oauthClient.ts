/**
 * A guild's own sign-in link, as far as the console is allowed to decide it.
 *
 * `#147` gave a guild the ability to sign its people in against its own
 * Outline rather than against the one this deployment is configured with,
 * and left it reachable only from `curl`: the `guild_oauth_client` rows
 * exist, five routes write them, and nothing in the browser ever has. This
 * module holds every decision the page that does makes, so that the page
 * itself is layout and request plumbing — the rule the rest of `app/utils`
 * follows, and the reason those rules can be tested without mounting
 * anything.
 *
 * Five things here are worth arguing for rather than reading past.
 *
 * **1. There is no shape here that can carry a client secret.**
 * {@link GuildOAuthClient} has `hasSecret` and nowhere to put a value,
 * because the API's read model has `has_secret` and nowhere to put a value.
 * {@link ClientDraft} — what the registration form edits and submits — has
 * no credential field at all, so "saving a change of base URL wiped the
 * secret" is not a bug this console avoids, it is a request this console
 * cannot construct. That is `~/utils/exportTargets`' argument, made again
 * because the failure it prevents is the same one and the stakes here are
 * higher: this credential decides who gets a session at all.
 *
 * **2. This console checks the *shape* of a slug and never its
 * availability.** `routes_oauth` answers 400 to a slug that is not spelled
 * like one and 409 to a slug that is spelled correctly and is not this
 * guild's to have — and it gives that same 409 whether the name is held by
 * another guild or reserved by the deployment, *deliberately*, so that
 * which of the two it was cannot be read off the reply. A console that
 * carried its own copy of `RESERVED_SLUGS` would answer "that name is
 * reserved" without a request, which re-introduces exactly the distinction
 * the API collapsed, in the one place an administrator reads. So
 * {@link slugProblem} mirrors `has_slug_shape` and stops there, and 409 is
 * rendered as the API's own one answer: pick a different name. **Nothing
 * anywhere in this console asks whether a slug is free.**
 *
 * **3. Nothing is normalised.** `Acme` is refused rather than lowercased,
 * matching `is_valid_slug`, and for its reason: a slug quietly rewritten on
 * the way into the table is a slug the administrator does not recognise in
 * the link they handed out. The same goes for the two URLs — they are
 * tested and never rewritten, which is why {@link isProviderUrl} refuses a
 * value with whitespace around it instead of trimming it.
 *
 * **4. A registration without a secret is a link that answers exactly as an
 * unknown one.** That is not a defect to be hidden behind a spinner; it is
 * the state an administrator is in between step 2 and step 3 of
 * §6.2.12, and {@link linkState} says so out loud. A page that drew a
 * half-configured link as working would have somebody hand it out.
 *
 * **5. The guild's own client governs the console sign-in and nothing
 * else.** The Discord account-link flow stays on the environment-configured
 * client permanently: `api` holds the master key and `link` does not, and
 * `charts/sturnus/templates/_helpers.tpl` refuses to render it onto that
 * component at all. {@link SCOPE_NOTE_KEY} is that sentence, kept here
 * beside the rest of the contract rather than loose in a template, so that
 * an interface which implied otherwise would have to delete an argument to
 * do it.
 *
 * Every sentence here is a translation key or a {@link Message}, never
 * prose: a pure function returns data. See `i18n/README.md`.
 *
 * **Why half of these names begin with `client`.** Nuxt auto-imports every
 * export under `app/utils`, so two modules exporting one name is not a
 * matter of taste — the build picks one of them and warns, and whichever it
 * picks is what any file that did not import explicitly gets. This module
 * and `~/utils/exportTargets` answer very similar questions about two very
 * different credentials, so the overlap was total: `secretState`,
 * `draftBody`, `draftProblems` and six more. They are prefixed here rather
 * than there because the older module's name is the one already written
 * into pages, and because "the draft body of *what*" is a question these
 * names should have been answering anyway.
 */
import type { Message } from './message'

/* -------------------------------------------------------------------- */
/* What the API sends                                                    */
/* -------------------------------------------------------------------- */

/**
 * One guild's console sign-in client, as anything outside may see it.
 *
 * `guildId` is a string because a Discord snowflake exceeds JavaScript's
 * safe integer range, where a JSON number silently loses its last digits
 * and produces an id that looks right and names nobody. The API sends it as
 * a string for that reason; this keeps it one.
 *
 * `redirectUri` is `null` for a guild using this deployment's own callback,
 * which is what nearly every guild wants. Present-and-null rather than
 * absent, so "the default" and "an API that does not send this field" stay
 * distinguishable.
 *
 * **There is nowhere here to put the secret.** `hasSecret` is the whole of
 * what any response says about one.
 */
export interface GuildOAuthClient {
  guildId: string
  slug: string
  provider: string
  baseUrl: string
  clientId: string
  redirectUri: string | null
  /** That a credential is stored. Never the credential. */
  hasSecret: boolean
  /** ISO-8601, or `null` when the API sent something unreadable. */
  createdAt: string | null
  updatedAt: string | null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asText(value: unknown): string | null {
  if (value === null || value === undefined) return null
  return typeof value === 'string' ? value : String(value)
}

/**
 * The registration in a `GET`, `PUT` or secret-write response, or `null`.
 *
 * `null` for a payload with no slug in it, because the slug is the whole
 * point of the row: a registration this console rendered with a blank name
 * would be a sign-in link nobody could read off the screen, and every
 * remedy for it — re-register, remove — is reachable anyway from the state
 * where there is no registration at all.
 *
 * Tolerant of everything else, the way `parseTarget` is. A registration
 * whose base URL came back empty is still a registration, and drawing it is
 * how somebody finds out it needs fixing.
 */
export function parseClient(payload: unknown): GuildOAuthClient | null {
  if (!isRecord(payload)) return null
  const held = isRecord(payload.oauth_client) ? payload.oauth_client : payload
  const slug = asText(held.slug)
  if (slug === null || slug === '') return null
  return {
    guildId: asText(payload.guild_id) ?? asText(held.guild_id) ?? '',
    slug,
    provider: asText(held.provider) ?? '',
    baseUrl: asText(held.base_url) ?? '',
    clientId: asText(held.client_id) ?? '',
    // Absent and null are one answer here, and it is the right one: both
    // mean "this deployment's own callback", which is what the field means
    // when the API omits it and what it means when the API sends null.
    redirectUri: asText(held.redirect_uri),
    // Absent is false rather than true. A registration this console cannot
    // tell the state of is better drawn as not yet live — the reader then
    // supplies a secret and learns the truth — than drawn as a working link
    // that answers 404 to everybody who follows it.
    hasSecret: held.has_secret === true,
    createdAt: asText(held.created_at),
    updatedAt: asText(held.updated_at),
  }
}

/* -------------------------------------------------------------------- */
/* The one provider this deployment can exchange with                    */
/* -------------------------------------------------------------------- */

/**
 * The provider `routes_oauth` accepts, which is the one `console.auth` can
 * complete a code exchange against.
 *
 * There is no picker over this and there should not be: a dropdown with one
 * row is a control that asks a question with one answer. `_registration`
 * refuses anything else with a 400 rather than storing it, and it says why
 * — a registration against a provider nothing here can exchange with is a
 * guild whose link is permanently and silently broken.
 */
export const PROVIDER_OUTLINE = 'outline'

/* -------------------------------------------------------------------- */
/* What a slug is                                                        */
/* -------------------------------------------------------------------- */

/** Short enough to be typed and read back over a chat message, long enough
 *  to name an organisation. `MIN_SLUG_LENGTH` in `domain/oauth_clients.py`. */
export const MIN_SLUG_LENGTH = 3
export const MAX_SLUG_LENGTH = 32

/**
 * Lowercase, hyphen-separated words, beginning with a letter.
 *
 * The leading letter is the rule that costs the most and buys the most: a
 * Discord snowflake is digits, and `/g/1289374650912837465/sign-in` and a
 * guild id in a path are the same string to whoever is reading the link.
 * Requiring a letter first makes a slug and an id unconfusable rather than
 * merely unlikely to be confused.
 *
 * Anchored at both ends with no `m` flag, which in JavaScript means the
 * whole string — the equivalent of Python's `fullmatch`.
 */
const SLUG_SHAPE = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/

/**
 * Why this cannot be a sign-in name, or `null`.
 *
 * A second copy of a rule the API enforces, and — like `acceptsTarget` next
 * door — a courtesy rather than a control. `apiError.sanitiseFetchError`
 * keeps nothing of a refusal but its status, on purpose, so a rule only the
 * API checks is a rule whose reason the reader never sees: they get a bare
 * 400 where they wanted to be told they had typed a capital letter.
 *
 * Three different complaints rather than one, because they are three
 * different typing mistakes and "the sign-in name is invalid" tells nobody
 * which of them they made.
 *
 * **Shape only.** Whether the name is free is not asked here and is not
 * asked anywhere — see the note at the top of this module.
 */
export function slugProblem(slug: string): Message | null {
  if (slug === '') return { key: 'admin.signInLink.slugEmpty' }
  if (slug.length < MIN_SLUG_LENGTH || slug.length > MAX_SLUG_LENGTH) {
    return {
      key: 'admin.signInLink.slugLength',
      params: { min: String(MIN_SLUG_LENGTH), max: String(MAX_SLUG_LENGTH) },
    }
  }
  if (!SLUG_SHAPE.test(slug)) return { key: 'admin.signInLink.slugShape' }
  return null
}

/* -------------------------------------------------------------------- */
/* What an address is                                                    */
/* -------------------------------------------------------------------- */

/** `_MAX_URL` and `_MAX_CLIENT_ID` in `routes_oauth`. Not a claim about
 *  what any provider issues — they are what keeps a `Text` column from
 *  being a place to store a megabyte through an authenticated endpoint. */
export const MAX_URL_LENGTH = 2048
export const MAX_CLIENT_ID_LENGTH = 512
/** `_MAX_SECRET`. The one bound this console applies to a value it must
 *  never otherwise look at. */
export const MAX_SECRET_LENGTH = 1024

/**
 * Whether this may be a guild's identity-provider base URL or its callback.
 *
 * `is_provider_url` in `domain/oauth_clients.py`, held to the same four
 * rules and for the same reasons. Both of these are addresses an
 * administrator of one guild chooses and other people's browsers follow.
 *
 * - **`https` only.** The authorization code, and the whole consent step,
 *   travel over it.
 * - **No userinfo.** `https://console.example@evil.example/` is a valid URL
 *   naming `evil.example` that reads to a human as the first host. It is
 *   the one form where refusing to parse is the difference between what an
 *   administrator reviewing the value sees and what a browser does — which
 *   is exactly why the check is worth having on the screen where the value
 *   is typed, and not only in the API that stores it.
 * - **No query and no fragment.** `authorize_url` builds its own query
 *   string, and a fragment never reaches a server at all.
 *
 * A path is allowed: an Outline behind `https://wiki.example/outline` is an
 * ordinary deployment.
 *
 * Whitespace around the value is refused rather than trimmed, because
 * nothing on this path normalises: the value stored is the value typed.
 */
export function isProviderUrl(value: string): boolean {
  if (value !== value.trim() || /\s/.test(value)) return false
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    return false
  }
  return (
    parsed.protocol === 'https:'
    && parsed.hostname !== ''
    && parsed.username === ''
    && parsed.password === ''
    && parsed.search === ''
    && parsed.hash === ''
  )
}

/* -------------------------------------------------------------------- */
/* Where the link goes, and whether it works                             */
/* -------------------------------------------------------------------- */

/**
 * The path a guild hands out.
 *
 * `/g/{slug}/sign-in`, the shape `domain/oauth_clients.py` names and the
 * one this console now actually serves. It is a path segment rather than
 * `?guild=` in a link an administrator distributes for one reason worth
 * stating: the API endpoint underneath it, `/api/auth/login?guild=…`, is a
 * redirect with no page — somebody who follows it while the registration is
 * half-finished meets a JSON body, and somebody who follows it while it is
 * finished never sees this deployment at all. A page in between is where
 * the product gets to say whose sign-in this is.
 */
export function signInPath(slug: string): string {
  return `/g/${encodeURIComponent(slug)}/sign-in`
}

/**
 * Whether a path is a guild's sign-in page.
 *
 * Kept beside {@link signInPath} rather than in the middleware that asks
 * the question, because a builder and a matcher for one route shape in two
 * files are two shapes waiting to disagree — and the way they would
 * disagree here is that a link an administrator handed out stops being
 * recognised as public and bounces its followers to a sign-in page they
 * have no way to use.
 *
 * Deliberately looser than {@link slugProblem}: **any** segment counts.
 * A middleware that only let well-formed slugs through would send `/g/ACME/
 * sign-in` to the ordinary sign-in page and a registered one to the guild
 * page, and the difference between those two screens is the difference the
 * whole design refuses to expose. Every well-formed name and every
 * malformed one reach the same page, which then hands them all to the same
 * endpoint, which answers all of them with the same 404.
 */
export function isGuildSignInPath(path: string): boolean {
  return /^\/g\/[^/]+\/sign-in\/?$/.test(path)
}

/** The link in full, for the box an administrator copies out of. `origin`
 *  is the console's own, which `useRequestURL` answers identically on the
 *  server and in the browser — so the value does not change under the
 *  reader on hydration. */
export function signInUrl(origin: string, slug: string): string {
  return `${origin.replace(/\/+$/, '')}${signInPath(slug)}`
}

/**
 * Where the guild sign-in page sends the browser.
 *
 * A plain navigation and never a fetch: the OAuth flow leaves this origin
 * and comes back, and an XHR cannot follow it. The same reasoning
 * `pages/sign-in.vue` gives for its own anchor.
 */
export function loginUrl(slug: string): string {
  return `/api/auth/login?guild=${encodeURIComponent(slug)}`
}

/**
 * What state a guild's link is in, in three words and a sentence.
 *
 * The middle state is the one this exists for. Between registering a client
 * and supplying its secret, the link is real, published, and answers
 * **exactly** as a name nobody has ever registered — the same 404, the same
 * body — because telling those two apart is precisely what §2.2 refuses to
 * let anybody do. That is the design working, not a fault, and an interface
 * that drew it as "configured" would have somebody hand the link out and
 * spend an afternoon on why their colleagues cannot sign in.
 */
export type LinkTone = 'absent' | 'incomplete' | 'live'

export interface LinkState {
  tone: LinkTone
  headingKey: string
  detail: Message
  /** Whether there is a link worth putting on screen to be copied. */
  showLink: boolean
}

export function linkState(client: GuildOAuthClient | null): LinkState {
  if (client === null) {
    return {
      tone: 'absent',
      headingKey: 'admin.signInLink.stateAbsentHeading',
      detail: { key: 'admin.signInLink.stateAbsentDetail' },
      showLink: false,
    }
  }
  if (!client.hasSecret) {
    return {
      tone: 'incomplete',
      headingKey: 'admin.signInLink.stateIncompleteHeading',
      detail: { key: 'admin.signInLink.stateIncompleteDetail' },
      showLink: true,
    }
  }
  return {
    tone: 'live',
    headingKey: 'admin.signInLink.stateLiveHeading',
    detail: { key: 'admin.signInLink.stateLiveDetail', params: { slug: client.slug } },
    showLink: true,
  }
}

/**
 * The sentence that keeps this page honest about its own reach.
 *
 * Rendered on the page, and kept here rather than loose in a template
 * because it is a statement about the architecture and not a caption: a
 * guild's client governs the **console sign-in** and never the Discord
 * account link, which stays on the environment-configured client
 * permanently. `link` does not hold the master key and cannot unwrap a
 * guild's secret; the chart's `_helpers.tpl` refuses to render it onto that
 * component at all. An interface that implied a guild could bring its own
 * client for `/link` would be promising something the deployment is built
 * to prevent.
 */
export const SCOPE_NOTE_KEY = 'admin.signInLink.scopeNote'

/* -------------------------------------------------------------------- */
/* The registration form                                                 */
/* -------------------------------------------------------------------- */

/** Whether the guild comes back to this deployment's own callback — which
 *  is what nearly every guild wants and what `redirect_uri: null` means —
 *  or to one of its own. Two named states rather than an empty string
 *  standing in for the default, because "" and null are the same value in a
 *  text box and different values in this API. */
export type RedirectMode = 'default' | 'custom'

/**
 * What the registration form edits.
 *
 * **There is no secret field, and its absence is the design.** `PUT` on a
 * registration does not touch the stored secret — the API gives the
 * credential two routes of its own precisely so that changing a base URL
 * cannot clear it — so a password box on this form would either lie about
 * what Save does or wipe a credential every time somebody corrected a
 * client id. This type has nowhere to put one, so that failure is
 * unrepresentable rather than merely avoided. `SignInClientSecret` is the
 * control that writes it, with its own request.
 *
 * `provider` is carried rather than chosen. There is one value this
 * deployment accepts and a dropdown with one row asks a question with one
 * answer — but a form that dropped the field and hard-coded the constant
 * into the body would silently rewrite the provider of a registration this
 * console does not understand, which is `directory.ts`' rule about an
 * unresolved snowflake applied to a word.
 */
export interface ClientDraft {
  slug: string
  provider: string
  baseUrl: string
  clientId: string
  redirectMode: RedirectMode
  redirectUri: string
}

/** A blank registration, ready to be filled in. `default` because
 *  `redirect_uri: null` is what §6.2.12 says nearly every guild wants. */
export function emptyClientDraft(): ClientDraft {
  return {
    slug: '',
    provider: PROVIDER_OUTLINE,
    baseUrl: '',
    clientId: '',
    redirectMode: 'default',
    redirectUri: '',
  }
}

/** An existing registration, ready to be changed. */
export function clientDraftOf(client: GuildOAuthClient): ClientDraft {
  return {
    slug: client.slug,
    provider: client.provider,
    baseUrl: client.baseUrl,
    clientId: client.clientId,
    redirectMode: client.redirectUri === null ? 'default' : 'custom',
    redirectUri: client.redirectUri ?? '',
  }
}

/** Which field a complaint is about, so the page can put it beside that
 *  field rather than in a list of grievances at the bottom. */
export type ClientDraftField = 'slug' | 'provider' | 'baseUrl' | 'clientId' | 'redirectUri'

export interface ClientDraftProblem {
  field: ClientDraftField
  message: Message
}

/**
 * What is wrong with a draft, in the order the fields are read.
 *
 * Every rule here is one `_registration` already enforces. Restating them
 * is the courtesy argued for above {@link slugProblem}, and it has a second
 * effect worth naming: once this returns nothing, a 400 from these routes
 * can only mean that this console and the deployment disagree about what a
 * registration is — which is a different sentence from any of the five
 * below, and {@link describeClientError} says so.
 *
 * The one rule that is **not** restated is availability. See the top of
 * this module.
 */
export function clientDraftProblems(draft: ClientDraft): ClientDraftProblem[] {
  const problems: ClientDraftProblem[] = []

  const slug = slugProblem(draft.slug)
  if (slug !== null) problems.push({ field: 'slug', message: slug })

  if (draft.provider !== PROVIDER_OUTLINE) {
    problems.push({
      field: 'provider',
      message: { key: 'admin.signInLink.providerUnsupported', params: { provider: draft.provider } },
    })
  }

  if (draft.baseUrl === '') {
    problems.push({ field: 'baseUrl', message: { key: 'admin.signInLink.baseUrlEmpty' } })
  } else if (draft.baseUrl.length > MAX_URL_LENGTH || !isProviderUrl(draft.baseUrl)) {
    problems.push({ field: 'baseUrl', message: { key: 'admin.signInLink.urlShape' } })
  }

  if (draft.clientId.trim() === '') {
    problems.push({ field: 'clientId', message: { key: 'admin.signInLink.clientIdEmpty' } })
  } else if (draft.clientId.length > MAX_CLIENT_ID_LENGTH) {
    problems.push({ field: 'clientId', message: { key: 'admin.signInLink.clientIdLong' } })
  }

  if (draft.redirectMode === 'custom') {
    if (draft.redirectUri === '') {
      problems.push({ field: 'redirectUri', message: { key: 'admin.signInLink.redirectEmpty' } })
    } else if (draft.redirectUri.length > MAX_URL_LENGTH || !isProviderUrl(draft.redirectUri)) {
      problems.push({ field: 'redirectUri', message: { key: 'admin.signInLink.urlShape' } })
    }
  }

  return problems
}

/** The complaint about one field, or `null`. */
export function clientProblemFor(
  problems: readonly ClientDraftProblem[],
  field: ClientDraftField,
): Message | null {
  return problems.find((problem) => problem.field === field)?.message ?? null
}

/** Whether a draft may be submitted at all. */
export function isClientDraftReady(draft: ClientDraft): boolean {
  return clientDraftProblems(draft).length === 0
}

/**
 * The body a registration sends.
 *
 * Five fields, and there is no sixth. Nothing here trims: `_registration`
 * does not either, and a console that quietly trimmed a slug would store a
 * name the administrator does not recognise in the link they were told to
 * hand out. The one exception is the client id, which is an opaque token
 * pasted out of another application's interface — a trailing newline off a
 * clipboard is not a client id anybody chose, and it is the one field here
 * whose value nobody reads back off a screen.
 */
export function clientDraftBody(draft: ClientDraft): Record<string, unknown> {
  return {
    slug: draft.slug,
    provider: draft.provider,
    base_url: draft.baseUrl,
    client_id: draft.clientId.trim(),
    redirect_uri: draft.redirectMode === 'custom' ? draft.redirectUri : null,
  }
}

/* -------------------------------------------------------------------- */
/* The credential                                                        */
/* -------------------------------------------------------------------- */

/**
 * What the secret control may do, and what it may say.
 *
 * The whole of the design is in what is *not* here. There is no `value`, no
 * `masked`, no `reveal`: `PUT .../oauth-client/secret` is the only route
 * that writes this credential, `DELETE` on the same path is the only one
 * that forgets it, and no route anywhere returns it. A control that offered
 * to show it would be offering something the API cannot serve, and a masked
 * placeholder would be worse than that — a row of dots is a value, it says
 * how long the credential is, and it promises a button nothing can honour.
 *
 * `canClear` is separate from `canReplace` because clearing is a separate
 * act with a separate request, and because the alternative — a password box
 * rendered empty beside a stored credential and saved with the rest of a
 * form — silently wipes it every time somebody corrects a typo elsewhere.
 * {@link ClientDraft} has no secret field at all, so that failure is not
 * merely avoided, it is unrepresentable.
 */
export interface ClientSecretState {
  /** Whether a credential is stored. The only thing known about it. */
  stored: boolean
  statusKey: string
  /** Storing the first one, or replacing the one that is there. */
  actionKey: string
  /** Only where there is something to clear. */
  canClear: boolean
}

export function clientSecretState(client: GuildOAuthClient): ClientSecretState {
  return {
    stored: client.hasSecret,
    statusKey: client.hasSecret
      ? 'admin.signInLink.secretStored'
      : 'admin.signInLink.secretNone',
    actionKey: client.hasSecret
      ? 'admin.signInLink.secretReplace'
      : 'admin.signInLink.secretSet',
    canClear: client.hasSecret,
  }
}

/**
 * Whether a typed credential may be submitted.
 *
 * Empty is refused here as well as by the API, which answers 400 to `""` —
 * and refusing it in the control is what stops "save an empty box" from
 * looking like a way to clear one. Clearing has its own button, and it is
 * the only way.
 *
 * The upper bound is `_MAX_SECRET`, checked here so that a paste of the
 * wrong thing entirely is refused before it is sent somewhere that would
 * have to refuse it — and a refusal from the API is one this console cannot
 * explain, because `apiError` keeps only the status.
 */
export function canSubmitClientSecret(typed: string): boolean {
  return typed !== '' && typed.length <= MAX_SECRET_LENGTH
}

/* -------------------------------------------------------------------- */
/* Where the requests go                                                 */
/* -------------------------------------------------------------------- */

export function clientPath(guildId: string): string {
  return `/guilds/${encodeURIComponent(guildId)}/oauth-client`
}

export function clientSecretPath(guildId: string): string {
  return `${clientPath(guildId)}/secret`
}

/* -------------------------------------------------------------------- */
/* When a request does not work                                          */
/* -------------------------------------------------------------------- */

/**
 * Whether a failed read means "this guild has no registration".
 *
 * `GET` answers 404 for a guild with no client **and** for a guild the
 * caller does not administer **and** for a guild that does not exist — one
 * answer, deliberately, because whether a given guild has its own sign-in
 * is the fact §2.2 exists to keep undiscoverable. The page can be relaxed
 * about that: it only ever asks about guilds the viewer administers, so the
 * one reading it can act on is "there is nothing registered yet", which is
 * a state and not an error.
 */
export function isMissingRegistration(error: unknown): boolean {
  return (error as { status?: unknown } | null)?.status === 404
}

/**
 * Why a write failed, from its status and nothing else.
 *
 * The status is all there is: `sanitiseFetchError` keeps nothing from a
 * failed response but that, on purpose, so that no page can accidentally
 * render an internal hostname out of a `$fetch` error.
 *
 * The 409 is the interesting one. It means "that sign-in name is not
 * available", and this console says exactly that and no more — it does not
 * say whether the name is held by another guild or reserved by the
 * deployment, because the API refuses to say, and the reason it refuses is
 * that the held ones are what §2.2 does not want enumerable. A console that
 * expanded the sentence would undo the endpoint's discretion from the
 * outside.
 */
export function describeClientError(error: unknown): Message {
  const held = (error as { status?: unknown } | null)?.status
  const status = typeof held === 'number' ? held : null
  switch (status) {
    case 400:
      return { key: 'admin.signInLink.errorRefused' }
    case 401:
      return { key: 'admin.signInLink.errorSession' }
    case 404:
      return { key: 'admin.signInLink.errorGone' }
    case 409:
      return { key: 'admin.signInLink.errorNameTaken' }
    case 0:
    case null:
      return { key: 'admin.signInLink.errorUnreachable' }
    default:
      return { key: 'admin.signInLink.errorStatus', params: { status: String(status) } }
  }
}
