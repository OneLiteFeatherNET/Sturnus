/**
 * A person's own consent, as it reads to the person it belongs to.
 *
 * The sibling module `~/utils/consents` answers the administrator's
 * question -- *whose* consent does this server hold, and what does taking
 * one away do. This one answers the only question somebody has about
 * themselves: **what is being recorded of me right now, and how do I change
 * it.** The two are deliberately separate modules rather than one with a
 * flag, because almost every sentence differs: "they are not recorded" and
 * "you are not recorded" are the same fact told to two people who can do
 * different things about it.
 *
 * A module rather than expressions in the page, for this codebase's usual
 * reason: which sentence a state produces, whether the video option exists
 * at all, what a refusal reads like -- these are decisions, and a decision
 * embedded in a template can only be tested by rendering one.
 *
 * **Every function here returns a translation key, never a sentence.** See
 * `i18n/README.md`: threading a translator through a pure module would make
 * it need a Vue application before it could answer a question, which is the
 * property that put it in its own file. A key is data.
 *
 * Three facts govern the wording in `settings.consent.*`, and none of them
 * are softened:
 *
 * - **Withdrawing here writes the record; it does not remove the Discord
 *   role.** The API process holds no Discord token, by design. This is the
 *   same fact `ROLE_STAYS_NOTE` states on the administrator's page, said to
 *   the person it is about rather than about them -- the wording is that
 *   note's, in the second person, and the two must not drift into two
 *   accounts of one mechanism.
 * - **Withdrawing is not deletion.** Consent governs what is captured from
 *   now on. Whatever already holds somebody's audio keeps holding it until
 *   they erase it deliberately, with `/audio delete` in Discord, and a page
 *   that let "withdrawn" read as "erased" would answer an erasure request
 *   with a lie.
 * - **Only the person can grant.** Nothing in this console starts a
 *   consent. `/consent grant` in Discord does, because agreeing to a
 *   document is not an act software performs on somebody's behalf.
 *
 * `state` and `active` are taken as the API gives them and never
 * re-derived. They answer two different questions -- `scheduled` and
 * `active` both mean recording is happening *now*, and only one of them
 * says it will still be happening on Friday.
 */
import { formatCount, formatMoment } from '~/utils/format'

/* -------------------------------------------------------------------- */
/* What the API sends                                                    */
/* -------------------------------------------------------------------- */

/** What a person consented to. `audio_video` is a scope, not a feature:
 *  Sturnus records no video today, and consenting to it records that it
 *  would be allowed to. */
export type ConsentScope = 'audio' | 'audio_video'

/**
 * The four states a consent record can be in.
 *
 * - `active` -- in force, with no end in sight.
 * - `scheduled` -- in force, and already carrying the instant it stops.
 * - `revoked` -- withdrawn, and the withdrawal has taken effect.
 * - `policy_superseded` -- never withdrawn; it names a policy version the
 *   guild has moved past, so it stopped counting on its own.
 *
 * `scheduled` is the one the interface most needs to name. Somebody whose
 * consent runs out on Friday should be able to see that on Tuesday, and a
 * state model with only "active" and "revoked" cannot tell them.
 */
export type MyConsentState = 'active' | 'scheduled' | 'revoked' | 'policy_superseded'

const STATES: readonly MyConsentState[] = ['active', 'scheduled', 'revoked', 'policy_superseded']

export interface MyConsent {
  /** A string, always. A Discord snowflake exceeds JavaScript's safe
   *  integer range, where a JSON number silently drops its last digits and
   *  names a different server. */
  guild_id: string
  state: MyConsentState
  /** Whether recording is happening now. `scheduled` and `active` both say
   *  yes, which is why this is a separate field and not a synonym. */
  active: boolean
  scope: ConsentScope
  /** The version this record names, not necessarily the one in force. */
  policy_version: string | null
  /** The version in force now. The difference between the two is what
   *  `policy_superseded` is. */
  guild_policy_version: string | null
  granted_at: string | null
  /** Set when a withdrawal exists -- possibly one that has not happened
   *  yet. A future value is a scheduled stop, not a tombstone. */
  revoked_at: string | null
  /** Whether the guild offers video consent at all. False means the video
   *  option is *absent* from the interface, never merely disabled. */
  video_consent_offered: boolean
}

/* JSON is read defensively here rather than through a shared helper: the
 * two consent modules answer to two endpoints that can be deployed apart,
 * and a shared reader is a shared assumption about a shape only one of
 * them owns. Ten lines is a cheaper price than that coupling. */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** `null` stays `null`; anything else becomes the string it prints as. */
function asText(value: unknown): string | null {
  if (value === null || value === undefined) return null
  const text = typeof value === 'string' ? value : String(value)
  return text.trim() === '' ? null : text
}

/** Anything that is not `audio_video` is `audio`. Erring the other way
 *  would show somebody a video consent they never gave. */
function asScope(value: unknown): ConsentScope {
  return value === 'audio_video' ? 'audio_video' : 'audio'
}

/**
 * The state, as named, or one derived from the flags when the name is not
 * one this console knows.
 *
 * A console meeting a fifth state from a newer API must still say something
 * true rather than render an unknown badge. The fallback deliberately needs
 * no clock: `active` decides first, and a record that is not active is
 * either one somebody withdrew or one that lapsed.
 */
function asState(value: unknown, active: boolean, revokedAt: string | null): MyConsentState {
  if (typeof value === 'string' && (STATES as readonly string[]).includes(value)) {
    return value as MyConsentState
  }
  if (active) return revokedAt ? 'scheduled' : 'active'
  return revokedAt ? 'revoked' : 'policy_superseded'
}

/**
 * The consent records in a payload.
 *
 * Accepts the `{consents: [...]}` envelope the endpoint sends and a bare
 * list, because both describe the same thing and accepting both removes a
 * failure whose only symptom is a section that renders empty with no error
 * anywhere -- which on this page is the worst possible failure, since an
 * empty list reads as "you have consented nowhere".
 *
 * An entry naming no guild is dropped: there is no server it could be a
 * statement about.
 */
export function parseMyConsents(payload: unknown): MyConsent[] {
  let container: unknown = payload
  if (isRecord(container) && 'consents' in container) container = container.consents
  if (!Array.isArray(container)) return []

  const rows: MyConsent[] = []
  for (const entry of container) {
    if (!isRecord(entry)) continue
    const guildId = asText(entry.guild_id)
    if (!guildId) continue
    const revokedAt = asText(entry.revoked_at)
    // `=== true` rather than truthiness: a missing flag, or the string
    // "false" a careless serialiser produces, must read as "not being
    // recorded". Erring the other way tells somebody they are being
    // recorded when they are not, which is the one lie this page cannot
    // afford in either direction -- and of the two, claiming recording
    // that is not happening is the one that costs nobody their privacy.
    const active = entry.active === true
    rows.push({
      guild_id: guildId,
      active,
      state: asState(entry.state, active, revokedAt),
      scope: asScope(entry.scope),
      policy_version: asText(entry.policy_version),
      guild_policy_version: asText(entry.guild_policy_version),
      granted_at: asText(entry.granted_at),
      revoked_at: revokedAt,
      video_consent_offered: entry.video_consent_offered === true,
    })
  }
  return rows
}

/**
 * The servers in a stable order.
 *
 * The ones where something is being recorded first, because that is what
 * somebody came to check, then by guild id so the list does not reshuffle
 * itself between the server render and the browser's. Ids compare by length
 * first: as plain strings "1000…" would sort before "999…", and as numbers
 * every snowflake past the safe integer range becomes a different snowflake.
 */
export function orderMyConsents(rows: readonly MyConsent[]): MyConsent[] {
  return [...rows].sort((a, b) => {
    if (a.active !== b.active) return a.active ? -1 : 1
    if (a.guild_id.length !== b.guild_id.length) return a.guild_id.length - b.guild_id.length
    if (a.guild_id === b.guild_id) return 0
    return a.guild_id < b.guild_id ? -1 : 1
  })
}

/* -------------------------------------------------------------------- */
/* Saying it                                                             */
/* -------------------------------------------------------------------- */

/**
 * One sentence, as a key and the values it interpolates.
 *
 * Values are already rendered -- a moment is a formatted moment, never a
 * raw ISO instant -- so that the template's only job is `$t`.
 */
export interface Line {
  key: string
  values?: Record<string, string | number>
}

/** Four states, four colours. Rendering "withdrawn" and "the policy version
 *  moved on" in the same grey would hide which of the two happened, and
 *  only one of them was the person's own decision. */
export type MyConsentTone = 'active' | 'scheduled' | 'withdrawn' | 'superseded'

const TONE_BY_STATE: Record<MyConsentState, MyConsentTone> = {
  active: 'active',
  scheduled: 'scheduled',
  revoked: 'withdrawn',
  policy_superseded: 'superseded',
}

export function consentTone(row: MyConsent): MyConsentTone {
  return TONE_BY_STATE[row.state]
}

/** The badge over a server's block. Short, because the sentences under it
 *  do the explaining. */
export function consentBadgeKey(row: MyConsent): string {
  return `settings.consent.badge.${row.state}`
}

/**
 * What is being recorded of this person right now.
 *
 * Keyed on `active` rather than on `state`, because `scheduled` and
 * `active` both mean recording is happening. A person reading "scheduled"
 * and inferring "so nothing is being recorded" would have inferred the
 * opposite of the truth, and the first sentence of the block is where that
 * has to be settled.
 */
export function recordingNowLine(row: MyConsent): Line {
  if (!row.active) return { key: 'settings.consent.now.nothing' }
  return {
    key: row.scope === 'audio_video'
      ? 'settings.consent.now.audioVideo'
      : 'settings.consent.now.audio',
  }
}

/** Under which policy version, and since when. Both are shown for every
 *  state: the version is what explains a superseded record, and reading it
 *  on an active one is how somebody knows what a policy bump would cost
 *  them. */
export function grantedUnderLine(row: MyConsent): Line {
  const granted = row.granted_at ? formatMoment(row.granted_at) : null
  const policy = row.policy_version
  if (granted && policy) return { key: 'settings.consent.granted.both', values: { granted, policy } }
  if (granted) return { key: 'settings.consent.granted.noPolicy', values: { granted } }
  if (policy) return { key: 'settings.consent.granted.noDate', values: { policy } }
  return { key: 'settings.consent.granted.neither' }
}

/**
 * The sentence that only this state produces, or `null` when the state
 * needs none.
 *
 * `scheduled` is the reason this function exists. A stop that has been set
 * and has not happened yet is invisible in every other field on the record,
 * and somebody who is not told about it finds out by being un-recorded on a
 * day they expected to be recorded.
 */
export function stateLine(row: MyConsent): Line | null {
  switch (row.state) {
    case 'active':
      return null
    case 'scheduled':
      return {
        key: 'settings.consent.state.scheduled',
        values: { when: formatMoment(row.revoked_at) },
      }
    case 'revoked':
      return {
        key: 'settings.consent.state.revoked',
        values: { when: formatMoment(row.revoked_at) },
      }
    case 'policy_superseded':
      return row.guild_policy_version
        ? {
            key: 'settings.consent.state.superseded',
            values: {
              policy: row.policy_version ?? '—',
              current: row.guild_policy_version,
            },
          }
        : { key: 'settings.consent.state.supersededUnknown' }
  }
}

/** Everything a server's block says about itself, in reading order. */
export function consentNarrative(row: MyConsent): Line[] {
  const state = stateLine(row)
  return state
    ? [recordingNowLine(row), grantedUnderLine(row), state]
    : [recordingNowLine(row), grantedUnderLine(row)]
}

/* -------------------------------------------------------------------- */
/* The scope control                                                     */
/* -------------------------------------------------------------------- */

/**
 * The scopes this server can be asked for.
 *
 * **When the guild does not offer video consent the option is not in the
 * list at all** -- not disabled, not greyed, not behind a tooltip. That is
 * §5.2 of the personalisation spec, and it is not a styling preference: a
 * consent record naming video under a policy document that describes only
 * audio is not consent, and an interface must not offer what it cannot
 * honour. A disabled control still tells somebody the thing exists and that
 * they are being kept from it, which is a different and untrue statement.
 */
export function scopeChoices(row: MyConsent): ConsentScope[] {
  return row.video_consent_offered ? ['audio', 'audio_video'] : ['audio']
}

/** The one sentence that replaces the missing option, so its absence reads
 *  as a fact about the server rather than as a control that failed to
 *  render. */
export const AUDIO_ONLY_KEY = 'settings.consent.scope.audioOnly'

export function scopeLabelKey(scope: ConsentScope): string {
  return scope === 'audio_video'
    ? 'settings.consent.scope.audioVideo'
    : 'settings.consent.scope.audio'
}

/**
 * Whether the scope may be changed at all.
 *
 * A withdrawn consent has no scope left to narrow or widen -- the endpoint
 * answers `already_revoked`, every time -- and offering a control whose
 * outcome is already known is the thing this console does not do. Every
 * other state keeps the control, `policy_superseded` included: widening
 * there writes a new record under the current policy version, which is
 * exactly the way back for somebody whose consent lapsed.
 */
export function mayChangeScope(row: MyConsent): boolean {
  return row.state !== 'revoked'
}

/* -------------------------------------------------------------------- */
/* Withdrawing                                                           */
/* -------------------------------------------------------------------- */

/**
 * The Discord role is not touched by this.
 *
 * `ROLE_STAYS_NOTE` in `~/utils/consents` is the same fact told to an
 * administrator about somebody else. This is that note in the second
 * person, and it exists as a named constant for the same reason that one
 * does: the fact must be said in the confirmation itself, never only in a
 * footnote, because a person who believes this removed their role will not
 * go and remove it and will keep wearing a role that says something untrue
 * about them.
 */
export const ROLE_STAYS_KEY = 'settings.consent.withdraw.roleStays'

/** Withdrawing is not deletion, said before the act rather than discovered
 *  after it. */
export const NOT_DELETION_KEY = 'settings.consent.withdraw.notDeletion'

/** Nothing in this console can grant consent. */
export const GRANT_IS_IN_DISCORD_KEY = 'settings.consent.withdraw.grantAgain'

export interface WithdrawConfirmation {
  titleKey: string
  /** Kept as separate sentences rather than one paragraph. A paragraph
   *  carrying all of them is a paragraph that gets skimmed, and it would be
   *  skimmed exactly where the reader most needs to notice that the role
   *  and the recordings are not part of this. */
  consequences: readonly Line[]
  confirmKey: string
}

export function withdrawConfirmation(row: MyConsent): WithdrawConfirmation {
  const consequences: Line[] = [
    { key: ROLE_STAYS_KEY },
    { key: NOT_DELETION_KEY },
    { key: GRANT_IS_IN_DISCORD_KEY },
  ]
  // Somebody with a stop already on the calendar is not being told what a
  // withdrawal does in general; they are asking what this one does to the
  // date they already have. So that sentence goes first.
  if (row.state === 'scheduled') {
    consequences.unshift({
      key: 'settings.consent.withdraw.alreadyScheduled',
      values: { when: formatMoment(row.revoked_at) },
    })
  }
  return {
    titleKey: 'settings.consent.withdraw.title',
    consequences,
    confirmKey: 'settings.consent.withdraw.confirm',
  }
}

/**
 * Whether to offer the withdrawal at all, and why not when not.
 *
 * Offered in every state but `revoked`. A `policy_superseded` record is
 * still a record: withdrawing it removes it rather than waiting for it,
 * which is the difference between "not counting today" and "gone", and it
 * matters the moment a server rolls `policy_version` back to a previous
 * value and every lapsed consent quietly comes back to life.
 */
export type Withdrawability = { may: true } | { may: false, reason: Line }

export function withdrawability(row: MyConsent): Withdrawability {
  if (row.state === 'revoked') {
    return {
      may: false,
      reason: {
        key: 'settings.consent.withdraw.alreadyDone',
        values: { when: formatMoment(row.revoked_at) },
      },
    }
  }
  return { may: true }
}

/* -------------------------------------------------------------------- */
/* What the API answered                                                 */
/* -------------------------------------------------------------------- */

export interface ScopeResult {
  scope: ConsentScope
  changed: boolean
  /** A machine name for a refusal, or `null` when the write went through. */
  refusal: string | null
  policy_version: string | null
}

export function parseScopeResult(payload: unknown): ScopeResult {
  if (!isRecord(payload)) {
    return { scope: 'audio', changed: false, refusal: null, policy_version: null }
  }
  return {
    scope: asScope(payload.scope),
    // Read strictly. A body this console cannot make sense of must never be
    // reported as a successful widening: the only person who finds out
    // otherwise is the one who thinks they have said no to video.
    changed: payload.changed === true,
    refusal: asText(payload.refusal),
    policy_version: asText(payload.policy_version),
  }
}

export interface MyRevokeResult {
  revoked: boolean
  refusal: string | null
  /** When the withdrawal takes effect. Normally now; the field exists
   *  because the same column carries an administrator's chosen instant. */
  effective_at: string | null
  /** How many recordings containing this person's audio fall on or after
   *  that instant. **Withdrawing deletes none of them.** */
  recordings_from_effective_at: number
  /** Always true, and rendered rather than assumed: the API says the role
   *  stays, and the interface says what the API says. */
  role_stays: boolean
}

/** A count that can be printed. Anything absent, negative or not a number
 *  is a defect upstream, and "-3 recordings" would put that defect in front
 *  of a reader as though it were a fact about them. */
function asCount(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 0
  return Math.round(value)
}

export function parseMyRevokeResult(payload: unknown): MyRevokeResult {
  if (!isRecord(payload)) {
    return {
      revoked: false,
      refusal: null,
      effective_at: null,
      recordings_from_effective_at: 0,
      role_stays: true,
    }
  }
  return {
    revoked: payload.revoked === true,
    refusal: asText(payload.refusal),
    effective_at: asText(payload.effective_at),
    recordings_from_effective_at: asCount(payload.recordings_from_effective_at),
    // Absent reads as `true`, which is the conservative answer: it is what
    // the API does today, and a console that quietly stopped saying "your
    // role stays" would let somebody believe the opposite.
    role_stays: payload.role_stays !== false,
  }
}

/**
 * A refusal, as a sentence.
 *
 * All four say the same thing to the person reading the screen -- nothing
 * they just did changed anything -- and they still get four sentences,
 * because they send somebody to four different places: to their server's
 * administrator, to Discord, to nowhere at all.
 *
 * The `null` case is not merely defensive. `useApi` strips the body off
 * every failed request on purpose, so a 409 arrives with a status and no
 * refusal code attached; that sentence therefore has to be true of all
 * four.
 */
export function describeRefusalKey(refusal: string | null): string {
  switch (refusal) {
    case 'video_consent_not_offered':
      return 'settings.consent.refusal.videoNotOffered'
    case 'no_consent_on_record':
      return 'settings.consent.refusal.noConsent'
    case 'already_revoked':
      return 'settings.consent.refusal.alreadyRevoked'
    case 'no_policy_version':
      return 'settings.consent.refusal.noPolicyVersion'
    default:
      return 'settings.consent.refusal.unknown'
  }
}

export interface Outcome {
  tone: 'done' | 'refused'
  headline: Line
  detail: readonly Line[]
}

/** What the scope control says after it has written. Never merely "Saved":
 *  narrowing and widening are different acts, and one of them created a new
 *  consent record under a policy version worth naming. */
export function scopeOutcome(result: ScopeResult): Outcome {
  if (result.refusal !== null || !result.changed) {
    // `changed: false` with no refusal is the API saying the scope was
    // already what was asked for. That is not a failure and must not be
    // painted as one, but it is also not a change, and reporting it as one
    // would teach somebody that the control lies.
    if (result.refusal === null) {
      return {
        tone: 'done',
        headline: { key: 'settings.consent.scope.unchangedHeadline' },
        detail: [{ key: 'settings.consent.scope.unchangedDetail' }],
      }
    }
    return {
      tone: 'refused',
      headline: { key: 'settings.consent.scope.refusedHeadline' },
      detail: [{ key: describeRefusalKey(result.refusal) }],
    }
  }
  if (result.scope === 'audio_video') {
    return {
      tone: 'done',
      headline: { key: 'settings.consent.scope.widenedHeadline' },
      detail: [
        {
          key: result.policy_version
            ? 'settings.consent.scope.widenedDetail'
            : 'settings.consent.scope.widenedDetailNoPolicy',
          values: { policy: result.policy_version ?? '—' },
        },
        { key: 'settings.consent.scope.videoNotRecordedYet' },
      ],
    }
  }
  return {
    tone: 'done',
    headline: { key: 'settings.consent.scope.narrowedHeadline' },
    detail: [{ key: 'settings.consent.scope.narrowedDetail' }],
  }
}

/**
 * The panel shown after a withdrawal, and never merely "Done".
 *
 * It repeats the two limits afterwards as well as before, because the
 * moment somebody is most likely to believe more happened than did is the
 * moment they have just watched their own row change state. The count comes
 * from the API's answer rather than from anything this console guessed, and
 * it is the number that tells somebody the recordings are still there.
 */
export function withdrawOutcome(result: MyRevokeResult): Outcome {
  if (!result.revoked) {
    return {
      tone: 'refused',
      headline: { key: 'settings.consent.withdraw.refusedHeadline' },
      detail: [{ key: describeRefusalKey(result.refusal) }],
    }
  }
  const held = result.recordings_from_effective_at
  const detail: Line[] = [
    {
      key: result.effective_at
        ? 'settings.consent.withdraw.doneDetail'
        : 'settings.consent.withdraw.doneDetailNoInstant',
      values: { when: formatMoment(result.effective_at) },
    },
  ]
  detail.push(
    held === 0
      ? { key: 'settings.consent.withdraw.heldNone' }
      : {
          key: held === 1
            ? 'settings.consent.withdraw.heldOne'
            : 'settings.consent.withdraw.heldMany',
          values: { count: formatCount(held) },
        },
  )
  if (result.role_stays) detail.push({ key: ROLE_STAYS_KEY })
  detail.push({ key: GRANT_IS_IN_DISCORD_KEY })
  return {
    tone: 'done',
    headline: { key: 'settings.consent.withdraw.doneHeadline' },
    detail,
  }
}

/* -------------------------------------------------------------------- */
/* When the API says nothing useful                                      */
/* -------------------------------------------------------------------- */

/** `ApiError` names it `status`; a raw `$fetch` failure may name it
 *  `statusCode`; a request that never got a response has neither, and null
 *  says so rather than standing in a number that would read as an answer. */
function statusOf(error: unknown): number | null {
  if (!isRecord(error)) return null
  for (const candidate of [error.status, error.statusCode]) {
    if (typeof candidate === 'number' && Number.isFinite(candidate)) {
      // `ApiError` uses 0 for "never reached the API", which is
      // deliberately distinguishable from every real status.
      return candidate === 0 ? null : candidate
    }
  }
  return null
}

/**
 * Whether the API this console is talking to has no consent endpoint yet.
 *
 * `GET /api/me/consents` answers 404 until the pull request that adds it is
 * deployed, and the console and the API ship as two images that can be
 * deployed apart. This is the one failure whose handling is not a nicety:
 * a 404 rendered as an empty list would tell somebody they have consented
 * nowhere, which is a false statement about their own data and the single
 * worst thing this page could say.
 */
export function isConsentServiceMissing(error: unknown): boolean {
  return statusOf(error) === 404
}

/** Why the section could not be read, in a sentence somebody can act on.
 *  Built from the status alone: `useApi` throws `ApiError`, which carries no
 *  body by design, so there is no server text to prefer. */
export function describeMyConsentErrorKey(error: unknown): string {
  switch (statusOf(error)) {
    case 401:
      return 'settings.consent.error.signedOut'
    case 404:
      return 'settings.consent.error.notDeployed'
    case null:
      return 'settings.consent.error.unreachable'
    default:
      return 'settings.consent.error.unknown'
  }
}

/** The values `describeMyConsentErrorKey`'s sentence interpolates. Only the
 *  unknown one names a number, and only because a status is the single
 *  thing worth reporting about a failure nothing else is known about. */
export function describeMyConsentError(error: unknown): Line {
  const key = describeMyConsentErrorKey(error)
  return key === 'settings.consent.error.unknown'
    ? { key, values: { status: statusOf(error) ?? 0 } }
    : { key }
}
