/**
 * Setting a guild up from the console: what the answer means, and what to say.
 *
 * `api` holds no Discord token and never will (Spec 13.2), so the console
 * cannot create the consent role, deny `Speak` to `@everyone` or register
 * the command tree. It writes an **intent** instead, and the bot's
 * ten-second reconcile tick makes it true and writes back what happened.
 * `docs/operations.md` §6.2.14 is the contract; this module is the part of
 * it that has to be said in an interface.
 *
 * Three properties of that mechanism decide everything in this file, and
 * none of them can be read off the payload by somebody who has not been
 * told:
 *
 * - **A failure is terminal.** The tick runs six times a minute forever, so
 *   an intent left unapplied after failing would retry a permission error
 *   against Discord's rate limiter just as often. One attempt settles it.
 *   There is no back-off, no retry and nothing to wait for: an
 *   administrator who has fixed the permission **asks again**, and that is
 *   a new row. So {@link reportRequest} says so on every failure rather
 *   than leaving a page to render "failed" beside a spinner.
 * - **The newest ask wins, outright.** Two administrators thirty seconds
 *   apart leave two rows; the bot applies the newer and settles the older
 *   as `superseded` **without acting on it**. Nothing went wrong to a
 *   superseded request, and a page that drew it in the failure colour would
 *   send somebody to check a permission that was never tested. It is a
 *   `neutral` tone here, and it says the words.
 * - **`pending` for more than a tick means the bot is not there.** A guild
 *   the bot has not joined has no gateway object to iterate, so its intents
 *   are never attempted. `bot.has_arrived` says which of the two it is, and
 *   {@link pickerState} keys off exactly that: an empty channel list means
 *   "this server has no voice channels" only once something has been
 *   mirrored, and means "nobody has looked yet" until then. One of those
 *   sends somebody to Discord to make a channel and the other sends them
 *   hunting for a bug that is not there.
 *
 * As everywhere in `app/utils`, the sentences are translation keys rather
 * than prose, and a decided sentence is a {@link Message}. See
 * `i18n/README.md`.
 */
import { type NamedRow, parseIdList, resolveChoice } from '~/utils/directory'
import type { Message } from '~/utils/message'

/* -------------------------------------------------------------------- */
/* The four statuses                                                     */
/* -------------------------------------------------------------------- */

/** No outcome has been written: the bot has not reached this row yet. */
export const PENDING = 'pending'

/** The bot did what was asked. */
export const APPLIED = 'applied'

/** The bot tried and could not, and `error` says what Discord answered.
 *  Terminal: there is no second attempt. */
export const FAILED = 'failed'

/** The bot never tried. A newer request replaced this one before the tick
 *  reached either. Terminal like the other two, and **not a failure**. */
export const SUPERSEDED = 'superseded'

/**
 * Whether the bot has finished with this row, whatever it did.
 *
 * Asked of the string rather than of a union on purpose. `outcome` is text
 * in the database rather than an enum precisely so that a value this build
 * has never seen is a row a reader can ignore instead of a write that fails
 * inside a reconcile tick — and a console that narrowed it back down to
 * four would hand that property back. Anything that is not `pending` is
 * settled, including a word written by a newer bot than this console.
 */
export function isSettled(status: string): boolean {
  return status !== PENDING
}

/* -------------------------------------------------------------------- */
/* Reading what the API sent                                             */
/* -------------------------------------------------------------------- */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** `null` stays `null`; anything else becomes the string it prints as.
 *  **Every Discord id is a string and stays one**: a snowflake exceeds
 *  `Number.MAX_SAFE_INTEGER`, so anything that round-trips through a
 *  JavaScript number hands back an id ending in other digits. */
function asText(value: unknown): string | null {
  if (value === null || value === undefined) return null
  return typeof value === 'string' ? value : String(value)
}

export interface SetupRequestView {
  /** A row id, and a string like every other id this API sends. */
  id: string
  /** `pending`, or whatever the bot wrote when it settled the row. Not
   *  narrowed to a union — see {@link isSettled}. */
  status: string
  /** The Discord id of whoever asked. */
  requestedBy: string
  requestedAt: string | null
  /** The list as the console offered it, so the same boxes can be ticked
   *  again without parsing a stored format. */
  channelIds: string[]
  consentRoleName: string | null
  /** When the bot finished with it, however it finished. */
  settledAt: string | null
  /** Free text the bot composed for a person to act on. Rendered, never
   *  keyed off. */
  error: string | null
}

export interface SetupState {
  guildId: string | null
  /**
   * Whether anything about this guild has ever been mirrored.
   *
   * The field the channel picker depends on, and the only thing that
   * separates "this server has no voice channels" from "nobody has looked
   * yet". Defaults to `false` for a payload that does not say, because
   * "not known to have arrived" is the honest reading of silence and it is
   * the one that makes the page wait rather than the one that makes it
   * claim a server has no rooms.
   */
  botHasArrived: boolean
  botSeenAt: string | null
  /** The most recent thing anybody asked for, settled or not. */
  request: SetupRequestView | null
}

export function parseSetupState(payload: unknown): SetupState {
  const bot = isRecord(payload) && isRecord(payload.bot) ? payload.bot : {}
  return {
    guildId: isRecord(payload) ? asText(payload.guild_id) : null,
    botHasArrived: bot.has_arrived === true,
    botSeenAt: asText(bot.seen_at),
    request: parseRequest(isRecord(payload) ? payload.request : null),
  }
}

function parseRequest(payload: unknown): SetupRequestView | null {
  if (!isRecord(payload)) return null
  const id = asText(payload.id)
  if (!id) return null
  const channels = Array.isArray(payload.channel_ids) ? payload.channel_ids : []
  return {
    id,
    // A row with no status at all is treated as still pending: it is the
    // reading that keeps the page waiting rather than the one that
    // declares an outcome nobody wrote.
    status: asText(payload.status) ?? PENDING,
    requestedBy: asText(payload.requested_by) ?? '',
    requestedAt: asText(payload.requested_at),
    channelIds: channels.map((each) => asText(each)).filter((each): each is string => Boolean(each)),
    consentRoleName: asText(payload.consent_role_name),
    settledAt: asText(payload.settled_at),
    error: asText(payload.error),
  }
}

export interface BotInvite {
  /** The `bot`-scope authorize URL, or `null` when this deployment has no
   *  `STURNUS_DISCORD_CLIENT_ID`. `null` is a configuration fact and not a
   *  failure, and the page says which. */
  url: string | null
  /** The permission bitmask, sent even when there is no link: it is what
   *  somebody ticks if they build the link by hand in Discord's own URL
   *  generator instead. */
  permissions: string | null
  scopes: string[]
}

export function parseInvite(payload: unknown): BotInvite {
  const scopes = isRecord(payload) && Array.isArray(payload.scopes) ? payload.scopes : []
  return {
    url: isRecord(payload) ? asText(payload.url) : null,
    permissions: isRecord(payload) ? asText(payload.permissions) : null,
    scopes: scopes.map((each) => asText(each)).filter((each): each is string => Boolean(each)),
  }
}

/* -------------------------------------------------------------------- */
/* What the channel picker is actually looking at                        */
/* -------------------------------------------------------------------- */

/**
 * Which of four things an empty channel list is.
 *
 * The distinction this whole page exists to draw. `waiting` and `empty`
 * both render as no channels to tick, and they are opposite instructions:
 * one says wait ten seconds, the other says go and make a voice channel.
 * `unreadable` is neither — the mirror is there and this console could not
 * read it — and rendering it as either would be a claim about a server
 * nobody has looked at.
 */
export type PickerState = 'waiting' | 'unreadable' | 'empty' | 'ready'

export function pickerState(input: {
  botHasArrived: boolean
  directoryFailed: boolean
  channelCount: number
}): PickerState {
  // Asked first, and before the failure: `has_arrived` is false exactly
  // while nothing has been mirrored, so a directory call that came back
  // empty for that guild answered correctly and there is nothing to
  // report as broken.
  if (!input.botHasArrived) return 'waiting'
  if (input.directoryFailed) return 'unreadable'
  return input.channelCount === 0 ? 'empty' : 'ready'
}

export interface StoredChannels {
  /** Ids the guild already records that the mirror can name. */
  recorded: string[]
  /**
   * Ids the guild records that the mirror has no row for.
   *
   * Never submitted, and that is not tidiness. The applier refuses a
   * channel a request names that it cannot see — "so it was not added" —
   * and a refusal is a `problem`, and one problem settles the whole intent
   * as `failed`. Carrying a stale stored id into every request would make
   * every request from that guild fail for a channel nobody asked about.
   */
  stale: string[]
}

/**
 * What this guild already records, split by whether the mirror can name it.
 *
 * `resolveChoice` rather than a lookup written here: how an unresolved
 * snowflake is presented is one decision this console has already taken,
 * in `~/utils/directory`, and a second answer to it is a second thing to
 * keep in step.
 */
export function storedChannels(
  stored: string | null | undefined,
  channels: readonly NamedRow[],
): StoredChannels {
  const recorded: string[] = []
  const stale: string[] = []
  for (const id of parseIdList(stored)) {
    if (resolveChoice(channels, id).resolved) recorded.push(id)
    else stale.push(id)
  }
  return { recorded, stale }
}

/**
 * The list a submission names: what is already recorded, plus what was ticked.
 *
 * The union rather than the ticks alone, because a setup request **adds**
 * to the stored list and never replaces it (`setup_apply._apply_one`).
 * Sending only the new ticks would work identically — the applier unions
 * them itself — but it would leave the payload saying something other than
 * what the guild will record, and this page shows the payload back.
 *
 * The consequence for the interface is the part worth stating: **unticking
 * an already-recorded channel here removes nothing.** That is why the page
 * renders those rows ticked and disabled rather than offering a control
 * that would do nothing. Removing a channel is `voice_channel_ids` on Bot
 * Settings, and the page says so beside them.
 */
export function submittedChannels(
  stored: StoredChannels,
  ticked: readonly string[],
): string[] {
  const ids = [...stored.recorded]
  for (const id of ticked) if (!ids.includes(id)) ids.push(id)
  return ids
}

/* -------------------------------------------------------------------- */
/* What the form will and will not send                                  */
/* -------------------------------------------------------------------- */

/** Discord's own limit on a role name, and the API's. Checked here as well
 *  so that somebody is told while they are still typing rather than by a
 *  request that is accepted, sits pending for a tick and comes back
 *  refused. */
export const MAX_ROLE_NAME = 100

export interface SetupDraft {
  /** Every channel the request names — {@link submittedChannels}' answer,
   *  not the ticks on their own. */
  channelIds: readonly string[]
  /** What to call the consent role. Blank means "do not name one", which
   *  keeps whatever role the guild already has. */
  consentRoleName: string
}

/**
 * Why this draft would be refused, or `null` if it would not be.
 *
 * This page never offers an action it knows will fail — the same rule the
 * destinations page follows — so the submit button is disabled and this
 * sentence sits beside it.
 */
export function draftProblem(draft: SetupDraft): Message | null {
  if (draft.channelIds.length === 0) return { key: 'admin.onboarding.needChannel' }
  if (draft.consentRoleName.trim().length > MAX_ROLE_NAME) {
    return { key: 'admin.onboarding.roleTooLong', params: { limit: String(MAX_ROLE_NAME) } }
  }
  return null
}

export interface SetupRequestBody {
  channel_ids: string[]
  consent_role_name: string | null
}

/**
 * The body to POST.
 *
 * A blank name becomes `null` rather than `""`: absent and null both mean
 * "do not name a role", which leaves whatever role the guild already has,
 * and omitting something must never be the destructive path (Spec 10.1).
 * The API refuses a blank string outright, which is the same decision from
 * the other side.
 */
export function requestBody(draft: SetupDraft): SetupRequestBody {
  const name = draft.consentRoleName.trim()
  return { channel_ids: [...draft.channelIds], consent_role_name: name === '' ? null : name }
}

/* -------------------------------------------------------------------- */
/* What the answer means                                                 */
/* -------------------------------------------------------------------- */

/**
 * How a request should read, in words as well as in colour.
 *
 * Six tones and not four, because `pending` is two different situations —
 * the bot is coming, or the bot is not there — and `superseded` is not a
 * failure however much it looks like one in a list of outcomes. Every tone
 * carries a badge word as well as a colour: a state a page communicates
 * only by being red is a state it has not communicated.
 */
export type RequestTone = 'waiting' | 'stalled' | 'good' | 'bad' | 'neutral' | 'unknown'

export function requestTone(status: string, botHasArrived: boolean): RequestTone {
  switch (status) {
    case PENDING:
      // The whole of the third property, in one line. A pending row is not
      // slow; it is either about to be picked up, or sitting in a guild
      // with no bot in it to pick it up.
      return botHasArrived ? 'waiting' : 'stalled'
    case APPLIED:
      return 'good'
    case FAILED:
      return 'bad'
    case SUPERSEDED:
      return 'neutral'
    default:
      // An outcome written by a newer bot than this console. Rendered as
      // itself rather than guessed at.
      return 'unknown'
  }
}

export interface RequestReport {
  tone: RequestTone
  /** The word in the badge. Never the only thing that says what happened. */
  badge: Message
  heading: Message
  /** Sentences under the heading, in order, each one a decision. */
  notes: Message[]
  /** The bot's own words, verbatim and multi-line, or `null`. Rendered
   *  rather than interpreted: it names which channel, which permission and
   *  what to do about it, and no key here could say that. */
  error: string | null
}

export interface ReportContext {
  /** The signed-in person's Discord id, for telling "you asked" from
   *  "somebody else did". */
  viewer: string | null
  /** The id of the request this browser submitted, if it submitted one.
   *  See {@link RequestReport} — this is how the supersede rule is made
   *  visible while it is happening. */
  submitted: string | null
}

/**
 * What to say about the request a guild's setup is currently waiting on.
 *
 * `null` when nobody has ever asked: there is no panel, rather than a panel
 * saying nothing has happened.
 *
 * **The replaced note comes first.** `GET` answers with the guild's newest
 * request, which after a colleague pressed the button thirty seconds later
 * is *theirs*. Everything else on the panel then describes a request this
 * reader did not make, and reading it as their own is how somebody
 * concludes their channel list was applied when another one was. The
 * status is still the guild's answer — that part is honest and stays — but
 * whose answer it is has to be said before it, not after.
 */
export function reportRequest(state: SetupState, context: ReportContext): RequestReport | null {
  const request = state.request
  if (!request) return null

  const tone = requestTone(request.status, state.botHasArrived)
  const notes: Message[] = []

  if (context.submitted !== null && context.submitted !== request.id) {
    notes.push({ key: 'admin.onboarding.replacedYours' })
  }

  switch (tone) {
    case 'waiting':
      notes.push({ key: 'admin.onboarding.waitingNote' })
      break
    case 'stalled':
      // Not "this is taking a while". The payload says outright that
      // nothing about this guild has ever been mirrored, and a page that
      // offered patience instead of that fact would have somebody waiting
      // on a tick that will never reach them.
      notes.push({ key: 'admin.onboarding.stalledNote' })
      break
    case 'good':
      notes.push({ key: 'admin.onboarding.appliedNote' }, { key: 'admin.onboarding.appliedNext' })
      break
    case 'bad':
      notes.push({ key: 'admin.onboarding.failedTerminal' }, { key: 'admin.onboarding.roleOrder' })
      break
    case 'neutral':
      notes.push({ key: 'admin.onboarding.supersededNote' })
      break
    default:
      notes.push({ key: 'admin.onboarding.unknownNote' })
  }

  return {
    tone,
    badge: badgeFor(tone, request.status),
    heading: { key: `admin.onboarding.heading.${tone}` },
    notes,
    // Only a failure carries one, and only a failure's is worth the room.
    // An `error` on any other outcome would be a row written by hand.
    error: tone === 'bad' ? request.error : null,
  }
}

function badgeFor(tone: RequestTone, status: string): Message {
  return tone === 'unknown'
    ? { key: 'admin.onboarding.status.unknown', params: { status } }
    : { key: `admin.onboarding.status.${tone}` }
}

/**
 * Who asked, as a sentence.
 *
 * "You" where it was this reader, because the supersede rule turns on
 * there being two administrators and the first question anybody has of a
 * request they did not expect is whose it is. Otherwise the mirror's name
 * for them, and where the mirror has no row — it holds the consent role's
 * and the admin role's members and nobody else — the bare id, which is
 * `~/utils/directory`'s single answer to an unresolved snowflake.
 */
export function requesterLabel(
  request: SetupRequestView,
  viewer: string | null,
  members: readonly NamedRow[],
): Message {
  if (viewer !== null && viewer === request.requestedBy) {
    return { key: 'admin.onboarding.askedByYou' }
  }
  const choice = resolveChoice(members, request.requestedBy)
  return choice.resolved
    ? { key: 'admin.onboarding.askedBy', params: { who: choice.label } }
    : { key: 'admin.onboarding.askedByUnresolved', params: { id: choice.label } }
}

/**
 * What pressing the button again will do, said before it is pressed.
 *
 * The API deliberately accepts a second request while the first is still
 * pending — refusing one would leave somebody who mistyped a channel unable
 * to correct it until a tick had passed, and would lock a guild whose bot
 * has not arrived out of being set up at all. So the form stays live, and
 * this is the sentence that keeps that from being a surprise.
 */
export function resubmitNote(state: SetupState): Message | null {
  const request = state.request
  if (!request) return null
  if (!isSettled(request.status)) return { key: 'admin.onboarding.resubmitReplaces' }
  if (request.status === FAILED) return { key: 'admin.onboarding.resubmitAfterFailure' }
  return { key: 'admin.onboarding.resubmitAgain' }
}

/* -------------------------------------------------------------------- */
/* Watching it happen                                                    */
/* -------------------------------------------------------------------- */

/**
 * Three seconds, against a tick that runs every ten.
 *
 * Fast enough that the answer arrives within a few seconds of the bot
 * writing it, and slow enough that one administrator watching one guild is
 * twenty reads a minute of a single row.
 */
export const POLL_INTERVAL_MS = 3000

/**
 * How many times to ask before giving up and offering a button instead.
 *
 * A hundred, which is five minutes. The wait this bounds is not the tick —
 * that is ten seconds — but a human one: `has_arrived` stays false until
 * somebody opens Discord and adds the bot, and a tab left open on that
 * state would otherwise poll for as long as the browser is running. Five
 * minutes is long enough that nobody who is actually doing it hits the
 * bound, and the page then says it has stopped rather than pretending to
 * still be watching.
 */
export const POLL_LIMIT = 100

/**
 * Whether there is still something to watch for.
 *
 * Two reasons, not one. A pending request is the obvious one. The other is
 * a guild the bot has not reached: the whole page is inert until it
 * arrives — no channels to tick, no request that can be attempted — and
 * polling is what makes the page come alive on its own when it does,
 * rather than leaving somebody to guess when to press Refresh.
 */
export function shouldPoll(state: SetupState | null, attempts: number): boolean {
  if (state === null || attempts >= POLL_LIMIT) return false
  return !state.botHasArrived || state.request?.status === PENDING
}

/* -------------------------------------------------------------------- */
/* Where the requests are                                                */
/* -------------------------------------------------------------------- */

export const INVITE_PATH = '/invite'

export function setupPath(guildId: string): string {
  return `/guilds/${guildId}/setup`
}

/* -------------------------------------------------------------------- */
/* When a call does not work                                             */
/* -------------------------------------------------------------------- */

/**
 * Why a call failed, from its status and nothing else.
 *
 * The status is all there is: `apiError.sanitiseFetchError` keeps nothing
 * else from a failed response, on purpose, so that no page can accidentally
 * render an internal hostname out of a `$fetch` error.
 *
 * The 404 is the one worth reading twice. These routes answer 404 both for
 * a guild that does not exist and for one this person does not administer,
 * deliberately and identically — so the sentence says what is true of both
 * without guessing which, and mentions the third case that actually
 * produces it here: a bot that has been removed from the server since the
 * page was opened.
 */
export function describeSetupError(error: unknown): Message {
  const held = (error as { status?: unknown } | null)?.status
  const status = typeof held === 'number' ? held : null
  switch (status) {
    case 400:
      return { key: 'admin.onboarding.errorRefused' }
    case 401:
      return { key: 'admin.onboarding.errorSession' }
    case 404:
      return { key: 'admin.onboarding.errorGone' }
    case 0:
    case null:
      return { key: 'admin.onboarding.errorUnreachable' }
    default:
      return { key: 'admin.onboarding.errorStatus', params: { status: String(status) } }
  }
}
