/**
 * What a consent record means, and what withdrawing one actually does.
 *
 * All of it lives in `~/utils/consents` rather than in the page, because
 * every one of these is a decision -- which row comes first, whether a
 * revoke is offered at all, how a refusal reads as a sentence -- and a
 * decision embedded in a template can only be tested by rendering one.
 *
 * The wording is asserted here on purpose, and more heavily than anywhere
 * else in this console. This page performs an act on somebody else's behalf
 * whose two limits are invisible: the Discord role stays, and the
 * recordings stay. A test that only checked a boolean would let either of
 * those quietly drop out of the confirmation, and the first person to
 * notice would be a member who was told their audio had been erased.
 */
import { describe, expect, it } from 'vitest'

import {
  AUDIT_LOG_NOTE,
  ROLE_STAYS_NOTE,
  activeCount,
  consentBadge,
  describeConsentError,
  describeRefusal,
  grantedLine,
  identityNote,
  isStaleRow,
  orderConsents,
  parseConsents,
  parseRevokeResult,
  personLabel,
  policyLine,
  recordingsKeptNote,
  recordingsLine,
  revocability,
  revokeConfirmation,
  revokeOutcome,
  withdrawnLine,
  type ConsentRow,
} from '../app/utils/consents'

/** A consent with everything harmless, so each test states only the one
 *  property it is actually about. */
function row(overrides: Partial<ConsentRow> & { discord_user_id: string }): ConsentRow {
  return {
    display_name: null,
    policy_version: '2026-01',
    granted_at: '2026-08-21T12:00:00+00:00',
    revoked_at: null,
    active: true,
    recordings_with_audio: 0,
    ...overrides,
  }
}

/** What `ApiError` looks like to the functions that read a failure. */
function failure(status: number) {
  return { status, path: '/guilds/4711/consents' }
}

describe('reading the consents payload', () => {
  it('reads the rows out of the guild envelope', () => {
    const parsed = parseConsents({
      guild_id: '4711',
      consents: [
        {
          discord_user_id: '100',
          display_name: 'Anna',
          policy_version: '2026-01',
          granted_at: '2026-08-21T12:00:00+00:00',
          revoked_at: null,
          active: true,
          recordings_with_audio: 3,
        },
      ],
    })
    expect(parsed).toHaveLength(1)
    expect(parsed[0]!.display_name).toBe('Anna')
    expect(parsed[0]!.recordings_with_audio).toBe(3)
  })

  it('reads the rows out of a bare list', () => {
    expect(parseConsents([{ discord_user_id: '100' }]).map((r) => r.discord_user_id)).toEqual(['100'])
  })

  it('keeps an id as a string even if it arrived as a number', () => {
    // A snowflake past the safe integer range has already lost its last
    // digits before this function sees it. Stringifying does not undo that;
    // it keeps the revoke URL well-formed so the damage surfaces as a 409
    // rather than as somebody else's consent being withdrawn.
    expect(parseConsents([{ discord_user_id: 100 }])[0]!.discord_user_id).toBe('100')
  })

  it('treats a missing name as no name rather than as an empty one', () => {
    expect(parseConsents([{ discord_user_id: '100' }])[0]!.display_name).toBeNull()
    expect(parseConsents([{ discord_user_id: '100', display_name: '   ' }])[0]!.display_name).toBeNull()
  })

  it('treats a missing active flag as not being recorded', () => {
    // Erring the other way would tell an administrator somebody is being
    // recorded when the bot has already stopped.
    expect(parseConsents([{ discord_user_id: '100' }])[0]!.active).toBe(false)
    expect(parseConsents([{ discord_user_id: '100', active: 'false' }])[0]!.active).toBe(false)
  })

  it('never reports a negative or nonsensical recording count', () => {
    // A defect upstream must not render as "-3 recordings" beside somebody's
    // name, where it reads as a fact about them.
    expect(parseConsents([{ discord_user_id: '1', recordings_with_audio: -3 }])[0]!.recordings_with_audio).toBe(0)
    expect(parseConsents([{ discord_user_id: '1', recordings_with_audio: 'many' }])[0]!.recordings_with_audio).toBe(0)
  })

  it('drops an entry that names nobody', () => {
    expect(parseConsents([{ display_name: 'Anna' }, { discord_user_id: '100' }])).toHaveLength(1)
  })

  it('yields nothing for a payload it cannot make sense of', () => {
    expect(parseConsents(null)).toEqual([])
    expect(parseConsents('nonsense')).toEqual([])
  })
})

describe('naming a person', () => {
  it('uses the display name when there is one', () => {
    expect(personLabel(row({ discord_user_id: '100', display_name: 'Anna' }))).toBe('Anna')
  })

  it('falls back to the whole id, never a shortened one', () => {
    // Snowflakes from the same era share their leading digits, so a
    // truncated id names a group rather than a person.
    expect(personLabel(row({ discord_user_id: '1129384756123456789' }))).toContain(
      '1129384756123456789',
    )
  })

  it('says plainly that a nameless row is showing an id', () => {
    const note = identityNote(row({ discord_user_id: '100' }))!
    expect(note).toContain('Discord user id')
  })

  it('explains why there is no name rather than looking broken', () => {
    // A bare snowflake where every other row has a name reads as a fault in
    // the console. It is not: consent is given in a command, and a name is
    // only learned in a recorded session.
    expect(identityNote(row({ discord_user_id: '100' }))!).toContain('recorded session')
  })

  it('says nothing at all when there is a name', () => {
    expect(identityNote(row({ discord_user_id: '100', display_name: 'Anna' }))).toBeNull()
  })
})

describe('the state a consent is in', () => {
  it('marks a consent that is in force', () => {
    const badge = consentBadge(row({ discord_user_id: '100', active: true }))
    expect(badge.tone).toBe('active')
    expect(badge.label.toLowerCase()).toContain('in force')
  })

  it('takes the API at its word instead of deriving activity from the dates', () => {
    // `active` is authoritative: consent also stops counting when the
    // guild's policy_version moves on, which no date on this row records.
    const badge = consentBadge(row({ discord_user_id: '100', active: false, revoked_at: null }))
    expect(badge.tone).not.toBe('active')
  })

  it('tells a withdrawn consent apart from one whose policy version moved on', () => {
    // The whole point of the badge. One of these the person decided; the
    // other happened to them. Rendering both as a grey "inactive" would
    // hide which, and the two need different things done about them.
    const withdrawn = consentBadge(
      row({ discord_user_id: '100', active: false, revoked_at: '2026-08-22T09:00:00+00:00' }),
    )
    const lapsed = consentBadge(row({ discord_user_id: '101', active: false, revoked_at: null }))
    expect(withdrawn.tone).toBe('withdrawn')
    expect(lapsed.tone).toBe('superseded')
    expect(withdrawn.tone).not.toBe(lapsed.tone)
  })

  it('says outright that a lapsed consent was not withdrawn by anybody', () => {
    const badge = consentBadge(row({ discord_user_id: '100', active: false, revoked_at: null }))
    expect(badge.detail.toLowerCase()).toContain('nobody withdrew')
    expect(badge.detail).toContain('policy_version')
  })

  it('names the version a lapsed consent was given under', () => {
    // It is the field that explains the row, and the one somebody needs
    // before deciding whether bumping the version again is worth it.
    const badge = consentBadge(
      row({ discord_user_id: '100', active: false, revoked_at: null, policy_version: '2025-11' }),
    )
    expect(badge.detail).toContain('2025-11')
  })

  it('says how a lapsed consent comes back, since the console cannot do it', () => {
    const badge = consentBadge(row({ discord_user_id: '100', active: false, revoked_at: null }))
    expect(badge.detail).toContain('/consent grant')
  })

  it('dates a withdrawal rather than merely reporting one', () => {
    const badge = consentBadge(
      row({ discord_user_id: '100', active: false, revoked_at: '2026-08-22T09:00:00+00:00' }),
    )
    expect(badge.detail).toContain('22 Aug 2026')
  })
})

describe('the facts on a row', () => {
  it('writes a moment in UTC and says so', () => {
    // The console has one way of printing a moment, borrowed from
    // `~/utils/format`: the server render cannot know the reader's zone, so
    // a second rendering would disagree with the first and hydration would
    // rewrite every timestamp on the page.
    expect(grantedLine(row({ discord_user_id: '100' }))).toBe('Granted 21 Aug 2026, 12:00 UTC.')
  })

  it('admits an unrecorded grant date instead of printing a dash', () => {
    expect(grantedLine(row({ discord_user_id: '100', granted_at: null })).toLowerCase()).toContain(
      'not recorded',
    )
  })

  it('gives no withdrawal line to a consent nobody withdrew', () => {
    // A lapsed consent borrowing this line would read as a decision the
    // person made, which is exactly what it is not.
    expect(withdrawnLine(row({ discord_user_id: '100', active: false }))).toBeNull()
  })

  it('dates a withdrawal when there was one', () => {
    expect(
      withdrawnLine(row({ discord_user_id: '100', revoked_at: '2026-08-22T09:30:00+00:00' })),
    ).toContain('22 Aug 2026, 09:30 UTC')
  })

  it('names the policy version on every row, not only the lapsed ones', () => {
    expect(policyLine(row({ discord_user_id: '100', policy_version: '2026-01' }))).toContain('2026-01')
  })

  it('counts the recordings still held in singular and plural', () => {
    expect(recordingsLine(row({ discord_user_id: '1', recordings_with_audio: 1 }))).toContain(
      '1 recording containing',
    )
    expect(recordingsLine(row({ discord_user_id: '1', recordings_with_audio: 3 }))).toContain(
      '3 recordings containing',
    )
  })

  it('says none are held rather than printing a zero', () => {
    expect(recordingsLine(row({ discord_user_id: '1', recordings_with_audio: 0 }))).toContain('no recordings')
  })
})

describe('whether a revoke is offered', () => {
  it('offers it for a consent that is in force', () => {
    expect(revocability(row({ discord_user_id: '100', active: true })).revocable).toBe(true)
  })

  it('still offers it for a consent whose policy version moved on', () => {
    // The record is still there. Withdrawing removes it rather than waiting
    // for it, which matters the moment somebody rolls policy_version back
    // to a previous value and every lapsed consent comes back to life.
    expect(
      revocability(row({ discord_user_id: '100', active: false, revoked_at: null })).revocable,
    ).toBe(true)
  })

  it('refuses to offer it for a consent already withdrawn', () => {
    // The endpoint answers 409 `already_revoked`, every time. An interface
    // that offers an action it knows the outcome of is worse than one that
    // explains why it cannot.
    const verdict = revocability(
      row({ discord_user_id: '100', active: false, revoked_at: '2026-08-22T09:00:00+00:00' }),
    )
    expect(verdict.revocable).toBe(false)
  })

  it('says when it was withdrawn instead of merely refusing', () => {
    const verdict = revocability(
      row({ discord_user_id: '100', active: false, revoked_at: '2026-08-22T09:00:00+00:00' }),
    )
    expect(verdict.revocable).toBe(false)
    if (!verdict.revocable) expect(verdict.reason).toContain('22 Aug 2026')
  })
})

describe('confirming a withdrawal', () => {
  const anna = row({ discord_user_id: '100', display_name: 'Anna', recordings_with_audio: 3 })

  it('names the person whose consent is about to go', () => {
    // Two rows apart on a long page, with the same button on each, is how
    // the wrong one gets clicked.
    expect(revokeConfirmation(anna).title).toContain('Anna')
  })

  it('says the Discord role is not removed', () => {
    // The load-bearing sentence of this page. An administrator who believes
    // the role went with it will not go and remove it, and the member keeps
    // a role that says something untrue about them.
    const said = revokeConfirmation(anna).consequences.join(' ')
    expect(said).toContain('does not remove the Discord consent role')
  })

  it('explains why the role cannot be removed rather than just that it is not', () => {
    expect(ROLE_STAYS_NOTE).toContain('no Discord token')
  })

  it('says recording still stops anyway, and how soon', () => {
    // Otherwise "the role stays" reads as "this did nothing".
    const said = revokeConfirmation(anna).consequences.join(' ')
    expect(said).toContain('five seconds')
    expect(said.toLowerCase()).toContain('running session')
  })

  it('says nothing already recorded is deleted', () => {
    // Letting "withdrawn" read as "erased" would answer a data subject's
    // erasure request with a lie.
    const said = revokeConfirmation(anna).consequences.join(' ')
    expect(said).toContain('Nothing already recorded is deleted')
  })

  it('names how many recordings of that person specifically remain', () => {
    // A general sentence about retention is easy to read past; "the 3
    // recordings that already contain Anna's audio stay" is not.
    const said = revokeConfirmation(anna).consequences.join(' ')
    expect(said).toContain('3 recordings that already contain')
    expect(said).toContain('Anna')
  })

  it('names the separate act that does erase them', () => {
    expect(revokeConfirmation(anna).consequences.join(' ')).toContain('/audio purge')
  })

  it('still points at the purge command for somebody with nothing on disk', () => {
    const said = recordingsKeptNote(row({ discord_user_id: '100', recordings_with_audio: 0 }))
    expect(said).toContain('no recordings')
    expect(said).toContain('/audio purge')
  })

  it('says the withdrawal is logged, before it happens', () => {
    expect(revokeConfirmation(anna).consequences).toContain(AUDIT_LOG_NOTE)
    expect(AUDIT_LOG_NOTE).toContain('audit log')
  })

  it('keeps the three facts as three, not as one paragraph', () => {
    // A wall of prose is skimmed exactly where the reader most needs to
    // notice that the role and the recordings are not part of this.
    expect(revokeConfirmation(anna).consequences).toHaveLength(3)
  })

  it('labels the button with what it does, not with "OK"', () => {
    expect(revokeConfirmation(anna).confirmLabel.toLowerCase()).toContain('withdraw')
  })
})

describe('what the revoke endpoint answered', () => {
  it('reads a successful withdrawal', () => {
    expect(parseRevokeResult({ revoked: true, refusal: null })).toEqual({
      revoked: true,
      refusal: null,
    })
  })

  it('reads a refusal and its reason', () => {
    expect(parseRevokeResult({ revoked: false, refusal: 'already_revoked' }).refusal).toBe(
      'already_revoked',
    )
  })

  it('never reports a body it cannot make sense of as a withdrawal', () => {
    // The only person who would find out otherwise is the one still being
    // recorded.
    expect(parseRevokeResult(null).revoked).toBe(false)
    expect(parseRevokeResult({}).revoked).toBe(false)
    expect(parseRevokeResult({ revoked: 'true' }).revoked).toBe(false)
  })

  it('tells the two refusals apart', () => {
    // Both mean the row is out of date; they send an administrator to
    // different places all the same.
    expect(describeRefusal('already_revoked')).not.toBe(describeRefusal('no_consent_on_record'))
    expect(describeRefusal('already_revoked').toLowerCase()).toContain('already been withdrawn')
    expect(describeRefusal('no_consent_on_record').toLowerCase()).toContain('no consent record')
  })

  it('writes a refusal it has no code for so that it is true of both', () => {
    // `useApi` strips the body off every failed request on purpose, so a
    // 409 reaches this console as a status with no refusal attached. The
    // sentence therefore has to cover either reason without guessing.
    const said = describeRefusal(null).toLowerCase()
    expect(said).toContain('withdrawn')
    expect(said).toContain('never a record')
    expect(said).toContain('not being recorded')
  })

  it('never says merely "done" after a successful withdrawal', () => {
    const outcome = revokeOutcome(
      row({ discord_user_id: '100', display_name: 'Anna', recordings_with_audio: 2 }),
      { revoked: true, refusal: null },
    )
    expect(outcome.tone).toBe('done')
    expect(outcome.headline).toContain('Anna')
  })

  it('repeats both limits after the act as well as before it', () => {
    // This is the moment somebody is most likely to believe more happened
    // than did: they have just watched the row change state.
    const outcome = revokeOutcome(
      row({ discord_user_id: '100', display_name: 'Anna', recordings_with_audio: 2 }),
      { revoked: true, refusal: null },
    )
    expect(outcome.detail).toContain('Discord consent role is unchanged')
    expect(outcome.detail).toContain('2 recordings')
    expect(outcome.detail).toContain('audit log')
  })

  it('says who can undo it, since this console cannot', () => {
    const outcome = revokeOutcome(row({ discord_user_id: '100' }), { revoked: true, refusal: null })
    expect(outcome.detail).toContain('/consent grant')
  })

  it('reports a refusal as a refusal, not as a withdrawal', () => {
    const outcome = revokeOutcome(row({ discord_user_id: '100' }), {
      revoked: false,
      refusal: 'already_revoked',
    })
    expect(outcome.tone).toBe('refused')
    expect(outcome.headline.toLowerCase()).toContain('nothing was withdrawn')
  })
})

describe('when the API says no', () => {
  it('explains the 404 without pretending to know which of the two it is', () => {
    // The API answers 404 both for a guild that does not exist and for one
    // the caller does not administer, on purpose.
    const said = describeConsentError(failure(404))
    expect(said).toContain('no longer administer it')
    expect(said.toLowerCase()).toContain('same')
  })

  it('describes a 409 as the row being out of date', () => {
    expect(describeConsentError(failure(409))).toBe(describeRefusal(null))
  })

  it('treats only a 409 as a stale row', () => {
    // A 409 always means there is nothing left to withdraw, which makes
    // reloading the list the correct response rather than a hopeful one.
    expect(isStaleRow(failure(409))).toBe(true)
    expect(isStaleRow(failure(404))).toBe(false)
    expect(isStaleRow(failure(0))).toBe(false)
    expect(isStaleRow(null)).toBe(false)
  })

  it('says nothing was withdrawn when the session has ended', () => {
    expect(describeConsentError(failure(401))).toContain('nothing was withdrawn')
  })

  it('tells an unreachable API apart from an API that refused', () => {
    // `ApiError` uses status 0 for a request that never got a response, and
    // "could not reach the API" and "the API said no" need different words.
    expect(describeConsentError(failure(0)).toLowerCase()).toContain('could not reach')
    expect(describeConsentError(failure(500))).toContain('500')
  })

  it('says nothing was changed for a status it has never heard of', () => {
    expect(describeConsentError(failure(503)).toLowerCase()).toContain('nothing was changed')
  })
})

describe('the order the people are listed in', () => {
  it('puts the consents in force first', () => {
    // They are the only rows where withdrawing changes what happens in a
    // meeting, including one running right now.
    const ordered = orderConsents([
      row({ discord_user_id: '2', display_name: 'Zoe', active: false, revoked_at: null }),
      row({ discord_user_id: '3', display_name: 'Anna', active: true }),
    ])
    expect(ordered.map((r) => r.display_name)).toEqual(['Anna', 'Zoe'])
  })

  it('puts the already-withdrawn rows last, below the lapsed ones', () => {
    // A withdrawn row has no control on it at all, so it belongs below
    // everything that has one.
    const ordered = orderConsents([
      row({
        discord_user_id: '1',
        display_name: 'Aaron',
        active: false,
        revoked_at: '2026-08-01T00:00:00+00:00',
      }),
      row({ discord_user_id: '2', display_name: 'Zoe', active: false, revoked_at: null }),
      row({ discord_user_id: '3', display_name: 'Mia', active: true }),
    ])
    expect(ordered.map((r) => r.display_name)).toEqual(['Mia', 'Zoe', 'Aaron'])
  })

  it('sorts the named people by name, ignoring case', () => {
    const ordered = orderConsents([
      row({ discord_user_id: '1', display_name: 'zoe' }),
      row({ discord_user_id: '2', display_name: 'Anna' }),
      row({ discord_user_id: '3', display_name: 'mia' }),
    ])
    expect(ordered.map((r) => r.display_name)).toEqual(['Anna', 'mia', 'zoe'])
  })

  it('puts the people with no name below the people with one', () => {
    // Somebody arrives here having been asked about a person and scans for
    // a name. Nobody scans for a snowflake -- they search the page for it.
    const ordered = orderConsents([
      row({ discord_user_id: '100', display_name: null }),
      row({ discord_user_id: '200', display_name: 'Zoe' }),
    ])
    expect(ordered.map((r) => r.discord_user_id)).toEqual(['200', '100'])
  })

  it('orders the nameless rows numerically rather than as strings', () => {
    // As plain strings "1000" sorts before "999"; as numbers a snowflake
    // past the safe integer range becomes a different id entirely.
    const ordered = orderConsents([
      row({ discord_user_id: '1000' }),
      row({ discord_user_id: '999' }),
      row({ discord_user_id: '1129384756123456789' }),
    ])
    expect(ordered.map((r) => r.discord_user_id)).toEqual([
      '999',
      '1000',
      '1129384756123456789',
    ])
  })

  it('never leaves two people sharing a name in an arbitrary order', () => {
    // Every comparison ends at the id, which is unique, so the order is
    // total and the rows do not swap places between renders.
    const twice = () =>
      orderConsents([
        row({ discord_user_id: '200', display_name: 'Anna' }),
        row({ discord_user_id: '100', display_name: 'Anna' }),
      ]).map((r) => r.discord_user_id)
    expect(twice()).toEqual(['100', '200'])
    expect(twice()).toEqual(['100', '200'])
  })

  it('leaves the payload it was given alone', () => {
    const given = [row({ discord_user_id: '2' }), row({ discord_user_id: '1' })]
    orderConsents(given)
    expect(given.map((r) => r.discord_user_id)).toEqual(['2', '1'])
  })
})

describe('how many people can actually be recorded', () => {
  it('counts only the consents in force', () => {
    // A list of forty rows where six are in force says something a bare row
    // count does not.
    expect(
      activeCount([
        row({ discord_user_id: '1', active: true }),
        row({ discord_user_id: '2', active: false, revoked_at: null }),
        row({ discord_user_id: '3', active: false, revoked_at: '2026-08-01T00:00:00+00:00' }),
      ]),
    ).toBe(1)
  })

  it('counts nothing in an empty guild', () => {
    expect(activeCount([])).toBe(0)
  })
})
