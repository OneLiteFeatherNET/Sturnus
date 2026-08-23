/**
 * What a configuration key is, and what changing one actually does.
 *
 * The endpoints hand back a *description* of each key -- whether it is
 * required, whether it must parse as an integer, whether writing it
 * invalidates consent, and when the running process will pick it up.
 * Turning that description into words, into an input, and into a verdict
 * on whether an action is even offered is a pile of decisions, so it lives
 * here rather than in the page: a decision embedded in a template can only
 * be tested by rendering one.
 *
 * The load-bearing one is `takes_effect`. Writing a value and replying
 * "Saved" while the running process keeps using the old one is the exact
 * defect the Discord `/config` replies were built to stop
 * (`sturnus.infrastructure.discord.config_cog`), and a console that says
 * "Saved" for all three cases would reintroduce it one layer up. So the
 * three cases are three different sentences, and the one that means
 * "nothing will happen until somebody restarts the deployment" says so.
 */
import type { KeyValueStore } from '~/utils/preferences'
import type { UiOption } from '~/utils/uiOption'

/** Read at each use. Storing it is applying it. */
export const TAKES_EFFECT_IMMEDIATELY = 'immediately'
/** Cached by the bot, which re-reads roughly every ten seconds. */
export const TAKES_EFFECT_NEXT_RECONCILE = 'next_reconcile'
/** Read once at process start. Waiting does not help. */
export const TAKES_EFFECT_PROCESS_RESTART = 'process_restart'

export interface SettingView {
  key: string
  /** The value in force, or `null` when the key is unset and has no
   *  default. Unset and empty-string are different things and the parser
   *  keeps them apart. */
  value: string | null
  default: string | null
  required: boolean
  /** Whether `DELETE` on this key restores something rather than taking
   *  the guild out of service -- the clear endpoint's own verdict, sent
   *  rather than inferred. It is *not* `!required`: `voice_channel_id` is
   *  required of nobody and refused to everybody, because a guild still
   *  being served by the deprecated spelling has nothing to fall back to
   *  either. Deriving the button from `required` is what put a live Clear
   *  next to that key and answered it with "this key is required". */
  may_clear: boolean
  integer: boolean
  invalidates_consent: boolean
  /** Deliberately a plain string rather than a union of the three known
   *  values. A fourth one added to the API must render as "we do not know
   *  when this lands" -- not silently as the friendliest of the three. */
  takes_effect: string
  deferred_while_recording: boolean
}

export interface GuildRef {
  id: string
  /** The API sends only `guild_id` today. Reading a name if one ever
   *  appears is what will let the switcher stop showing two
   *  indistinguishable snowflakes without another change here. */
  name: string | null
}

/* -------------------------------------------------------------------- */
/* Reading what the API sent                                            */
/* -------------------------------------------------------------------- */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** `null` stays `null`; anything else becomes the string it prints as.
 *  Values are strings on the wire, and a number that arrived instead has
 *  already lost whatever precision it was going to lose. */
function asText(value: unknown): string | null {
  if (value === null || value === undefined) return null
  return typeof value === 'string' ? value : String(value)
}

function asView(key: string, raw: Record<string, unknown>): SettingView {
  return {
    key,
    value: asText(raw.value),
    default: asText(raw.default),
    // `=== true` rather than truthiness: a missing flag must read as off,
    // and so must a string "false", which is what a careless serialiser
    // produces and what would otherwise turn every key into a required one.
    required: raw.required === true,
    // `=== true` here means an API that does not send the flag renders no
    // Clear button at all. That is the right way round: a control that is
    // missing costs a support question, and one that is offered and then
    // refused costs a 409 in somebody's face on a page that had already
    // told them the field was optional.
    may_clear: raw.may_clear === true,
    integer: raw.integer === true,
    invalidates_consent: raw.invalidates_consent === true,
    takes_effect: typeof raw.takes_effect === 'string' ? raw.takes_effect : '',
    deferred_while_recording: raw.deferred_while_recording === true,
  }
}

/**
 * The key views in a settings payload.
 *
 * Accepts a list, a `{settings: ...}` envelope around one, or an object
 * keyed by key name. "Every key with value, default, ..." describes all
 * three equally, and accepting them all costs a few lines and removes a
 * class of failure whose symptom is a page that renders empty with no
 * error anywhere.
 */
export function parseSettings(payload: unknown): SettingView[] {
  let container: unknown = payload
  if (isRecord(container)) {
    for (const envelope of ['settings', 'keys'] as const) {
      if (envelope in container) {
        container = container[envelope]
        break
      }
    }
  }

  if (Array.isArray(container)) {
    const views: SettingView[] = []
    for (const entry of container) {
      if (!isRecord(entry)) continue
      const key = asText(entry.key)
      if (!key) continue
      views.push(asView(key, entry))
    }
    return views
  }

  if (isRecord(container)) {
    return Object.entries(container)
      .filter(([, raw]) => isRecord(raw))
      .map(([key, raw]) => asView(key, raw as Record<string, unknown>))
  }

  return []
}

/** The guilds the caller administers. */
export function parseGuilds(payload: unknown): GuildRef[] {
  const container = isRecord(payload) && 'guilds' in payload ? payload.guilds : payload
  if (!Array.isArray(container)) return []
  const guilds: GuildRef[] = []
  for (const entry of container) {
    if (!isRecord(entry)) continue
    const id = asText(entry.guild_id) ?? asText(entry.id)
    if (!id) continue
    guilds.push({ id, name: asText(entry.name) })
  }
  return guilds
}

/** What to call a guild on screen. The whole id, never a shortened one:
 *  snowflakes minted around the same time share their leading digits, and
 *  a truncated id is precisely the ambiguity the switcher exists to
 *  remove. */
export function guildLabel(guild: GuildRef): string {
  return guild.name ?? `Server ${guild.id}`
}

/**
 * The guilds as rows of a dropdown.
 *
 * The id goes in `detail` — the subtext line the control was built for —
 * and only where there is a name above it to disambiguate. A guild the API
 * sent no name for is already labelled `Server 100000000000000001` by
 * `guildLabel`, and repeating the snowflake underneath it would render the
 * same eighteen digits twice in two type sizes.
 */
export function guildOptions(guilds: readonly GuildRef[]): UiOption[] {
  return guilds.map((guild) => ({
    value: guild.id,
    label: guildLabel(guild),
    ...(guild.name === null ? {} : { detail: guild.id }),
  }))
}

/* -------------------------------------------------------------------- */
/* When a change actually lands                                          */
/* -------------------------------------------------------------------- */

/** `live` is done, `soon` is on its way, `restart` needs a human, and
 *  `unknown` is the API describing a timing this console predates. They
 *  are four tones because they deserve four different colours -- a
 *  `restart` rendered in the same green as a `live` is the lie again. */
export type EffectTone = 'live' | 'soon' | 'restart' | 'unknown'

export interface EffectBadge {
  tone: EffectTone
  label: string
  /** The long form, for a `title` and for the panel after a write. */
  detail: string
}

export interface WriteOutcome {
  tone: EffectTone
  headline: string
  detail: string
}

const DEFERRAL_NOTE =
  ' If a session is recording in this server the change is held until that session has finished ' +
  'and uploaded — at the latest after `max_session_hours` — and the recording is never lost.'

function effectFacts(view: SettingView): EffectBadge {
  switch (view.takes_effect) {
    case TAKES_EFFECT_IMMEDIATELY:
      return {
        tone: 'live',
        label: 'Applies at once',
        detail: 'Sturnus reads this key each time it uses it, so storing it is applying it.',
      }
    case TAKES_EFFECT_NEXT_RECONCILE:
      return {
        tone: 'soon',
        label: view.deferred_while_recording
          ? 'Applies within ~10s · held during a recording'
          : 'Applies within ~10s',
        detail:
          'The bot keeps this key in memory and re-reads its configuration roughly every ten ' +
          'seconds.' + (view.deferred_while_recording ? DEFERRAL_NOTE : ''),
      }
    case TAKES_EFFECT_PROCESS_RESTART:
      // No deferral note here even when the flag is set: holding a change
      // until a recording ends is meaningless for a value nothing reads
      // again until the process starts over.
      return {
        tone: 'restart',
        label: 'Needs a pod restart',
        detail:
          'This key is read once, when the bot process starts. No amount of waiting applies it — ' +
          'somebody has to restart the deployment.',
      }
    default:
      return {
        tone: 'unknown',
        label: 'Timing unknown',
        detail:
          'The API described a timing this console does not recognise. Do not assume the value is ' +
          'in force; `/config show` in Discord reports what the process is actually using.',
      }
  }
}

/** The short mark shown beside a key *before* anybody edits it. Knowing a
 *  change needs a restart is worth far more before the edit than after. */
export function effectBadge(view: SettingView): EffectBadge {
  return effectFacts(view)
}

/**
 * The sentence shown after a write succeeded. Never merely "Saved".
 *
 * **It names the key.** Every one of these panels used to read
 * "Saved, and in effect now." and there is one per key on a page that has
 * twenty of them; grouped onto tabs there can be two open panels a screen
 * apart, one of them left over from the key somebody wrote a minute ago. A sentence that
 * does not say which key it is about is a sentence that can be read against
 * the wrong one — and the reading that costs something is the cheerful
 * `live` sentence being taken for the `restart` key underneath it.
 *
 * The key is named the way the heading above it names it, rather than as
 * the raw key: the two sit within a few centimetres of each other and a
 * reader should not have to notice they are the same word twice.
 */
export function writeOutcome(view: SettingView, action: 'saved' | 'cleared'): WriteOutcome {
  const facts = effectFacts(view)
  const verb = action === 'saved' ? 'Saved' : 'Cleared'
  const name = keyLabel(view.key)
  switch (facts.tone) {
    case 'live':
      return { tone: 'live', headline: `${verb}: ${name} is in effect now.`, detail: facts.detail }
    case 'soon':
      return {
        tone: 'soon',
        headline: `${verb}: ${name} is in effect within about ten seconds.`,
        detail: facts.detail,
      }
    case 'restart':
      return {
        tone: 'restart',
        headline: `${verb}, but ${name} is not in force.`,
        detail: facts.detail,
      }
    default:
      return {
        tone: 'unknown',
        headline: `${verb}: when ${name} is picked up is not known.`,
        detail: facts.detail,
      }
  }
}

/* -------------------------------------------------------------------- */
/* What may be done to a key                                             */
/* -------------------------------------------------------------------- */

export type Clearability = { clearable: true } | { clearable: false; reason: string }

/**
 * Whether to offer a clear at all.
 *
 * A key with no default answers 409, always. Offering the button and
 * rendering the refusal afterwards would be an interface that invites an
 * action it already knows the outcome of.
 *
 * The verdict is `may_clear`, sent by the API, and deliberately not
 * `!required` computed here. Those two agreed while there were two
 * classes of key and stopped agreeing the moment there were three: the
 * deprecated `voice_channel_id` is required of nobody and clearable by
 * nobody, and a console with its own copy of the rule is a console that
 * can disagree with the endpoint enforcing it. `required` still decides
 * how the field is *marked*; it decides nothing about this button.
 */
export function clearability(view: SettingView): Clearability {
  if (!view.may_clear) {
    return {
      clearable: false,
      reason: view.required
        ? 'Cannot be cleared: required, with no default to fall back to. Set a different value ' +
          'instead.'
        : 'Cannot be cleared: there is no default to fall back to, so clearing it would stop ' +
          'this server recording rather than restore anything. Set a different value instead.',
    }
  }
  if (view.value === null) {
    return { clearable: false, reason: 'Nothing is stored for this key.' }
  }
  return { clearable: true }
}

export interface Confirmation {
  title: string
  consequence: string
  confirmLabel: string
}

/**
 * The confirmation a key needs before it is written, or `null`.
 *
 * `invalidates_consent` warns *before* the write. Bumping `policy_version`
 * stops every consent that names the old one from counting, and the
 * packet-level filter re-checks within the consent cache's five-second TTL
 * — so this lands in the middle of a meeting that is already running
 * (`docs/operations.md` §6). Discovering that afterwards is discovering it
 * from the people who stopped being recorded.
 */
export function confirmation(view: SettingView): Confirmation | null {
  if (!view.invalidates_consent) return null
  return {
    title: `Changing ${keyLabel(view.key)} withdraws everybody's consent.`,
    consequence:
      'Every consent record naming the current value stops counting the moment this is written. ' +
      'Members holding the consent role are not recorded any more — including in a meeting that ' +
      'is running right now — until each of them runs /consent grant again under the new value.',
    confirmLabel: 'Yes, invalidate every consent',
  }
}

/* -------------------------------------------------------------------- */
/* Checking a value before it is sent                                    */
/* -------------------------------------------------------------------- */

export type Validation = { ok: true; value: string } | { ok: false; message: string }

/** Digits and nothing else. Deliberately *not* `Number.parseInt`:
 *  `admin_role_id` is an integer key and a Discord snowflake, which
 *  exceeds `Number.MAX_SAFE_INTEGER`, so anything that round-trips through
 *  a JavaScript number would happily accept an id and hand back one
 *  ending in different digits. */
const DIGITS = /^[0-9]+$/

/**
 * A client-side check, and only that.
 *
 * The server's validation is the authority: this exists so a typo is
 * caught while the cursor is still in the field, not so the console can
 * decide what the store will accept. Anything that passes here is still
 * sent and its 400, if one comes, is rendered as the message.
 */
export function validateValue(view: SettingView, raw: string): Validation {
  const value = raw.trim()

  if (value === '') {
    if (view.required) {
      return {
        ok: false,
        message: `${keyLabel(view.key)} is required and has no default, so it cannot be empty.`,
      }
    }
    if (view.integer) {
      return { ok: false, message: 'Whole number, greater than zero. This cannot be empty.' }
    }
    // An empty `transcription_prompt` is a real choice -- it asks Whisper
    // for no vocabulary bias at all -- and is not the same as clearing the
    // key, which restores the default prompt. Refusing it here would take
    // away a setting the store allows.
    return { ok: true, value }
  }

  if (view.integer) {
    if (!DIGITS.test(value)) {
      return {
        ok: false,
        message: 'Whole number only — no sign, no decimal point, no thousands separator.',
      }
    }
    if (/^0+$/.test(value)) {
      return { ok: false, message: 'Must be greater than zero.' }
    }
  }

  return { ok: true, value }
}

/** Shortens a value for a hint or a summary line, marking that it did. */
export function summariseValue(value: string, limit = 120): string {
  const flat = value.replace(/\s+/g, ' ').trim()
  return flat.length <= limit ? flat : `${flat.slice(0, limit)}…`
}

/** The notes under a field: what is enforced, and what it falls back to. */
export function fieldHints(view: SettingView): string[] {
  const hints: string[] = []
  if (view.required) {
    hints.push('Required — there is no default, so it must always have a value.')
  }
  if (view.integer) {
    hints.push('Whole number, greater than zero.')
  }
  if (view.default !== null) {
    // Truncated: the default `transcription_prompt` is a two hundred
    // character sentence, and a hint that pushes the next field off the
    // screen is not a hint.
    hints.push(`Default: ${summariseValue(view.default, 80)}`)
  }
  return hints
}

export type InputKind = 'integer' | 'multiline' | 'text'

/**
 * Which control a key gets.
 *
 * The eighty-character threshold is arbitrary; the failure it prevents is
 * not. The default `transcription_prompt` is a two hundred character
 * German sentence, and in a one-line field it can only be edited by
 * scrolling through it a word at a time.
 */
export function inputKind(view: SettingView): InputKind {
  if (view.integer) return 'integer'
  const sample = view.value ?? view.default ?? ''
  if (sample.includes('\n') || sample.length > 80) return 'multiline'
  return 'text'
}

/** Initialisms that read as typos when a naive title case gets them. */
const UPPERCASE_WORDS = new Set(['id', 'url', 'ttl', 'api', 's3'])

/** `policy_version` → `Policy version`, `admin_role_id` → `Admin role ID`. */
export function keyLabel(key: string): string {
  const words = key.split('_').filter((word) => word.length > 0)
  return words
    .map((word, index) => {
      if (UPPERCASE_WORDS.has(word)) return word.toUpperCase()
      if (index === 0) return word.charAt(0).toUpperCase() + word.slice(1)
      return word
    })
    .join(' ')
}

/**
 * The order the keys are listed in.
 *
 * A required key with nothing set comes first: those are the ones keeping
 * the guild from being watched at all, and burying them under an
 * alphabetical list is how a guild stays quietly unconfigured. Everything
 * else is alphabetical inside its rank, so the list never reshuffles
 * itself under somebody's cursor when a value changes.
 */
export function orderSettings(views: readonly SettingView[]): SettingView[] {
  const rank = (view: SettingView) => (view.required ? (view.value === null ? 0 : 1) : 2)
  return [...views].sort((a, b) => rank(a) - rank(b) || a.key.localeCompare(b.key))
}

/**
 * The required keys with nothing set.
 *
 * These are not merely incomplete rows: until every one of them has a
 * value the bot does not watch that guild's voice channel at all, and the
 * settings page is the only place somebody would find that out before the
 * next meeting records nothing. Sorted so the notice does not reorder
 * itself between renders.
 */
export function missingRequired(views: readonly SettingView[]): string[] {
  return views
    .filter((view) => view.required && view.value === null)
    .map((view) => view.key)
    .sort()
}

/* -------------------------------------------------------------------- */
/* When the API says no                                                  */
/* -------------------------------------------------------------------- */

function statusOf(error: unknown): number | null {
  if (!isRecord(error)) return null
  for (const candidate of [error.status, error.statusCode]) {
    if (typeof candidate === 'number') return candidate
  }
  const response = error.response
  if (isRecord(response) && typeof response.status === 'number') return response.status
  return null
}

/** The API's own words, if it sent any. Only strings: a `detail` that is
 *  a list of field errors would render as `[object Object]`, which is
 *  worse than the generic sentence. */
function serverMessage(error: unknown): string | null {
  if (!isRecord(error)) return null
  const data = error.data
  if (typeof data === 'string' && data.trim() !== '') return data.trim()
  if (!isRecord(data)) return null
  for (const field of ['error', 'detail', 'message', 'reason'] as const) {
    const value = data[field]
    if (typeof value === 'string' && value.trim() !== '') return value.trim()
  }
  return null
}

/**
 * A failed request, in a sentence somebody can act on.
 *
 * For a 400 and a 409 the server's own text wins where there is one: those
 * are the two statuses where the API knows something specific about *this*
 * value that the console does not. For the rest the console's explanation
 * is the more useful of the two — "Forbidden" says nothing about
 * `admin_role_id`.
 */
export function describeError(error: unknown): string {
  const status = statusOf(error)
  const fromServer = serverMessage(error)

  switch (status) {
    case 400:
      return fromServer ?? 'The API rejected that value, without saying why.'
    case 401:
      return 'Your session has ended. Sign in again, then retry — nothing was written.'
    case 403:
      return (
        'You do not administer this server. Administrators are the members holding the role named ' +
        'by that guild’s `admin_role_id`.'
      )
    case 404:
      return 'Sturnus no longer knows this server or this key. Reload the page.'
    case 409:
      return (
        fromServer ??
        'This key is required and cannot be cleared. Set a different value instead.'
      )
    default:
      break
  }

  if (status === null) {
    return 'Could not reach the API. Nothing was written; check the connection and retry.'
  }
  return fromServer
    ? `Sturnus answered ${status}: ${fromServer}`
    : `Sturnus answered ${status}. Nothing is known about why.`
}

/* -------------------------------------------------------------------- */
/* Which guild is being edited                                           */
/* -------------------------------------------------------------------- */

export const SELECTED_GUILD_KEY = 'sturnus.settings.guild'

/**
 * The guild to edit: the one chosen last time, if it is still one of
 * theirs.
 *
 * The check is not a formality. Administrator status is mirrored from
 * Discord and can be revoked, and the bot can be removed from a server —
 * a remembered id that survived either of those would leave somebody
 * editing a guild the API will refuse, with a stale name in the heading.
 */
export function chooseGuild(guilds: readonly GuildRef[], remembered: string | null): string | null {
  if (guilds.length === 0) return null
  if (remembered && guilds.some((guild) => guild.id === remembered)) return remembered
  return guilds[0]!.id
}

/** Guarded the same way the sidebar preference is: `localStorage` throws
 *  on access in a private window with site data blocked, and a settings
 *  page that failed to render over a remembered guild id would be a poor
 *  trade for a convenience. */
export function readSelectedGuild(storage: KeyValueStore | null | undefined): string | null {
  if (!storage) return null
  try {
    return storage.getItem(SELECTED_GUILD_KEY)
  } catch {
    return null
  }
}

/** Records the choice, and reports whether it will survive a reload. */
export function writeSelectedGuild(
  storage: KeyValueStore | null | undefined,
  guildId: string,
): boolean {
  if (!storage) return false
  try {
    storage.setItem(SELECTED_GUILD_KEY, guildId)
    return true
  } catch {
    return false
  }
}
