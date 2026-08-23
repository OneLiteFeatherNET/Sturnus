/**
 * The instant a withdrawal takes effect, and the two ways of getting it
 * wrong that nothing else would catch.
 *
 * The first is the offset. A `datetime-local` input produces wall-clock
 * time with no zone, the API answers 400 to a naive instant, and the
 * conversion is exactly the kind of code that looks right in the zone its
 * author was sitting in. So the round trip is asserted against a real
 * moment rather than against a string.
 *
 * The second is the bound. A withdrawal cannot take effect before the
 * consent it withdraws; sending one earns a 400 whose body `useApi`
 * deliberately throws away, so the refusal has to happen here, with a
 * sentence somebody wrote.
 */
import { describe, expect, it } from 'vitest'

import {
  effectiveConsequence,
  effectiveKind,
  effectiveOutcome,
  isoFromLocalInput,
  localInputFromIso,
  localOffsetMinutes,
  offsetLabel,
  validateEffectiveAt,
} from '../app/utils/effectiveInstant'

/** Berlin in summer: `getTimezoneOffset` reports -120, and ISO-8601 writes
 *  the same offset as `+02:00`. */
const BERLIN_SUMMER = -120
/** New York in winter: +300, written `-05:00`. */
const NEW_YORK_WINTER = 300

function keysOf(lines: readonly { key: string }[]): string[] {
  return lines.map((line) => line.key)
}

describe('writing an offset', () => {
  it('flips the sign the browser reports into the one ISO-8601 writes', () => {
    // `getTimezoneOffset` counts minutes to *add* to local time to reach
    // UTC, so it is negative east of Greenwich. Getting this backwards
    // moves every instant by twice the offset and looks perfectly
    // plausible in London.
    expect(offsetLabel(BERLIN_SUMMER)).toBe('+02:00')
    expect(offsetLabel(NEW_YORK_WINTER)).toBe('-05:00')
  })

  it('writes UTC as an offset rather than as Z', () => {
    // Both are legal and one of them is a special case. A formatter with
    // no special case cannot get its special case wrong.
    expect(offsetLabel(0)).toBe('+00:00')
  })

  it('writes a half-hour offset in full', () => {
    // India is +05:30 and Nepal is +05:45; an implementation that divides
    // by sixty and stops is wrong for a fifth of the planet.
    expect(offsetLabel(-330)).toBe('+05:30')
    expect(offsetLabel(-345)).toBe('+05:45')
  })

  it('falls back to UTC rather than writing NaN into an instant', () => {
    expect(offsetLabel(Number.NaN)).toBe('+00:00')
  })
})

describe('a datetime-local value on its way to the API', () => {
  it('always carries an offset, and never arrives naive', () => {
    // The whole reason this module exists: the API answers 400 to an
    // instant with no offset, and this is precisely where that mistake
    // gets made.
    const iso = isoFromLocalInput('2026-08-23T14:30', BERLIN_SUMMER)
    expect(iso).toBe('2026-08-23T14:30:00+02:00')
    expect(iso).toMatch(/[+-]\d{2}:\d{2}$/)
  })

  it('means the moment it says it means', () => {
    // 14:30 in Berlin is 12:30 UTC. Asserted as an instant rather than as
    // a string, because a string can be well-formed and two hours out.
    const iso = isoFromLocalInput('2026-08-23T14:30', BERLIN_SUMMER)!
    expect(new Date(iso).toISOString()).toBe('2026-08-23T12:30:00.000Z')
  })

  it('works west of Greenwich too', () => {
    const iso = isoFromLocalInput('2026-01-15T09:00', NEW_YORK_WINTER)!
    expect(iso).toBe('2026-01-15T09:00:00-05:00')
    expect(new Date(iso).toISOString()).toBe('2026-01-15T14:00:00.000Z')
  })

  it('keeps the seconds when the control offers them', () => {
    expect(isoFromLocalInput('2026-08-23T14:30:45', 0)).toBe('2026-08-23T14:30:45+00:00')
  })

  it('refuses a half-typed value rather than guessing at one', () => {
    expect(isoFromLocalInput('', 0)).toBeNull()
    expect(isoFromLocalInput('2026-08-23', 0)).toBeNull()
    expect(isoFromLocalInput('tomorrow', 0)).toBeNull()
  })

  it('refuses a date that does not exist', () => {
    // `Date` rolls 30 February forward into March without complaint, and
    // a withdrawal silently moved by two days is a withdrawal nobody
    // chose.
    expect(isoFromLocalInput('2026-02-30T10:00', 0)).toBeNull()
    expect(isoFromLocalInput('2026-13-01T10:00', 0)).toBeNull()
    expect(isoFromLocalInput('2026-08-23T25:00', 0)).toBeNull()
  })

  it('accepts the leap day of a leap year and refuses it otherwise', () => {
    expect(isoFromLocalInput('2028-02-29T10:00', 0)).toBe('2028-02-29T10:00:00+00:00')
    expect(isoFromLocalInput('2027-02-29T10:00', 0)).toBeNull()
  })
})

describe('an instant on its way back into the control', () => {
  it('renders in the zone it is asked for, not in UTC', () => {
    expect(localInputFromIso('2026-08-23T12:30:00+00:00', BERLIN_SUMMER)).toBe('2026-08-23T14:30')
    expect(localInputFromIso('2026-01-15T14:00:00+00:00', NEW_YORK_WINTER)).toBe('2026-01-15T09:00')
  })

  it('survives the round trip in both directions', () => {
    const original = '2026-08-23T14:30'
    const iso = isoFromLocalInput(original, BERLIN_SUMMER)!
    expect(localInputFromIso(iso, BERLIN_SUMMER)).toBe(original)
  })

  it('yields nothing for an instant it cannot read', () => {
    expect(localInputFromIso('not an instant', 0)).toBeNull()
    expect(localInputFromIso('2026-08-23T12:30:00+00:00', Number.NaN)).toBeNull()
  })
})

describe('the offset of the moment that was chosen', () => {
  it('describes the chosen wall clock rather than today', () => {
    // The whole of this feature's daylight-saving correctness. Whatever
    // zone this test runs in, converting a value with the offset this
    // function reports for it must name the same instant the host's own
    // calendar does -- which is a January offset for a January value and a
    // July one for a July value, and an hour apart wherever the clocks
    // change.
    for (const value of ['2026-01-15T09:00', '2026-07-15T09:00']) {
      const iso = isoFromLocalInput(value, localOffsetMinutes(value))!
      const [date, clock] = value.split('T')
      const [year, month, day] = date!.split('-').map(Number)
      const [hour, minute] = clock!.split(':').map(Number)
      const asHostSeesIt = new Date(year!, month! - 1, day!, hour!, minute!)
      expect(new Date(iso).getTime()).toBe(asHostSeesIt.getTime())
    }
  })

  it('falls back to the reference moment for a value it cannot read', () => {
    const reference = new Date('2026-08-23T00:00:00Z')
    expect(localOffsetMinutes('nonsense', reference)).toBe(reference.getTimezoneOffset())
  })
})

describe('whether an instant may be sent at all', () => {
  const GRANTED = '2026-08-01T09:00:00+00:00'

  it('refuses an instant before the consent it withdraws', () => {
    // A withdrawal effective before the grant describes no interval at
    // all. The API answers 400; refusing it here earns a sentence
    // somebody wrote instead of one nobody did.
    const verdict = validateEffectiveAt('2026-07-31T09:00:00+00:00', GRANTED)
    expect(verdict.ok).toBe(false)
    if (!verdict.ok) {
      expect(verdict.problem.key).toBe('admin.consents.effective.beforeGrant')
      expect(verdict.problem.values!.granted).toBe('1 Aug 2026, 09:00 UTC')
    }
  })

  it('accepts the grant instant itself', () => {
    // §5.4 permits any instant *not before* `granted_at`, and a boundary
    // refused by one side and accepted by the other is a boundary that
    // produces a 400 nobody can explain.
    expect(validateEffectiveAt(GRANTED, GRANTED).ok).toBe(true)
  })

  it('accepts anything after it, past or future', () => {
    expect(validateEffectiveAt('2026-08-10T09:00:00+00:00', GRANTED).ok).toBe(true)
    expect(validateEffectiveAt('2027-01-01T09:00:00+00:00', GRANTED).ok).toBe(true)
  })

  it('refuses a value that is not a moment', () => {
    expect(validateEffectiveAt(null, GRANTED).ok).toBe(false)
    expect(validateEffectiveAt('half past four', GRANTED).ok).toBe(false)
    const verdict = validateEffectiveAt(null, GRANTED)
    if (!verdict.ok) expect(verdict.problem.key).toBe('admin.consents.effective.unreadable')
  })

  it('refuses nothing on the strength of a grant date that was never recorded', () => {
    // The API is still the authority. Guessing a bound out of a missing
    // field would block a legitimate withdrawal client-side.
    expect(validateEffectiveAt('2020-01-01T00:00:00+00:00', null).ok).toBe(true)
  })
})

describe('what the chosen instant means', () => {
  const NOW = new Date('2026-08-23T12:00:00Z')

  it('treats no choice as now', () => {
    // Pressing straight through the control without opening it must behave
    // exactly as it did before this change: no instant sent, the API
    // stamps its own.
    expect(effectiveKind(null, NOW)).toBe('now')
  })

  it('folds an instant a few seconds either side of now into now', () => {
    // Not a difference an administrator meant to express, and not one
    // worth a paragraph about recordings that already exist.
    expect(effectiveKind('2026-08-23T12:00:30Z', NOW)).toBe('now')
    expect(effectiveKind('2026-08-23T11:59:30Z', NOW)).toBe('now')
  })

  it('tells the past from the future', () => {
    expect(effectiveKind('2026-08-20T12:00:00Z', NOW)).toBe('past')
    expect(effectiveKind('2026-08-29T12:00:00Z', NOW)).toBe('future')
  })

  it('treats an unreadable instant as now rather than as a state of its own', () => {
    expect(effectiveKind('nonsense', NOW)).toBe('now')
  })
})

describe('what the confirmation gains from the instant', () => {
  it('adds nothing when the withdrawal takes effect now', () => {
    // No existing habit breaks: an administrator who never opens the
    // instant control sees exactly the confirmation they saw before.
    expect(effectiveConsequence('now', null, 12)).toEqual([])
  })

  it('says how many recordings are involved when the instant is in the past', () => {
    // Choosing a past instant and being shown nothing would let somebody
    // believe they had erased something.
    const lines = effectiveConsequence('past', '2026-08-01T09:00:00+00:00', 12)
    expect(lines[0]!.key).toBe('admin.consents.effective.pastMany')
    expect(lines[0]!.values).toEqual({ when: '1 Aug 2026, 09:00 UTC', count: '12' })
  })

  it('always follows that count with the fact that nothing is deleted', () => {
    expect(keysOf(effectiveConsequence('past', '2026-08-01T09:00:00+00:00', 12))).toContain(
      'admin.consents.effective.notDeletion',
    )
    expect(keysOf(effectiveConsequence('past', '2026-08-01T09:00:00+00:00', 0))).toContain(
      'admin.consents.effective.notDeletion',
    )
  })

  it('counts one recording in the singular and none at all in neither', () => {
    expect(effectiveConsequence('past', '2026-08-01T09:00:00+00:00', 1)[0]!.key).toBe(
      'admin.consents.effective.pastOne',
    )
    expect(effectiveConsequence('past', '2026-08-01T09:00:00+00:00', 0)[0]!.key).toBe(
      'admin.consents.effective.pastNone',
    )
  })

  it('says a future instant is a schedule and not a stop', () => {
    // A scheduled withdrawal that looks like an immediate one is an
    // administrator believing recording has stopped when it has not.
    const lines = effectiveConsequence('future', '2026-12-05T17:00:00+00:00', 12)
    expect(lines).toHaveLength(1)
    expect(lines[0]!.key).toBe('admin.consents.effective.futureNote')
    expect(lines[0]!.values!.when).toBe('5 Dec 2026, 17:00 UTC')
  })
})

describe('what the API says it did', () => {
  it('reports the instant it recorded and the count it computed', () => {
    // The console's arithmetic and the API's are two different
    // arithmetics, and only one of them has the recordings table.
    const lines = effectiveOutcome('2026-08-01T09:00:00+00:00', 12)
    expect(keysOf(lines)).toEqual([
      'admin.consents.effective.tookEffect',
      'admin.consents.effective.fromMany',
    ])
    expect(lines[1]!.values).toEqual({ when: '1 Aug 2026, 09:00 UTC', count: '12' })
  })

  it('counts one and none apart from many', () => {
    expect(effectiveOutcome('2026-08-01T09:00:00+00:00', 1)[1]!.key).toBe(
      'admin.consents.effective.fromOne',
    )
    expect(effectiveOutcome('2026-08-01T09:00:00+00:00', 0)[1]!.key).toBe(
      'admin.consents.effective.fromNone',
    )
  })

  it('says nothing at all when the API reported no instant', () => {
    // An API that predates the field. Inventing a moment to fill the gap
    // would be the console reporting its own request as an answer.
    expect(effectiveOutcome(null, 12)).toEqual([])
  })
})
