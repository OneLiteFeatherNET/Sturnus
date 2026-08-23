/**
 * The consent roster as something somebody can work through: one page at a
 * time, several people at once, and a name in front of every snowflake.
 *
 * `~/utils/consents` decides what *one* consent means — its badge, its
 * sentence, whether a withdrawal may be offered at all — and none of that
 * is re-decided here. What this module adds is everything that only exists
 * once the roster is bigger than a screen:
 *
 * - **A page of it, rather than all of it.** `GET .../consents` now
 *   answers `{consents, total, limit, offset}`, and `total` counts
 *   *people* rather than consent rows — the table is append-only, so a
 *   count of rows would grow every time somebody re-granted. The old page
 *   fetched every record a guild ever had and sorted them in the browser;
 *   the order is SQL's now, and `orderConsents` is deliberately not called
 *   from the page any more. A second ordering applied on top of the
 *   server's would reshuffle a page whose neighbours the reader cannot
 *   see, which is a list that disagrees with its own pager.
 * - **A name for people the consent record has none for.** `personLabel`
 *   falls back to `Discord user 100…`, which is honest and unreadable. The
 *   guild directory holds display names for exactly these people, and this
 *   module is where the two meet. Somebody the directory cannot resolve
 *   still renders — as the bare id, with a sentence saying why — never as
 *   a blank and never silently dropped.
 * - **A withdrawal that names more than one person.** The dangerous one.
 *   Everything below about the batch exists to keep two promises: the
 *   confirmation states exactly who it will apply to before it applies to
 *   anything, and the answer is legible per person afterwards. `POST
 *   .../consents/revoke` answers 200 for a mixed result on purpose, so
 *   "seven withdrawn, two already withdrawn, one had no record" is the
 *   ordinary case and not an error condition.
 *
 * **The batch bound and the page size are the same bound.** The API caps a
 * batch at exactly one page of the roster it is withdrawn from, so an
 * interface can never build a request it could not have shown. This module
 * therefore refuses an oversized selection out loud rather than letting
 * the reader discover it as a 400 — and nothing here offers a "select all
 * matching" that could exceed it, because no such request exists.
 *
 * **Every function here returns a translation key, never a sentence.**
 * `i18n/README.md` reserves `admin.consents.*` for this page and says why.
 * The English prose still in `~/utils/consents` predates that rule and
 * moves when the sweep reaches it; nothing new is written in it.
 */
import { type ConsentRow, parseConsents, revocability } from '~/utils/consents'
import type { NamedRow } from '~/utils/directory'
import { effectiveOutcome, localInputFromIso } from '~/utils/effectiveInstant'
import type { Message } from '~/utils/message'
import type { Line } from '~/utils/myConsents'
import { pageSummary } from '~/utils/paging'
import type { UiRow } from '~/utils/uiDisclosureList'
import type { Day } from '~/utils/uiDatePicker'

/**
 * The most people one withdrawal may name.
 *
 * The API's own `MAX_REVOCATIONS_PER_REQUEST`, restated here so the
 * console refuses a request it knows will be refused rather than sending
 * it. It is deliberately equal to the largest page the roster endpoint
 * will serve: the biggest legal batch is exactly one page, which is what
 * makes "never offer a selection you could not have shown" a property of
 * the two endpoints rather than a rule this file has to remember.
 */
export const MAX_BATCH = 100

/* -------------------------------------------------------------------- */
/* One page of the roster                                                */
/* -------------------------------------------------------------------- */

export interface ConsentPage {
  rows: ConsentRow[]
  /**
   * How many **people** have a consent record in this guild — not how many
   * consent rows exist. The table is append-only, so somebody who granted,
   * withdrew and granted again is three rows and one person, and a pager
   * built on the row count would offer pages that hold nobody new.
   */
  total: number
  limit: number
  offset: number
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** A whole non-negative number, or the fallback. A negative offset or a
 *  fractional total is a defect upstream, and arithmetic built on it
 *  produces a pager that offers page −1. */
function asCount(value: unknown, fallback: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return fallback
  return Math.floor(value)
}

/**
 * One page of the roster, envelope and all.
 *
 * The rows themselves go through `parseConsents`, which already tolerates
 * every shape this endpoint has ever sent. What is read here is the window
 * around them, and each field falls back to something that describes a
 * *single-page* list rather than to zero: an API that sends no `total` is
 * one that predates paging, and a console that then rendered "0 people"
 * over twenty visible rows would be reporting its own parser as a fact
 * about the guild.
 */
export function parseConsentPage(payload: unknown, asked: number): ConsentPage {
  const rows = parseConsents(payload)
  const envelope = isRecord(payload) ? payload : {}
  return {
    rows,
    total: asCount(envelope.total, rows.length),
    limit: asCount(envelope.limit, asked),
    offset: asCount(envelope.offset, 0),
  }
}

/**
 * What this page of the list is showing, in words.
 *
 * The arithmetic is `~/utils/paging`'s and is not done twice: it already
 * decided that the upper bound is the last row actually on screen rather
 * than `offset + size`, which is what stops a roster of 47 announcing
 * "41–60 of 47". Only the noun belongs to this page, so only the key is
 * replaced — and which of the two keys it is, is read from the shape of
 * the params rather than from the key it came back with, so renaming a
 * key over there cannot silently pick the plural sentence here.
 */
export function rosterSummary(total: number, offset: number, shown: number): Message | null {
  const summary = pageSummary(total, offset, shown)
  if (!summary) return null
  const several = summary.params !== undefined && 'to' in summary.params
  return {
    key: several ? 'admin.consents.roster.showing' : 'admin.consents.roster.showingOne',
    params: summary.params,
  }
}

/**
 * How many people the guild holds a consent record for, and how many of
 * the ones on this page are in force.
 *
 * Two sentences rather than one, and the split is the honest part. The
 * total comes from the API and describes the whole guild; "in force" can
 * only be counted over the rows actually in hand, and the page this list
 * used to be said both about the same set because it had every row. A
 * single sentence would now silently mean "3 of 400 are in force" while
 * counting 3 of the 20 on screen.
 */
export function rosterCount(total: number): Message {
  return { key: 'admin.consents.roster.total', params: { count: total } }
}

export function rosterInForce(inForce: number, shown: number): Message {
  return {
    key: 'admin.consents.roster.inForce',
    params: { count: inForce, shown },
  }
}

/* -------------------------------------------------------------------- */
/* Putting a name to a snowflake                                         */
/* -------------------------------------------------------------------- */

/**
 * Where the name on a row came from, which decides what has to be said
 * underneath it.
 *
 * - `record` — the consent row carried a display name. Nothing to add.
 * - `directory` — the guild directory supplied one. Worth a note: the name
 *   describes Discord as the last mirror sweep saw it, and the consent
 *   record itself still has none.
 * - `unresolved` — the directory was read and holds no entry for this id.
 * - `unknown` — there was no directory to ask, because the request for it
 *   failed or has not arrived. Not the same fact as `unresolved`, and
 *   rendering both as "could not be resolved" would tell somebody their
 *   guild is missing a member when the console simply did not look.
 */
export type NameSource = 'record' | 'directory' | 'unresolved' | 'unknown'

export interface RosterPerson {
  id: string
  /** What to render. Never empty, and the whole id when there is no name:
   *  snowflakes minted in the same era share their leading digits, so a
   *  truncated one names a group rather than a person. */
  label: string
  source: NameSource
  /**
   * When their consent was granted, carried alongside the name so that a
   * bulk withdrawal's effective instant can still be checked against it
   * after the reader has paged away from the row. A selection outlives the
   * page it was made on; the facts needed to validate it have to as well.
   */
  grantedAt: string | null
}

/**
 * One person, named as well as this console can name them.
 *
 * `members` is `null` when there is no directory to consult, which is a
 * different answer from an empty one — see `NameSource`. A directory row
 * whose name is its own id is treated as no name at all: `parseDirectory`
 * falls back to the id for a member Discord sent no name for, and copying
 * that into the label would turn "unresolved" into "resolved to a
 * snowflake" without changing a single character on screen.
 */
export function rosterPerson(
  row: ConsentRow,
  members: readonly NamedRow[] | null,
): RosterPerson {
  const id = row.discord_user_id
  const base = { id, grantedAt: row.granted_at }
  if (row.display_name) return { ...base, label: row.display_name, source: 'record' }
  if (members === null) return { ...base, label: id, source: 'unknown' }
  const found = members.find((member) => member.id === id)
  if (found && found.name !== id) return { ...base, label: found.name, source: 'directory' }
  return { ...base, label: id, source: 'unresolved' }
}

/**
 * The sentence a row needs under its name, or `null` when it needs none.
 *
 * A bare snowflake where every other row has a name reads as a fault in
 * the console, and it is not one: consent is given by a Discord command,
 * and a display name is only learned when somebody turns up in a session
 * that was recorded. The three cases that produce one are three different
 * facts and get three different sentences — a name borrowed from the
 * directory is not the same claim as no name anywhere, and no name
 * anywhere is not the same as not having looked.
 */
export function nameNote(person: RosterPerson): Message | null {
  switch (person.source) {
    case 'record':
      return null
    case 'directory':
      return { key: 'admin.consents.roster.fromDirectory' }
    case 'unresolved':
      return { key: 'admin.consents.roster.unresolved' }
    default:
      return { key: 'admin.consents.roster.unlisted' }
  }
}

/* -------------------------------------------------------------------- */
/* Which rows a bulk action may touch                                    */
/* -------------------------------------------------------------------- */

/**
 * A row of the list: the consent, the person, and whether a bulk action may
 * touch them.
 *
 * One shape rather than three parallel arrays indexed by position. The
 * disclosure list hands a row back to its slots, so what it is given is
 * what the template gets — and a lookup by index into a second array is
 * exactly how a name ends up beside somebody else's consent when one of
 * the two is filtered and the other is not.
 */
export interface RosterEntry extends UiRow {
  row: ConsentRow
  person: RosterPerson
}

/**
 * The page as rows, with the already-withdrawn ones locked out of the
 * selection.
 *
 * `revocability` decides, exactly as it does for the single-row button:
 * this page never offers an action it knows will fail, and a header
 * checkbox that ticked a withdrawn consent would promise a withdrawal the
 * API answers `already_revoked` to. Leaving them unselectable is also what
 * keeps "select this page" honest — the count it produces is the count the
 * request will carry.
 *
 * A *lapsed* consent is still selectable, for the reason `revocability`
 * gives: the record is still there, and withdrawing removes it rather than
 * waiting for it.
 */
export function rosterEntries(
  rows: readonly ConsentRow[],
  members: readonly NamedRow[] | null,
): RosterEntry[] {
  return rows.map((row) => ({
    id: row.discord_user_id,
    selectable: revocability(row).revocable,
    row,
    person: rosterPerson(row, members),
  }))
}

/**
 * Everybody ever ticked, remembered by id.
 *
 * A selection survives a page change on purpose — that is the whole reason
 * `selectionSummary` exists — which means the confirmation has to name
 * people whose rows are no longer in hand. Remembering them as they are
 * seen is the only way to state exactly who a withdrawal applies to
 * without a second request, and a confirmation that could not name them
 * would be a confirmation of a number.
 */
export function rememberPeople(
  known: Readonly<Record<string, RosterPerson>>,
  people: readonly RosterPerson[],
): Record<string, RosterPerson> {
  const next = { ...known }
  for (const person of people) next[person.id] = person
  return next
}

/**
 * The selection, as people.
 *
 * An id nobody has a record of still comes back — as itself, marked
 * `unknown` — rather than being dropped. A person quietly missing from the
 * list in a confirmation is the one case this whole panel exists to
 * prevent: the reader would approve a withdrawal for a set of names and
 * the request would carry one more.
 */
export function chosenPeople(
  known: Readonly<Record<string, RosterPerson>>,
  selected: readonly string[],
): RosterPerson[] {
  return selected.map(
    (id) => known[id] ?? { id, label: id, source: 'unknown' as const, grantedAt: null },
  )
}

/* -------------------------------------------------------------------- */
/* Whether the batch may be sent                                         */
/* -------------------------------------------------------------------- */

export type BatchVerdict = { ok: true } | { ok: false, problem: Message }

/**
 * Whether this selection is a request the API will accept.
 *
 * Both refusals are client-side and both produce a sentence rather than a
 * 400 whose body `useApi` deliberately throws away. The oversized one is
 * the one that matters: a selection grows a page at a time, so the reader
 * who reaches the bound reached it gradually and has no way of knowing
 * where it is unless the console says so with the number in it.
 */
export function batchVerdict(selected: readonly string[]): BatchVerdict {
  if (selected.length === 0) {
    return { ok: false, problem: { key: 'admin.consents.bulk.none' } }
  }
  if (selected.length > MAX_BATCH) {
    return {
      ok: false,
      problem: {
        key: 'admin.consents.bulk.tooMany',
        params: { count: selected.length, max: MAX_BATCH },
      },
    }
  }
  return { ok: true }
}

/**
 * The latest moment any of these consents was granted, or `null`.
 *
 * The floor for a bulk withdrawal's effective instant. A withdrawal cannot
 * take effect before the consent it withdraws, and the API checks that per
 * person — so an instant legal for nine people and not for the tenth earns
 * nine withdrawals and one `effective_before_grant`. Checking against the
 * latest grant refuses that whole batch up front, with a sentence naming
 * the moment, rather than half-applying it and explaining afterwards.
 *
 * A consent whose grant instant was never recorded contributes no bound,
 * because a missing field is not a constraint.
 */
export function latestGrant(people: readonly RosterPerson[]): string | null {
  let latest: string | null = null
  let at = Number.NEGATIVE_INFINITY
  for (const person of people) {
    if (!person.grantedAt) continue
    const when = Date.parse(person.grantedAt)
    if (Number.isNaN(when) || when <= at) continue
    at = when
    latest = person.grantedAt
  }
  return latest
}

/**
 * The earliest day the picker will offer, in the reader's own zone.
 *
 * A bound somebody can see beats one they discover by tripping over it.
 * The offset is a parameter rather than read from a `Date` in here: the
 * whole daylight-saving correctness of this feature is that the offset
 * belongs to the *chosen* moment, and a function that read the browser's
 * current one would be an hour out for half the year — see
 * `~/utils/effectiveInstant`.
 */
export function grantFloor(iso: string | null, offsetMinutes: number): Day | null {
  if (!iso) return null
  const local = localInputFromIso(iso, offsetMinutes)
  return local ? local.slice(0, 10) : null
}

/* -------------------------------------------------------------------- */
/* What must be said before a batch is sent                              */
/* -------------------------------------------------------------------- */

export interface BulkConfirmation {
  /** How many, in the heading, so the size of the act is the first thing
   *  read rather than something counted off a list. */
  title: Message
  lead: Message
  /**
   * Exactly who, by name, and in the order they were ticked. Not a count,
   * and not "the selected rows": the reader may be looking at a different
   * page entirely, and a confirmation they cannot check against is a
   * confirmation of nothing.
   */
  people: RosterPerson[]
  /**
   * Three sentences, kept as three, for the reason
   * `~/utils/consents` keeps the single-row ones as three: a paragraph
   * carrying all of them is skimmed exactly where the reader most needs to
   * notice that the roles and the recordings are not part of this.
   */
  consequences: Message[]
  confirmLabel: Message
}

export function bulkConfirmation(people: readonly RosterPerson[]): BulkConfirmation {
  return {
    title: { key: 'admin.consents.bulk.title', params: { count: people.length } },
    lead: { key: 'admin.consents.bulk.lead', params: { count: people.length } },
    people: [...people],
    consequences: [
      { key: 'admin.consents.bulk.roleStays' },
      { key: 'admin.consents.bulk.recordingsKept' },
      { key: 'admin.consents.bulk.audit' },
    ],
    confirmLabel: { key: 'admin.consents.bulk.confirm' },
  }
}

/**
 * The request body, from the selection and the chosen instant.
 *
 * Ids stay strings — a snowflake exceeds JavaScript's safe integer range,
 * and the API refuses a JSON number rather than coercing it, precisely so
 * that an id which lost its last digits can never be mistaken for one that
 * did not. Repeats are dropped here rather than earning a 400: the API
 * refuses a duplicate because it answers one outcome per name and cannot
 * say which of two identical names an outcome belongs to, which is a
 * reason to send each name once, not a reason to send the request twice.
 *
 * **No instant, no field.** Pressing straight through without opening the
 * control sends the body it always would have, and the API stamps its own
 * `now` — so "the default is now" stays a fact about the wire.
 */
export function bulkRevokeBody(
  selected: readonly string[],
  instant: string | null,
): { discord_user_ids: string[], effective_at?: string } {
  const ids: string[] = []
  for (const id of selected) {
    if (!ids.includes(id)) ids.push(id)
  }
  return instant ? { discord_user_ids: ids, effective_at: instant } : { discord_user_ids: ids }
}

/* -------------------------------------------------------------------- */
/* What the API answered, person by person                               */
/* -------------------------------------------------------------------- */

export interface PersonOutcome {
  discord_user_id: string
  revoked: boolean
  /** One of the bounded literals the single-person endpoint uses, or
   *  `null`. The two endpoints share the vocabulary on purpose, so this
   *  console writes one sentence per refusal rather than two. */
  refusal: string | null
  effective_at: string | null
  recordings_from_effective_at: number
}

export interface BulkRevokeResult {
  guild_id: string | null
  requested: number
  revoked: number
  refused: number
  outcomes: PersonOutcome[]
}

function asText(value: unknown): string | null {
  if (value === null || value === undefined) return null
  const text = typeof value === 'string' ? value : String(value)
  return text.trim() === '' ? null : text
}

/**
 * The batch endpoint's answer.
 *
 * `revoked` is read strictly, per person, for the reason
 * `parseRevokeResult` reads it strictly: a body this console cannot make
 * sense of must never be reported as a successful withdrawal, because the
 * only person who would find out otherwise is the one still being
 * recorded. An outcome naming nobody is dropped — there is no person it
 * could be shown against — and the tallies are recounted from the outcomes
 * rather than taken from the envelope, so a `revoked: 7` beside six
 * successful outcomes cannot put a number on screen that the list below it
 * contradicts.
 */
export function parseBulkRevoke(payload: unknown): BulkRevokeResult {
  const envelope = isRecord(payload) ? payload : {}
  const raw = Array.isArray(envelope.outcomes) ? envelope.outcomes : []
  const outcomes: PersonOutcome[] = []
  for (const entry of raw) {
    if (!isRecord(entry)) continue
    const id = asText(entry.discord_user_id) ?? asText(entry.user_id)
    if (!id) continue
    outcomes.push({
      discord_user_id: id,
      revoked: entry.revoked === true,
      refusal: asText(entry.refusal),
      effective_at: asText(entry.effective_at),
      recordings_from_effective_at: asCount(entry.recordings_from_effective_at, 0),
    })
  }
  const revoked = outcomes.filter((outcome) => outcome.revoked).length
  return {
    guild_id: asText(envelope.guild_id),
    requested: asCount(envelope.requested, outcomes.length),
    revoked,
    refused: outcomes.length - revoked,
    outcomes,
  }
}

export interface OutcomeRow {
  person: RosterPerson
  tone: 'done' | 'refused'
  /** What happened to this one person, in one sentence. */
  sentence: Message
  /**
   * What the API says it did with the instant, borrowed entire from
   * `~/utils/effectiveInstant`. The console's arithmetic and the API's are
   * two arithmetics and only one of them has the recordings table, so this
   * is built from the echoed `effective_at` rather than from the request.
   */
  detail: Line[]
}

/**
 * One row per person, and never one sentence for the batch.
 *
 * This is the whole reason the endpoint answers 200 for a mixed result. A
 * batch of ten that came back as "some were refused" is an administrator
 * who now has to withdraw all ten again one at a time to find out which;
 * "already withdrawn" against a name is something they can act on, and
 * "Sturnus holds no consent record for this person at all" against another
 * sends them somewhere different.
 *
 * The outcomes are matched to people by id rather than by position. The
 * API does answer index-for-index, and relying on that would mean a single
 * dropped entry renames every outcome after it — which is the one kind of
 * error here that is invisible on screen, because the wrong sentence
 * against the wrong name still reads perfectly.
 */
export function bulkOutcomeRows(
  result: BulkRevokeResult,
  known: Readonly<Record<string, RosterPerson>>,
): OutcomeRow[] {
  return result.outcomes.map((outcome) => {
    const person = known[outcome.discord_user_id] ?? {
      id: outcome.discord_user_id,
      label: outcome.discord_user_id,
      source: 'unknown' as const,
      grantedAt: null,
    }
    if (outcome.revoked) {
      return {
        person,
        tone: 'done' as const,
        sentence: { key: 'admin.consents.bulk.outcomeWithdrawn' },
        // The instant is not restated in the sentence above. It is
        // `effectiveOutcome`'s to say, it is already said in that voice
        // everywhere else on this page, and two renderings of one moment
        // are two chances to render it differently.
        detail: effectiveOutcome(outcome.effective_at, outcome.recordings_from_effective_at),
      }
    }
    return {
      person,
      tone: 'refused' as const,
      sentence: { key: refusalKey(outcome.refusal) },
      // No instant line under a refusal. Repeating "it takes effect on
      // Tuesday" beneath "nothing was withdrawn" would describe a
      // withdrawal that does not exist.
      detail: [],
    }
  })
}

/**
 * A refusal, as a key.
 *
 * The literals are the API's and are bounded; the default covers both an
 * unnamed refusal and one this console predates. It has to be true of
 * every case it can stand in for, which is why it says what is certain —
 * nothing of theirs changed — rather than guessing at a cause.
 */
export function refusalKey(refusal: string | null): string {
  switch (refusal) {
    case 'already_revoked':
      return 'admin.consents.bulk.outcomeAlready'
    case 'no_consent_on_record':
      return 'admin.consents.bulk.outcomeNoRecord'
    case 'effective_before_grant':
      return 'admin.consents.bulk.outcomeBeforeGrant'
    default:
      return 'admin.consents.bulk.outcomeRefused'
  }
}

/**
 * The headline over the per-person list.
 *
 * A tally, and only ever a tally: it says how many of the names given came
 * back withdrawn, and the rows underneath say which. Collapsing the whole
 * answer into this sentence is exactly what the per-person list exists to
 * prevent, so it deliberately does not attempt to name the refusals.
 */
export function bulkTally(result: BulkRevokeResult): Message {
  return {
    key: 'admin.consents.bulk.tally',
    params: { count: result.revoked, requested: result.requested },
  }
}
