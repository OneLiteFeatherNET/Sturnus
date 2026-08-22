/**
 * Who has consented to being recorded in a guild, and what withdrawing one
 * of those consents actually does.
 *
 * A module rather than expressions in the page, for the same reason
 * `~/utils/settings` is one: every function here is a *decision* -- which
 * row comes first, whether a revoke is offered at all, what a refusal reads
 * like as a sentence, how a moment is written -- and a decision embedded in
 * a template can only be tested by rendering one.
 *
 * Three facts govern the wording of everything below, and none of them are
 * softened anywhere in this file:
 *
 * - **Withdrawing here removes the stored consent record, not the Discord
 *   role.** The API process holds no Discord token by design, so it cannot
 *   take a role off anybody. Recording still stops, within about five
 *   seconds and in the middle of a running session, because the stored
 *   record -- not the role -- is what the packet filter checks on every
 *   frame. But an administrator who believes the role is gone will be wrong
 *   about what the member can see in Discord, and about what happens the
 *   next time the policy version changes.
 * - **Withdrawing deletes nothing that was already recorded.** Consent
 *   governs what is captured from now on. The audio already on disk stays
 *   until somebody erases it deliberately with `/audio purge`, and a page
 *   that let "withdrawn" read as "removed" would be answering a data
 *   subject's erasure request with a lie.
 * - **It is logged.** Who withdrew whose consent, and when, goes to the
 *   audit log. Saying so up front is fairer than letting somebody find out
 *   afterwards.
 *
 * The `active` flag is authoritative and is never re-derived here. A
 * consent also stops counting when the guild's `policy_version` moves past
 * the one it names, so a record can carry no revocation date and still be
 * inactive. Those are two different facts about a person -- one of them
 * they chose -- and this module keeps them apart everywhere.
 */
import { formatCount, formatMoment } from '~/utils/format'

/** One person's consent, as the API describes it. */
export interface ConsentRow {
  /** A string, always. A Discord snowflake exceeds JavaScript's safe
   *  integer range, where a JSON number silently drops its last digits and
   *  produces an id that looks right and names somebody else. */
  discord_user_id: string
  /** Null when Sturnus has never seen a name for them -- they consented but
   *  have not been in a recorded session since, so there was no occasion to
   *  learn one. */
  display_name: string | null
  /** The guild policy version the consent names, not necessarily the one in
   *  force now. The difference between the two is what `active` reports. */
  policy_version: string | null
  granted_at: string | null
  /** Set only when somebody withdrew it. A lapsed consent has none. */
  revoked_at: string | null
  /** The API's verdict, taken as given. See the module comment. */
  active: boolean
  /** How many recordings the guild still holds that contain this person's
   *  audio. Withdrawing changes none of them. */
  recordings_with_audio: number
}

/* -------------------------------------------------------------------- */
/* Reading what the API sent                                             */
/* -------------------------------------------------------------------- */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** `null` stays `null`; anything else becomes the string it prints as. Ids
 *  and versions are strings on the wire, and a number that arrived instead
 *  has already lost whatever precision it was going to lose. */
function asText(value: unknown): string | null {
  if (value === null || value === undefined) return null
  const text = typeof value === 'string' ? value : String(value)
  return text.trim() === '' ? null : text
}

/** A count that can be printed. Anything absent, negative or not a number
 *  is a defect upstream, and rendering it as "-3 recordings" would put the
 *  defect in front of the reader as though it were a fact about them. Zero
 *  is the honest floor: it says "none held", which is exactly as far as
 *  this console can vouch for. */
function asCount(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 0
  return Math.round(value)
}

/**
 * The consent rows in a payload.
 *
 * Accepts the `{guild_id, consents: [...]}` envelope the endpoint sends and
 * a bare list, because "the consents of a guild" describes both equally and
 * accepting both removes a class of failure whose only symptom is a page
 * that renders empty with no error anywhere. An entry naming no user is
 * dropped: there is nothing a row without an id could be revoked for.
 */
export function parseConsents(payload: unknown): ConsentRow[] {
  let container: unknown = payload
  if (isRecord(container) && 'consents' in container) container = container.consents
  if (!Array.isArray(container)) return []

  const rows: ConsentRow[] = []
  for (const entry of container) {
    if (!isRecord(entry)) continue
    const id = asText(entry.discord_user_id) ?? asText(entry.user_id)
    if (!id) continue
    rows.push({
      discord_user_id: id,
      display_name: asText(entry.display_name),
      policy_version: asText(entry.policy_version),
      granted_at: asText(entry.granted_at),
      revoked_at: asText(entry.revoked_at),
      // `=== true` rather than truthiness: a missing flag, or the string
      // "false" a careless serialiser produces, must read as "not active".
      // Erring the other way would tell an administrator somebody is being
      // recorded when the bot has already stopped.
      active: entry.active === true,
      recordings_with_audio: asCount(entry.recordings_with_audio),
    })
  }
  return rows
}

/* -------------------------------------------------------------------- */
/* Naming a person                                                       */
/* -------------------------------------------------------------------- */

/** What to call somebody on screen. The whole id when there is no name --
 *  never a shortened one, since snowflakes minted in the same era share
 *  their leading digits and a truncated id identifies a group rather than a
 *  person. */
export function personLabel(row: ConsentRow): string {
  return row.display_name ?? `Discord user ${row.discord_user_id}`
}

/**
 * The line under a nameless row, or `null` when there is a name.
 *
 * A bare snowflake where every other row has a name reads as a fault in the
 * console. It is not one: consent is given in a Discord command, and a
 * display name is only learned when somebody turns up in a session that was
 * recorded. Saying so is the difference between "this row is broken" and
 * "this person has consented and has not been in a meeting yet".
 */
export function identityNote(row: ConsentRow): string | null {
  if (row.display_name) return null
  return (
    'No display name on record: this is their Discord user id. They gave consent but have not '
    + 'been in a recorded session since, so Sturnus has never had a name to learn.'
  )
}

/* -------------------------------------------------------------------- */
/* What state a consent is in                                            */
/* -------------------------------------------------------------------- */

/** Three states, three colours. "Withdrawn" and "the policy version they
 *  agreed to is no longer the current one" are different facts about a
 *  person -- one of them their own decision -- and rendering both as a
 *  single grey "inactive" would hide which. */
export type ConsentTone = 'active' | 'superseded' | 'withdrawn'

export interface ConsentBadge {
  tone: ConsentTone
  label: string
  /** The long form, said in full on the row rather than hidden in a
   *  tooltip: the difference between the two inactive states decides what
   *  an administrator should do next, and nobody hovers to find that out. */
  detail: string
}

/**
 * What this row's state is, in a badge and a sentence.
 *
 * `active` decides, because the API says it decides. The revocation date
 * only distinguishes the two ways of being inactive. A row that somehow
 * arrives active *and* carrying a revocation date is contradictory data;
 * the badge trusts `active` as instructed, while `revocability` below still
 * withholds the button, because the API would refuse that write whatever
 * this console believes.
 */
export function consentBadge(row: ConsentRow): ConsentBadge {
  if (row.active) {
    return {
      tone: 'active',
      label: 'Consent in force',
      detail:
        'Sturnus records this person while a session runs. The stored record is what the audio '
        + 'filter checks on every frame — the Discord role only decides who may be asked, never '
        + 'who is captured.',
    }
  }
  if (row.revoked_at) {
    return {
      tone: 'withdrawn',
      label: 'Withdrawn',
      detail:
        `Withdrawn on ${formatMoment(row.revoked_at)}. They are not recorded, and will not be `
        + 'again until they run /consent grant themselves.',
    }
  }
  return {
    tone: 'superseded',
    label: 'Policy version superseded',
    detail:
      `Nobody withdrew this consent — it lapsed. They agreed under policy version `
      + `${row.policy_version ?? 'an unrecorded value'}, this server's policy_version has moved on `
      + 'since, and a consent naming an old version stops counting. They are not being recorded '
      + 'until they run /consent grant again under the current version.',
  }
}

/* -------------------------------------------------------------------- */
/* The facts on a row                                                    */
/* -------------------------------------------------------------------- */

/**
 * Which policy version this consent names.
 *
 * Shown for every row, not only the superseded ones. It is the field that
 * explains a superseded row, and reading it on the active rows is how
 * somebody works out what bumping `policy_version` would cost before they
 * do it on the Bot Settings page.
 */
export function policyLine(row: ConsentRow): string {
  if (!row.policy_version) return 'The policy version they agreed under was not recorded.'
  return `Agreed under policy version ${row.policy_version}.`
}

/** When they gave it. `formatMoment` is borrowed from `~/utils/format`
 *  rather than reimplemented: the console has one way of printing a moment
 *  -- UTC, saying so, because the server render cannot know the reader's
 *  zone and a second rendering would disagree with the first. */
export function grantedLine(row: ConsentRow): string {
  if (!row.granted_at) return 'When they granted it was not recorded.'
  return `Granted ${formatMoment(row.granted_at)}.`
}

/** When it was withdrawn, or `null` for a consent nobody has withdrawn. A
 *  lapsed consent must not borrow this line: it would read as a decision
 *  the person made, which is precisely what it is not. */
export function withdrawnLine(row: ConsentRow): string | null {
  if (!row.revoked_at) return null
  return `Withdrawn ${formatMoment(row.revoked_at)}.`
}

/**
 * How much of this person is still on disk.
 *
 * On the row as well as in the confirmation, because it is the number that
 * answers the question an administrator is usually really asking. Somebody
 * who came here to erase a person's audio has come to the wrong page, and
 * this is where they find that out.
 */
export function recordingsLine(row: ConsentRow): string {
  const held = row.recordings_with_audio
  if (held === 0) return 'Sturnus holds no recordings containing their audio.'
  const noun = held === 1 ? 'recording' : 'recordings'
  return `Sturnus still holds ${formatCount(held)} ${noun} containing their audio.`
}

/* -------------------------------------------------------------------- */
/* Whether a revoke may be offered                                       */
/* -------------------------------------------------------------------- */

export type Revocability = { revocable: true } | { revocable: false, reason: string }

/**
 * Whether to offer the revoke control at all.
 *
 * A consent already withdrawn answers 409 `already_revoked`, every time.
 * Rendering the button and the refusal afterwards would be an interface
 * inviting an action whose outcome it already knows -- the same rule the
 * settings page follows for a required key.
 *
 * A *lapsed* consent is still offered, deliberately. The record is still
 * there; withdrawing it removes it rather than waiting for it, which is the
 * difference between "not counting today" and "gone". It matters the moment
 * somebody rolls `policy_version` back to a previous value, which would
 * otherwise bring every superseded consent quietly back to life.
 */
export function revocability(row: ConsentRow): Revocability {
  if (row.revoked_at) {
    return {
      revocable: false,
      reason:
        `Already withdrawn on ${formatMoment(row.revoked_at)}. There is nothing left to withdraw.`,
    }
  }
  return { revocable: true }
}

/* -------------------------------------------------------------------- */
/* The three things that must be said before a revoke                    */
/* -------------------------------------------------------------------- */

/**
 * The Discord role is not touched. Stated in the confirmation itself, never
 * only in a footnote at the bottom of the page: an administrator who
 * believes this removed the role will not go and remove it, and the member
 * keeps a role that says something untrue about them.
 */
export const ROLE_STAYS_NOTE =
  'This withdraws the consent record Sturnus stores. It does not remove the Discord consent role '
  + 'from this person — the API holds no Discord token, by design, and cannot change anybody’s '
  + 'roles. Recording of them still stops within about five seconds, in the middle of a running '
  + 'session if there is one, because the stored record is what is checked on every frame. If the '
  + 'role should go too, remove it in Discord.'

/**
 * Nothing already recorded is deleted, and the count says how much that is
 * for this person specifically. A general sentence about retention is easy
 * to read past; "and the four recordings that already contain their audio
 * stay" is not.
 */
export function recordingsKeptNote(row: ConsentRow): string {
  const held = row.recordings_with_audio
  const who = personLabel(row)
  if (held === 0) {
    return (
      `Nothing already recorded is deleted. Sturnus holds no recordings containing ${who}’s audio `
      + 'right now, so there is nothing here to erase — and erasing recordings is a separate act '
      + 'either way: /audio purge in Discord.'
    )
  }
  const noun = held === 1 ? 'recording that already contains' : 'recordings that already contain'
  return (
    `Nothing already recorded is deleted. The ${formatCount(held)} ${noun} ${who}’s audio stay `
    + 'exactly where they are. Erasing them is a separate act: /audio purge in Discord.'
  )
}

/** Said before the act rather than discovered after it. */
export const AUDIT_LOG_NOTE =
  'This is written to the audit log: your Discord account, whose consent you withdrew, and when.'

export interface RevokeConfirmation {
  title: string
  /**
   * Three sentences, kept as three. A single paragraph carrying all of
   * them is a paragraph that gets skimmed, and it would be skimmed exactly
   * where the reader most needs to notice that the role and the recordings
   * are not part of this.
   */
  consequences: readonly string[]
  confirmLabel: string
}

/**
 * The confirmation shown before a consent is withdrawn.
 *
 * Always shown -- there is no unattended path to this write. Withdrawing
 * somebody's consent stops them being recorded in a meeting that may be
 * running right now, is done on their behalf without them being asked, and
 * cannot be undone from this console: only the person themselves can grant
 * consent again, with `/consent grant` in Discord.
 */
export function revokeConfirmation(row: ConsentRow): RevokeConfirmation {
  return {
    title: `Withdraw ${personLabel(row)}’s consent?`,
    consequences: [ROLE_STAYS_NOTE, recordingsKeptNote(row), AUDIT_LOG_NOTE],
    confirmLabel: 'Yes, withdraw this consent',
  }
}

/* -------------------------------------------------------------------- */
/* What the API answered                                                 */
/* -------------------------------------------------------------------- */

export interface RevokeResult {
  revoked: boolean
  /** A machine name for the refusal (`already_revoked`,
   *  `no_consent_on_record`), or `null` when the write succeeded. */
  refusal: string | null
}

/** The revoke endpoint's answer. `revoked` is read strictly: a body this
 *  console cannot make sense of must never be reported as a successful
 *  withdrawal, because the only person who would find out otherwise is the
 *  one still being recorded. */
export function parseRevokeResult(payload: unknown): RevokeResult {
  if (!isRecord(payload)) return { revoked: false, refusal: null }
  return { revoked: payload.revoked === true, refusal: asText(payload.refusal) }
}

/**
 * A refusal, as a sentence.
 *
 * Both named refusals mean the same thing to the person reading the screen:
 * the row they clicked is out of date, and nothing they did just now
 * changed anything. They still get different words, because "somebody else
 * already did this" and "Sturnus has no record of this person consenting at
 * all" send an administrator to different places.
 *
 * The `null` case is not merely defensive. `useApi` strips the body off
 * every failed request on purpose -- `ApiError` keeps the status and the
 * path and nothing else, so an in-cluster hostname can never reach the
 * hydration payload -- which means a 409 arrives here as a status with no
 * refusal code attached. That sentence therefore has to be true of both.
 */
export function describeRefusal(refusal: string | null): string {
  switch (refusal) {
    case 'already_revoked':
      return (
        'This consent had already been withdrawn — by somebody else, or in another tab. Nothing '
        + 'changed just now, and nothing needed to.'
      )
    case 'no_consent_on_record':
      return (
        'Sturnus holds no consent record for this person at all, so there was nothing to '
        + 'withdraw. They are not being recorded.'
      )
    default:
      return (
        'Sturnus refused: there is no consent of theirs left to withdraw — it had been withdrawn '
        + 'already, or there was never a record of it. Either way they are not being recorded, '
        + 'and this row was out of date.'
      )
  }
}

export interface RevokeOutcome {
  tone: 'done' | 'refused'
  headline: string
  detail: string
}

/**
 * The panel shown after the write, and never merely "Done".
 *
 * It repeats the two limits afterwards as well as before, because the
 * moment somebody is most likely to believe more happened than did is the
 * moment they have just watched a row change state.
 */
export function revokeOutcome(row: ConsentRow, result: RevokeResult): RevokeOutcome {
  if (!result.revoked) {
    return {
      tone: 'refused',
      headline: 'Nothing was withdrawn.',
      detail: describeRefusal(result.refusal),
    }
  }
  const held = row.recordings_with_audio
  const recordings
    = held === 0
      ? 'no recordings of them are held'
      : `the ${formatCount(held)} ${held === 1 ? 'recording' : 'recordings'} already containing `
        + 'their audio are untouched'
  return {
    tone: 'done',
    headline: `${personLabel(row)}’s consent is withdrawn.`,
    detail:
      'Recording of them stops within about five seconds, mid-session if a meeting is running. '
      + `Their Discord consent role is unchanged, ${recordings}, and this is in the audit log. `
      + 'Only they can grant consent again, with /consent grant.',
  }
}

/* -------------------------------------------------------------------- */
/* When the API says no                                                  */
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
 * Whether the failure means "the row you clicked is out of date".
 *
 * Only a 409 does, and it always does: the endpoint refuses exactly when
 * there is no consent left to withdraw. That makes reloading the list the
 * correct response rather than a hopeful one, which is why this is a
 * decision with a name instead of a `=== 409` in the page.
 */
export function isStaleRow(error: unknown): boolean {
  return statusOf(error) === 409
}

/**
 * A failed request, in a sentence somebody can act on.
 *
 * Built from the status alone. `useApi` throws `ApiError`, which carries no
 * body by design, so there is no server text to prefer here even if the API
 * sent some -- and every sentence below therefore has to stand on its own.
 */
export function describeConsentError(error: unknown): string {
  switch (statusOf(error)) {
    case 401:
      return 'Your session has ended. Sign in again, then retry — nothing was withdrawn.'
    case 403:
      return (
        'You do not administer this server. Administrators are the members holding the role named '
        + 'by that guild’s `admin_role_id`.'
      )
    case 404:
      // The API answers 404 both for a guild that does not exist and for
      // one the caller does not administer, on purpose: it will not confirm
      // the existence of a server to somebody with no business there. So
      // this sentence must cover both without guessing which.
      return (
        'Sturnus does not know this server, or you no longer administer it — it answers the same '
        + 'way to both. Reload the page; the list of servers is rebuilt from Discord.'
      )
    case 409:
      return describeRefusal(null)
    case null:
      return 'Could not reach the API. Nothing was changed; check the connection and retry.'
    default:
      return `Sturnus answered ${statusOf(error)}. Nothing is known about why, and nothing was changed.`
  }
}

/* -------------------------------------------------------------------- */
/* The order the people are listed in                                    */
/* -------------------------------------------------------------------- */

/**
 * Rank by what the reader can still do about a row.
 *
 * 0 -- consent in force: the only rows where withdrawing changes what
 *      happens in a meeting, including one running right now.
 * 1 -- lapsed with the policy version: withdrawing is still offered and
 *      still removes something, but nobody is being recorded under it.
 * 2 -- already withdrawn: history. There is no control on these at all, so
 *      they belong below everything that has one.
 */
function rank(row: ConsentRow): number {
  if (row.active) return 0
  return row.revoked_at ? 2 : 1
}

/**
 * Case-insensitive, and deliberately not `localeCompare`.
 *
 * `localeCompare` sorts by the runtime's locale, and this list is built
 * once on the server and again in the browser. Two ICU versions disagreeing
 * about where "Ö" goes is a hydration mismatch: Vue reports it in the
 * console, and the reader sees the rows reshuffle themselves a moment after
 * the page appears. The same trade `formatCount` makes for thousands
 * separators, for the same reason.
 */
function compareNames(a: string, b: string): number {
  const left = a.toLowerCase()
  const right = b.toLowerCase()
  if (left === right) return 0
  return left < right ? -1 : 1
}

/**
 * Snowflakes in numeric order without ever becoming numbers.
 *
 * Shorter is smaller, and equal lengths compare digit by digit. Comparing
 * them as plain strings would put "1000..." before "999...", and comparing
 * them as numbers would round every id past the safe integer range into a
 * different id.
 */
function compareIds(a: string, b: string): number {
  if (a.length !== b.length) return a.length - b.length
  if (a === b) return 0
  return a < b ? -1 : 1
}

/**
 * The order the rows are listed in.
 *
 * Actionable first (see `rank`), then people with a name before people
 * without one, then by name, then by id.
 *
 * Names before ids because of how the page is actually used: somebody
 * arrives here having been asked about a *person*, and scans for a name. A
 * snowflake is not something anybody scans for -- it is something they
 * search the page for with the browser's own find -- so the nameless rows
 * lose nothing by sitting at the bottom of their rank, and every named row
 * above them gains. Within the nameless run the ids are in numeric order,
 * which is roughly the order the accounts were created, so at least the run
 * is stable and not arbitrary.
 *
 * Every comparison ends at the id, which is unique, so the order is total:
 * two people sharing a display name never swap places between renders.
 */
export function orderConsents(rows: readonly ConsentRow[]): ConsentRow[] {
  return [...rows].sort((a, b) => {
    const byRank = rank(a) - rank(b)
    if (byRank !== 0) return byRank
    if (a.display_name && b.display_name) {
      const byName = compareNames(a.display_name, b.display_name)
      if (byName !== 0) return byName
    } else if (a.display_name || b.display_name) {
      return a.display_name ? -1 : 1
    }
    return compareIds(a.discord_user_id, b.discord_user_id)
  })
}

/**
 * How many of these people are actually being recorded.
 *
 * The headline figure for the page: a list of forty rows where six are in
 * force says something a bare row count does not, and "who can Sturnus
 * record here right now" is the question an administrator came to answer.
 */
export function activeCount(rows: readonly ConsentRow[]): number {
  return rows.filter((row) => row.active).length
}
