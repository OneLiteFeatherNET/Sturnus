/**
 * The consent roster as something somebody can work through.
 *
 * Three things are checked here, and all three are ways a paged list with
 * a bulk action lies without looking wrong:
 *
 * - **A window that describes a different list than the one on screen.** A
 *   `total` counting consent rows rather than people, a page summary
 *   reading `41–60 of 47`, an envelope from an API that predates paging
 *   reported as "0 people" over twenty visible rows.
 * - **A name that is not this person's.** The directory resolves the
 *   nameless, and the one thing it must never do is quietly hand back a
 *   snowflake as though it were a name — nor drop somebody it cannot find.
 * - **A batch that does something other than what the confirmation said.**
 *   The confirmation names people whose rows the reader can no longer see,
 *   the request may not exceed a bound the reader was never shown, and the
 *   answer has to be legible one person at a time.
 */
import { describe, expect, it } from 'vitest'

import type { ConsentRow } from '../app/utils/consents'
import {
  MAX_BATCH,
  type RosterPerson,
  batchVerdict,
  bulkConfirmation,
  bulkOutcomeRows,
  bulkRevokeBody,
  bulkTally,
  chosenPeople,
  grantFloor,
  latestGrant,
  nameNote,
  parseBulkRevoke,
  parseConsentPage,
  refusalKey,
  rememberPeople,
  rosterCount,
  rosterInForce,
  rosterEntries,
  rosterPerson,
  rosterSummary,
} from '../app/utils/consentRoster'

function row(over: Partial<ConsentRow> = {}): ConsentRow {
  return {
    discord_user_id: '100000000000000001',
    display_name: 'Ada',
    policy_version: '3',
    granted_at: '2026-01-04T10:00:00+00:00',
    revoked_at: null,
    active: true,
    scope: 'audio',
    recordings_with_audio: 0,
    ...over,
  }
}

function person(over: Partial<RosterPerson> = {}): RosterPerson {
  return { id: 'a', label: 'Ada', source: 'record', grantedAt: null, ...over }
}

describe('one page of the roster', () => {
  it('reads the rows and the window around them', () => {
    const page = parseConsentPage(
      { consents: [{ discord_user_id: '1' }, { discord_user_id: '2' }], total: 137, limit: 20, offset: 40 },
      20,
    )
    expect(page.rows.map((held) => held.discord_user_id)).toEqual(['1', '2'])
    expect(page.total).toBe(137)
    expect(page.limit).toBe(20)
    expect(page.offset).toBe(40)
  })

  it('reports a total the API sent even when it exceeds the rows in hand', () => {
    // The whole point of the endpoint: `total` counts people in the guild,
    // the rows count the page. A parser that reconciled them would be a
    // pager that only ever offers one page.
    const page = parseConsentPage({ consents: [{ discord_user_id: '1' }], total: 400 }, 20)
    expect(page.total).toBe(400)
  })

  it('falls back to a single page rather than to zero', () => {
    // An API that predates paging sends no envelope at all. Reporting "0
    // people" over rows that are visibly there would put this console's
    // parser on screen as a fact about the guild.
    const page = parseConsentPage([{ discord_user_id: '1' }, { discord_user_id: '2' }], 20)
    expect(page.total).toBe(2)
    expect(page.limit).toBe(20)
    expect(page.offset).toBe(0)
  })

  it('refuses a negative or fractional window', () => {
    const page = parseConsentPage({ consents: [], total: -5, limit: 1.5, offset: -1 }, 20)
    expect(page.total).toBe(0)
    expect(page.limit).toBe(1)
    expect(page.offset).toBe(0)
  })

  it('survives a payload that is not a page at all', () => {
    const page = parseConsentPage('nonsense', 20)
    expect(page.rows).toEqual([])
    expect(page.total).toBe(0)
  })
})

describe('saying which slice is on screen', () => {
  it('names the range and the total', () => {
    expect(rosterSummary(137, 40, 20)).toEqual({
      key: 'admin.consents.roster.showing',
      params: { from: 41, to: 60, total: 137 },
    })
  })

  it('stops at the last row actually shown, not at the end of the window', () => {
    // A roster of 47 announcing "41-60 of 47" is a list nobody trusts
    // about anything else either.
    expect(rosterSummary(47, 40, 7)?.params).toEqual({ from: 41, to: 47, total: 47 })
  })

  it('has a sentence of its own for a page holding one person', () => {
    expect(rosterSummary(41, 40, 1)).toEqual({
      key: 'admin.consents.roster.showingOne',
      params: { from: 41, total: 41 },
    })
  })

  it('says nothing at all about an empty list', () => {
    // The empty state already says it in a full sentence; an arithmetical
    // second saying of it underneath reads as a fault.
    expect(rosterSummary(0, 0, 0)).toBeNull()
  })

  it('counts people for the guild and force for the page, in two sentences', () => {
    // One sentence would now mean "3 of 400 are in force" while counting 3
    // of the 20 rows in hand.
    expect(rosterCount(400)).toEqual({ key: 'admin.consents.roster.total', params: { count: 400 } })
    expect(rosterInForce(3, 20)).toEqual({
      key: 'admin.consents.roster.inForce',
      params: { count: 3, shown: 20 },
    })
  })
})

describe('putting a name to a snowflake', () => {
  const MEMBERS = [{ id: '100000000000000001', name: 'Ada Lovelace' }]

  it('prefers the name on the consent record', () => {
    const named = rosterPerson(row({ display_name: 'Ada' }), MEMBERS)
    expect(named).toMatchObject({ label: 'Ada', source: 'record' })
    expect(nameNote(named)).toBeNull()
  })

  it('borrows a name from the guild directory, and says that it did', () => {
    const named = rosterPerson(row({ display_name: null }), MEMBERS)
    expect(named).toMatchObject({ label: 'Ada Lovelace', source: 'directory' })
    expect(nameNote(named)).toEqual({ key: 'admin.consents.roster.fromDirectory' })
  })

  it('renders somebody the directory has no row for as their bare id, with a note', () => {
    // Never a blank, and never silently dropped: this person consented,
    // and a roster that omits them is a roster that is wrong about who
    // may be recorded.
    const named = rosterPerson(row({ display_name: null, discord_user_id: '999' }), MEMBERS)
    expect(named).toMatchObject({ label: '999', source: 'unresolved' })
    expect(nameNote(named)).toEqual({ key: 'admin.consents.roster.unresolved' })
  })

  it('will not accept a directory row whose name is its own id', () => {
    // `parseDirectory` labels a member Discord sent no name for with their
    // id. Copying that into the label would turn "unresolved" into
    // "resolved" without changing a character on screen.
    const named = rosterPerson(row({ display_name: null, discord_user_id: '77' }), [
      { id: '77', name: '77' },
    ])
    expect(named.source).toBe('unresolved')
  })

  it('distinguishes "the directory has no such member" from "there was no directory"', () => {
    // Two different facts. Telling somebody their guild is missing a
    // member when the console simply did not look is a support ticket
    // about Discord that belongs to the console.
    const named = rosterPerson(row({ display_name: null }), null)
    expect(named.source).toBe('unknown')
    expect(nameNote(named)).toEqual({ key: 'admin.consents.roster.unlisted' })
  })

  it('carries the grant instant along with the name', () => {
    // A selection outlives the page it was made on, so the facts needed to
    // validate it have to as well.
    expect(rosterPerson(row({ granted_at: '2026-01-04T10:00:00+00:00' }), null).grantedAt).toBe(
      '2026-01-04T10:00:00+00:00',
    )
  })

  it('names a whole page in the order it was given', () => {
    const entries = rosterEntries(
      [row({ discord_user_id: 'a' }), row({ discord_user_id: 'b' })],
      null,
    )
    expect(entries.map((entry) => entry.person.id)).toEqual(['a', 'b'])
  })
})

describe('which rows a bulk withdrawal may touch', () => {
  it('locks an already-withdrawn consent out of the selection', () => {
    // The API answers `already_revoked` every time, and a header checkbox
    // that ticked one would promise a withdrawal that is then refused.
    const entries = rosterEntries(
      [
        row({ discord_user_id: 'a' }),
        row({ discord_user_id: 'b', revoked_at: '2026-02-01T00:00:00+00:00', active: false }),
      ],
      null,
    )
    expect(entries.map((entry) => [entry.id, entry.selectable])).toEqual([
      ['a', true],
      ['b', false],
    ])
  })

  it('still offers a lapsed consent', () => {
    // The record is still there; withdrawing removes it rather than
    // waiting for it, which matters the moment somebody rolls
    // `policy_version` back.
    expect(rosterEntries([row({ active: false, revoked_at: null })], null)[0]?.selectable).toBe(true)
  })

  it('carries the consent and the person on the row itself', () => {
    // One shape rather than two arrays indexed by position: a lookup by
    // index is how a name ends up beside somebody else's consent.
    const entry = rosterEntries([row({ display_name: null })], [
      { id: '100000000000000001', name: 'Ada Lovelace' },
    ])[0]
    expect(entry?.row.discord_user_id).toBe('100000000000000001')
    expect(entry?.person.label).toBe('Ada Lovelace')
  })
})

describe('remembering who was ticked', () => {
  it('keeps people the reader has paged away from', () => {
    const known = rememberPeople({}, [person({ id: 'a' }), person({ id: 'b', label: 'Grace' })])
    const later = rememberPeople(known, [person({ id: 'c', label: 'Alan' })])
    expect(chosenPeople(later, ['a', 'c']).map((held) => held.label)).toEqual(['Ada', 'Alan'])
  })

  it('does not mutate what it was given', () => {
    const known = {}
    rememberPeople(known, [person({ id: 'a' })])
    expect(known).toEqual({})
  })

  it('returns an unknown id as itself rather than dropping it', () => {
    // A person quietly missing from a confirmation is the one failure this
    // panel exists to prevent: the reader approves a list of names and the
    // request carries one more.
    expect(chosenPeople({}, ['404'])).toEqual([
      { id: '404', label: '404', source: 'unknown', grantedAt: null },
    ])
  })

  it('keeps the selection in the order it was made', () => {
    const known = rememberPeople({}, [person({ id: 'a' }), person({ id: 'b', label: 'Grace' })])
    expect(chosenPeople(known, ['b', 'a']).map((held) => held.id)).toEqual(['b', 'a'])
  })
})

describe('whether the batch may be sent at all', () => {
  it('accepts a selection within the bound', () => {
    expect(batchVerdict(['a', 'b'])).toEqual({ ok: true })
  })

  it('refuses an empty selection', () => {
    expect(batchVerdict([])).toEqual({
      ok: false,
      problem: { key: 'admin.consents.bulk.none' },
    })
  })

  it('refuses more people than the API will take, and says the number', () => {
    // A selection grows a page at a time, so whoever reaches the bound
    // reached it gradually and cannot know where it is unless it is said.
    const many = Array.from({ length: MAX_BATCH + 1 }, (_, index) => String(index))
    expect(batchVerdict(many)).toEqual({
      ok: false,
      problem: {
        key: 'admin.consents.bulk.tooMany',
        params: { count: MAX_BATCH + 1, max: MAX_BATCH },
      },
    })
  })

  it('accepts exactly the bound', () => {
    const exactly = Array.from({ length: MAX_BATCH }, (_, index) => String(index))
    expect(batchVerdict(exactly).ok).toBe(true)
  })
})

describe('the floor a bulk withdrawal may take effect from', () => {
  it('is the latest grant among the people chosen', () => {
    // An instant legal for nine people and not the tenth earns nine
    // withdrawals and one refusal. Checking the latest refuses the batch
    // up front instead.
    expect(
      latestGrant([
        person({ grantedAt: '2026-01-04T10:00:00+00:00' }),
        person({ grantedAt: '2026-03-09T08:00:00+00:00' }),
        person({ grantedAt: '2026-02-01T00:00:00+00:00' }),
      ]),
    ).toBe('2026-03-09T08:00:00+00:00')
  })

  it('ignores a grant instant that was never recorded', () => {
    expect(latestGrant([person({ grantedAt: null }), person({ grantedAt: 'not a moment' })])).toBeNull()
  })

  it('is null when nobody is chosen', () => {
    expect(latestGrant([])).toBeNull()
  })

  it('becomes the earliest day the picker offers, in the reader’s own zone', () => {
    // Berlin in January is +01:00, so 00:30 UTC is already the 5th there.
    expect(grantFloor('2026-01-05T00:30:00+00:00', -60)).toBe('2026-01-05')
    // And the same instant west of Greenwich is still the 4th.
    expect(grantFloor('2026-01-05T00:30:00+00:00', 300)).toBe('2026-01-04')
  })

  it('offers no floor at all where there is no grant instant', () => {
    expect(grantFloor(null, 0)).toBeNull()
    expect(grantFloor('not a moment', 0)).toBeNull()
  })
})

describe('what is said before a batch is sent', () => {
  const PEOPLE = [person({ id: 'a' }), person({ id: 'b', label: 'Grace' })]

  it('states exactly who, by name, and how many', () => {
    const confirmation = bulkConfirmation(PEOPLE)
    expect(confirmation.title).toEqual({
      key: 'admin.consents.bulk.title',
      params: { count: 2 },
    })
    expect(confirmation.people.map((held) => held.label)).toEqual(['Ada', 'Grace'])
  })

  it('keeps the three limits as three sentences', () => {
    // A paragraph carrying all of them is skimmed exactly where the reader
    // most needs to notice that the roles and the recordings are not part
    // of this.
    expect(bulkConfirmation(PEOPLE).consequences.map((held) => held.key)).toEqual([
      'admin.consents.bulk.roleStays',
      'admin.consents.bulk.recordingsKept',
      'admin.consents.bulk.audit',
    ])
  })

  it('does not hand back the array it was given', () => {
    const people = [person({ id: 'a' })]
    bulkConfirmation(people).people.push(person({ id: 'z' }))
    expect(people).toHaveLength(1)
  })
})

describe('the request body', () => {
  it('sends the ids as strings, in the order they were ticked', () => {
    expect(bulkRevokeBody(['200', '100'], null)).toEqual({ discord_user_ids: ['200', '100'] })
  })

  it('sends no instant field at all when none was chosen', () => {
    // Pressing straight through sends the body it always would have, and
    // the API stamps its own `now`.
    expect('effective_at' in bulkRevokeBody(['1'], null)).toBe(false)
  })

  it('sends the instant when one was chosen', () => {
    expect(bulkRevokeBody(['1'], '2026-03-01T12:00:00+01:00')).toEqual({
      discord_user_ids: ['1'],
      effective_at: '2026-03-01T12:00:00+01:00',
    })
  })

  it('names each person once', () => {
    // The API refuses a repeat because it answers one outcome per name and
    // cannot say which of two identical names an outcome belongs to.
    expect(bulkRevokeBody(['1', '2', '1'], null).discord_user_ids).toEqual(['1', '2'])
  })
})

describe('what the API answered', () => {
  const ANSWER = {
    guild_id: '55',
    requested: 3,
    revoked: 1,
    refused: 2,
    outcomes: [
      {
        discord_user_id: '1',
        revoked: true,
        refusal: null,
        effective_at: '2026-03-01T12:00:00+00:00',
        recordings_from_effective_at: 2,
      },
      { discord_user_id: '2', revoked: false, refusal: 'already_revoked' },
      { discord_user_id: '3', revoked: false, refusal: 'no_consent_on_record' },
    ],
  }

  it('reads one outcome per person', () => {
    const result = parseBulkRevoke(ANSWER)
    expect(result.guild_id).toBe('55')
    expect(result.requested).toBe(3)
    expect(result.outcomes).toHaveLength(3)
  })

  it('counts the tallies from the outcomes rather than from the envelope', () => {
    // A `revoked: 7` beside six successful outcomes would put a number on
    // screen that the list below it contradicts.
    const result = parseBulkRevoke({ ...ANSWER, revoked: 7, refused: 0 })
    expect(result.revoked).toBe(1)
    expect(result.refused).toBe(2)
  })

  it('treats anything but a literal true as not withdrawn', () => {
    // The only person who finds out otherwise is the one still being
    // recorded.
    const result = parseBulkRevoke({ outcomes: [{ discord_user_id: '1', revoked: 'true' }] })
    expect(result.outcomes[0]?.revoked).toBe(false)
  })

  it('drops an outcome naming nobody', () => {
    const result = parseBulkRevoke({ outcomes: [{ revoked: true }, { discord_user_id: '1' }] })
    expect(result.outcomes.map((held) => held.discord_user_id)).toEqual(['1'])
  })

  it('survives a body it cannot read', () => {
    expect(parseBulkRevoke(null)).toEqual({
      guild_id: null,
      requested: 0,
      revoked: 0,
      refused: 0,
      outcomes: [],
    })
  })
})

describe('the mixed answer, person by person', () => {
  const KNOWN = {
    1: person({ id: '1', label: 'Ada' }),
    2: person({ id: '2', label: 'Grace' }),
  }

  it('gives each person their own sentence', () => {
    // The reason the endpoint answers 200 for a mixed result. "Some were
    // refused" is an administrator who has to withdraw all ten again one
    // at a time to find out which.
    const rows = bulkOutcomeRows(
      parseBulkRevoke({
        outcomes: [
          { discord_user_id: '1', revoked: true, effective_at: null },
          { discord_user_id: '2', revoked: false, refusal: 'already_revoked' },
        ],
      }),
      KNOWN,
    )
    expect(rows.map((held) => [held.person.label, held.tone, held.sentence.key])).toEqual([
      ['Ada', 'done', 'admin.consents.bulk.outcomeWithdrawn'],
      ['Grace', 'refused', 'admin.consents.bulk.outcomeAlready'],
    ])
  })

  it('matches outcomes to people by id rather than by position', () => {
    // The API does answer index-for-index; relying on it means one dropped
    // entry renames every outcome after it, and the wrong sentence against
    // the wrong name still reads perfectly.
    const rows = bulkOutcomeRows(
      parseBulkRevoke({
        outcomes: [
          { discord_user_id: '2', revoked: true },
          { discord_user_id: '1', revoked: false, refusal: 'no_consent_on_record' },
        ],
      }),
      KNOWN,
    )
    expect(rows.map((held) => held.person.label)).toEqual(['Grace', 'Ada'])
  })

  it('reports what the API did with the instant, under the people it did it to', () => {
    const rows = bulkOutcomeRows(
      parseBulkRevoke({
        outcomes: [
          {
            discord_user_id: '1',
            revoked: true,
            effective_at: '2026-03-01T12:00:00+00:00',
            recordings_from_effective_at: 2,
          },
        ],
      }),
      KNOWN,
    )
    expect(rows[0]?.detail.map((line) => line.key)).toEqual([
      'admin.consents.effective.tookEffect',
      'admin.consents.effective.fromMany',
    ])
  })

  it('says nothing about an instant under a refusal', () => {
    // "It takes effect on Tuesday" beneath "nothing was withdrawn"
    // describes a withdrawal that does not exist.
    const rows = bulkOutcomeRows(
      parseBulkRevoke({
        outcomes: [
          {
            discord_user_id: '1',
            revoked: false,
            refusal: 'already_revoked',
            effective_at: '2026-03-01T12:00:00+00:00',
          },
        ],
      }),
      KNOWN,
    )
    expect(rows[0]?.detail).toEqual([])
  })

  it('names somebody it has no record of rather than leaving the row blank', () => {
    const rows = bulkOutcomeRows(
      parseBulkRevoke({ outcomes: [{ discord_user_id: '404', revoked: true }] }),
      KNOWN,
    )
    expect(rows[0]?.person).toMatchObject({ label: '404', source: 'unknown' })
  })

  it('has a sentence for every refusal the API can name, and one for the ones it cannot', () => {
    expect(refusalKey('already_revoked')).toBe('admin.consents.bulk.outcomeAlready')
    expect(refusalKey('no_consent_on_record')).toBe('admin.consents.bulk.outcomeNoRecord')
    expect(refusalKey('effective_before_grant')).toBe('admin.consents.bulk.outcomeBeforeGrant')
    expect(refusalKey(null)).toBe('admin.consents.bulk.outcomeRefused')
    expect(refusalKey('something_new')).toBe('admin.consents.bulk.outcomeRefused')
  })

  it('heads the list with a tally and leaves the detail to the rows', () => {
    const result = parseBulkRevoke({
      requested: 10,
      outcomes: [
        { discord_user_id: '1', revoked: true },
        { discord_user_id: '2', revoked: false, refusal: 'already_revoked' },
      ],
    })
    expect(bulkTally(result)).toEqual({
      key: 'admin.consents.bulk.tally',
      params: { count: 1, requested: 10 },
    })
  })
})
