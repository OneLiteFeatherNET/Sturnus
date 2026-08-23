/**
 * What a person's own consent record says to them, and what the interface
 * refuses to imply about it.
 *
 * All of it lives in `~/utils/myConsents` rather than in `/settings`,
 * because every one of these is a decision -- which state produces which
 * sentence, whether the video option exists at all, what a refusal reads
 * like -- and a decision embedded in a template can only be tested by
 * rendering one.
 *
 * The four states are asserted individually and by name. `scheduled` is the
 * reason: a consent that runs out on Friday is indistinguishable from one
 * that runs for ever in every field except `revoked_at`, and a state model
 * that collapsed the two would leave somebody to find out by being
 * un-recorded on a day they expected to be recorded.
 */
import { describe, expect, it } from 'vitest'

import {
  AUDIO_ONLY_KEY,
  GRANT_IS_IN_DISCORD_KEY,
  NOT_DELETION_KEY,
  ROLE_STAYS_KEY,
  consentBadgeKey,
  consentNarrative,
  consentTone,
  describeMyConsentError,
  describeMyConsentErrorKey,
  describeRefusalKey,
  grantedUnderLine,
  isConsentServiceMissing,
  mayChangeScope,
  orderMyConsents,
  parseMyConsents,
  parseMyRevokeResult,
  parseScopeResult,
  recordingNowLine,
  scopeChoices,
  scopeLabelKey,
  scopeOutcome,
  stateLine,
  withdrawConfirmation,
  withdrawOutcome,
  withdrawability,
  type MyConsent,
} from '../app/utils/myConsents'

/** A consent with everything harmless, so each test states only the one
 *  property it is actually about. */
function consent(overrides: Partial<MyConsent> = {}): MyConsent {
  return {
    guild_id: '4711',
    state: 'active',
    active: true,
    scope: 'audio',
    policy_version: '3',
    guild_policy_version: '3',
    granted_at: '2026-08-01T09:00:00+00:00',
    revoked_at: null,
    video_consent_offered: false,
    ...overrides,
  }
}

/** What `ApiError` looks like to the functions that read a failure. */
function failure(status: number) {
  return { status, path: '/me/consents' }
}

/** Every key a run of lines names, in order. */
function keysOf(lines: readonly { key: string }[]): string[] {
  return lines.map((line) => line.key)
}

describe('reading the consents payload', () => {
  it('reads the rows out of the envelope the endpoint sends', () => {
    const parsed = parseMyConsents({
      consents: [
        {
          guild_id: '4711',
          state: 'active',
          active: true,
          scope: 'audio',
          policy_version: '3',
          guild_policy_version: '3',
          granted_at: '2026-08-01T09:00:00+00:00',
          revoked_at: null,
          video_consent_offered: false,
        },
      ],
    })
    expect(parsed).toHaveLength(1)
    expect(parsed[0]!.guild_id).toBe('4711')
    expect(parsed[0]!.state).toBe('active')
  })

  it('reads a bare list too', () => {
    // Accepting both removes a failure whose only symptom is an empty
    // section -- which on this page reads as "you have consented nowhere".
    expect(parseMyConsents([{ guild_id: '4711' }])).toHaveLength(1)
  })

  it('keeps a guild id as a string even if it arrived as a number', () => {
    expect(parseMyConsents([{ guild_id: 4711 }])[0]!.guild_id).toBe('4711')
  })

  it('drops an entry that names no server', () => {
    expect(parseMyConsents([{ state: 'active' }, { guild_id: '1' }])).toHaveLength(1)
  })

  it('yields nothing for a payload it cannot make sense of', () => {
    expect(parseMyConsents(null)).toEqual([])
    expect(parseMyConsents('nonsense')).toEqual([])
  })

  it('treats a missing active flag as not being recorded', () => {
    expect(parseMyConsents([{ guild_id: '1' }])[0]!.active).toBe(false)
    expect(parseMyConsents([{ guild_id: '1', active: 'true' }])[0]!.active).toBe(false)
  })

  it('treats anything that is not audio_video as audio', () => {
    expect(parseMyConsents([{ guild_id: '1' }])[0]!.scope).toBe('audio')
    expect(parseMyConsents([{ guild_id: '1', scope: 'video' }])[0]!.scope).toBe('audio')
    expect(parseMyConsents([{ guild_id: '1', scope: 'audio_video' }])[0]!.scope).toBe('audio_video')
  })

  it('treats a missing video offer as no video offer', () => {
    // The default is false in the guild configuration, and it has to be
    // false here too: an absent field that read as "offered" would put a
    // video option in front of somebody whose server has no policy for it.
    expect(parseMyConsents([{ guild_id: '1' }])[0]!.video_consent_offered).toBe(false)
    expect(
      parseMyConsents([{ guild_id: '1', video_consent_offered: 'yes' }])[0]!.video_consent_offered,
    ).toBe(false)
  })

  it('derives a state the API did not name, without needing a clock', () => {
    // A console meeting a fifth state from a newer API must still say
    // something true rather than render an unknown badge.
    const rows = parseMyConsents([
      { guild_id: '1', active: true },
      { guild_id: '2', active: true, revoked_at: '2026-12-01T00:00:00+00:00' },
      { guild_id: '3', active: false, revoked_at: '2026-01-01T00:00:00+00:00' },
      { guild_id: '4', active: false },
    ])
    expect(rows.map((row) => row.state)).toEqual([
      'active',
      'scheduled',
      'revoked',
      'policy_superseded',
    ])
  })

  it('keeps a state the API did name', () => {
    expect(parseMyConsents([{ guild_id: '1', state: 'policy_superseded' }])[0]!.state).toBe(
      'policy_superseded',
    )
  })
})

describe('the order the servers are listed in', () => {
  it('puts the servers where something is being recorded first', () => {
    const rows = orderMyConsents([
      consent({ guild_id: '10', active: false, state: 'revoked' }),
      consent({ guild_id: '20', active: true }),
    ])
    expect(rows.map((row) => row.guild_id)).toEqual(['20', '10'])
  })

  it('compares snowflakes numerically without ever making them numbers', () => {
    // As plain strings "1000…" would sort before "999…"; as numbers every
    // id past the safe integer range becomes a different id.
    const rows = orderMyConsents([
      consent({ guild_id: '1000000000000000000' }),
      consent({ guild_id: '999999999999999999' }),
    ])
    expect(rows.map((row) => row.guild_id)).toEqual([
      '999999999999999999',
      '1000000000000000000',
    ])
  })
})

describe('what the four states say', () => {
  it('gives each state its own badge', () => {
    const badges = (['active', 'scheduled', 'revoked', 'policy_superseded'] as const).map((state) =>
      consentBadgeKey(consent({ state })),
    )
    expect(new Set(badges).size).toBe(4)
  })

  it('gives each state its own colour', () => {
    // "Withdrawn" and "the policy version moved on" are different facts
    // about a person, and only one of them was their own decision.
    const tones = (['active', 'scheduled', 'revoked', 'policy_superseded'] as const).map((state) =>
      consentTone(consent({ state })),
    )
    expect(new Set(tones).size).toBe(4)
  })

  it('says a scheduled consent is still recording right now', () => {
    // The trap this whole state exists to avoid: reading "scheduled" and
    // inferring "so nothing is being recorded", which is the opposite of
    // the truth until the instant arrives.
    const scheduled = consent({
      state: 'scheduled',
      active: true,
      revoked_at: '2026-12-05T17:00:00+00:00',
    })
    expect(recordingNowLine(scheduled).key).toBe('settings.consent.now.audio')
  })

  it('names the instant a scheduled consent stops', () => {
    const scheduled = consent({
      state: 'scheduled',
      active: true,
      revoked_at: '2026-12-05T17:00:00+00:00',
    })
    const line = stateLine(scheduled)!
    expect(line.key).toBe('settings.consent.state.scheduled')
    // Formatted, never the raw instant: a person reading their own page
    // should not have to parse an ISO string to learn when they stop
    // being recorded.
    expect(line.values!.when).toBe('5 Dec 2026, 17:00 UTC')
  })

  it('says nothing is recorded once a consent is withdrawn', () => {
    const revoked = consent({ state: 'revoked', active: false, revoked_at: '2026-08-10T09:00:00+00:00' })
    expect(recordingNowLine(revoked).key).toBe('settings.consent.now.nothing')
    expect(stateLine(revoked)!.key).toBe('settings.consent.state.revoked')
  })

  it('gives a superseded consent a sentence of its own, naming both versions', () => {
    const lapsed = consent({
      state: 'policy_superseded',
      active: false,
      policy_version: '2',
      guild_policy_version: '3',
    })
    const line = stateLine(lapsed)!
    expect(line.key).toBe('settings.consent.state.superseded')
    expect(line.values).toEqual({ policy: '2', current: '3' })
  })

  it('does not invent a current policy version it was not told', () => {
    const lapsed = consent({
      state: 'policy_superseded',
      active: false,
      guild_policy_version: null,
    })
    expect(stateLine(lapsed)!.key).toBe('settings.consent.state.supersededUnknown')
  })

  it('adds nothing to an active consent that has no end', () => {
    expect(stateLine(consent({ state: 'active' }))).toBeNull()
  })

  it('names the scope in the sentence about what is recorded now', () => {
    expect(recordingNowLine(consent({ scope: 'audio' })).key).toBe('settings.consent.now.audio')
    expect(recordingNowLine(consent({ scope: 'audio_video' })).key).toBe(
      'settings.consent.now.audioVideo',
    )
  })

  it('says when it was granted and under which policy version', () => {
    const line = grantedUnderLine(consent())
    expect(line.key).toBe('settings.consent.granted.both')
    expect(line.values).toEqual({ granted: '1 Aug 2026, 09:00 UTC', policy: '3' })
  })

  it('says which of the two it does not know rather than printing a dash', () => {
    expect(grantedUnderLine(consent({ policy_version: null })).key).toBe(
      'settings.consent.granted.noPolicy',
    )
    expect(grantedUnderLine(consent({ granted_at: null })).key).toBe('settings.consent.granted.noDate')
    expect(grantedUnderLine(consent({ granted_at: null, policy_version: null })).key).toBe(
      'settings.consent.granted.neither',
    )
  })

  it('always says what is recorded now and under which policy, in that order', () => {
    // What is happening to somebody right now comes before the paperwork
    // that explains it.
    const lines = keysOf(consentNarrative(consent()))
    expect(lines[0]).toBe('settings.consent.now.audio')
    expect(lines[1]).toBe('settings.consent.granted.both')
  })
})

describe('the video option', () => {
  it('is absent when the server does not offer video consent', () => {
    // Not disabled, not greyed, not behind a tooltip. Absent. A consent
    // record naming video under a policy that describes only audio is not
    // consent, and an interface must not offer what it cannot honour.
    expect(scopeChoices(consent({ video_consent_offered: false }))).toEqual(['audio'])
  })

  it('is offered when the server does offer it', () => {
    expect(scopeChoices(consent({ video_consent_offered: true }))).toEqual([
      'audio',
      'audio_video',
    ])
  })

  it('is absent even for somebody whose record already names video', () => {
    // The record can outlive the offer: an administrator turning
    // `video_consent_offered` back off leaves rows behind it. Offering the
    // option because the row has it would be the interface trusting a
    // record over the policy that record has to be covered by.
    expect(scopeChoices(consent({ scope: 'audio_video', video_consent_offered: false }))).toEqual([
      'audio',
    ])
  })

  it('has one sentence standing in for the option it does not show', () => {
    // So that the absence reads as a fact about the server rather than as
    // a control that failed to render.
    expect(AUDIO_ONLY_KEY).toBe('settings.consent.scope.audioOnly')
  })

  it('labels the two scopes differently', () => {
    expect(scopeLabelKey('audio')).not.toBe(scopeLabelKey('audio_video'))
  })

  it('offers no scope change on a withdrawn consent', () => {
    // The endpoint answers `already_revoked` every time, and offering a
    // control whose outcome is already known is what this console does not
    // do.
    expect(mayChangeScope(consent({ state: 'revoked' }))).toBe(false)
  })

  it('still offers one on a lapsed consent', () => {
    // Widening there writes a new record under the current policy version,
    // which is the way back for somebody whose consent lapsed.
    expect(mayChangeScope(consent({ state: 'policy_superseded', active: false }))).toBe(true)
    expect(mayChangeScope(consent({ state: 'scheduled' }))).toBe(true)
  })
})

describe('the three things that must be said before a withdrawal', () => {
  it('says the Discord role stays', () => {
    // The API holds no Discord token by design. Somebody who believes this
    // removed their role will not go and remove it.
    expect(keysOf(withdrawConfirmation(consent()).consequences)).toContain(ROLE_STAYS_KEY)
  })

  it('says withdrawing is not deletion', () => {
    expect(keysOf(withdrawConfirmation(consent()).consequences)).toContain(NOT_DELETION_KEY)
  })

  it('says only the person can grant it again, and not from here', () => {
    expect(keysOf(withdrawConfirmation(consent()).consequences)).toContain(GRANT_IS_IN_DISCORD_KEY)
  })

  it('tells somebody with a scheduled stop what this does to that date, first', () => {
    const confirmation = withdrawConfirmation(
      consent({ state: 'scheduled', revoked_at: '2026-12-05T17:00:00+00:00' }),
    )
    expect(confirmation.consequences[0]!.key).toBe('settings.consent.withdraw.alreadyScheduled')
    expect(confirmation.consequences[0]!.values!.when).toBe('5 Dec 2026, 17:00 UTC')
  })

  it('offers the withdrawal in every state but the one where it would fail', () => {
    expect(withdrawability(consent({ state: 'active' })).may).toBe(true)
    expect(withdrawability(consent({ state: 'scheduled' })).may).toBe(true)
    expect(withdrawability(consent({ state: 'policy_superseded' })).may).toBe(true)
    expect(withdrawability(consent({ state: 'revoked' })).may).toBe(false)
  })

  it('says when it was withdrawn instead of offering the button', () => {
    const verdict = withdrawability(
      consent({ state: 'revoked', revoked_at: '2026-08-10T09:00:00+00:00' }),
    )
    expect(verdict.may).toBe(false)
    if (!verdict.may) expect(verdict.reason.values!.when).toBe('10 Aug 2026, 09:00 UTC')
  })
})

describe('what the scope endpoint answered', () => {
  it('reads a change and the policy version it was written under', () => {
    expect(
      parseScopeResult({ scope: 'audio_video', changed: true, refusal: null, policy_version: '3' }),
    ).toEqual({ scope: 'audio_video', changed: true, refusal: null, policy_version: '3' })
  })

  it('never reports a body it cannot make sense of as a change', () => {
    // The only person who finds out otherwise is the one who thinks they
    // have said no to video.
    expect(parseScopeResult(null).changed).toBe(false)
    expect(parseScopeResult({ changed: 'true' }).changed).toBe(false)
  })

  it('reports a scope that was already what was asked for as no change, not as a failure', () => {
    const outcome = scopeOutcome({
      scope: 'audio',
      changed: false,
      refusal: null,
      policy_version: '3',
    })
    expect(outcome.tone).toBe('done')
    expect(outcome.headline.key).toBe('settings.consent.scope.unchangedHeadline')
  })

  it('tells narrowing and widening apart', () => {
    const narrowed = scopeOutcome({ scope: 'audio', changed: true, refusal: null, policy_version: '3' })
    const widened = scopeOutcome({
      scope: 'audio_video',
      changed: true,
      refusal: null,
      policy_version: '4',
    })
    expect(narrowed.headline.key).toBe('settings.consent.scope.narrowedHeadline')
    expect(widened.headline.key).toBe('settings.consent.scope.widenedHeadline')
    expect(widened.detail[0]!.values!.policy).toBe('4')
  })

  it('says video is not recorded yet when somebody has just consented to it', () => {
    // Sturnus records no video today. Consenting records that it would be
    // allowed to; it starts nothing, and letting somebody believe it did
    // would be the console promising a capability that does not exist.
    const widened = scopeOutcome({
      scope: 'audio_video',
      changed: true,
      refusal: null,
      policy_version: '4',
    })
    expect(keysOf(widened.detail)).toContain('settings.consent.scope.videoNotRecordedYet')
  })

  it('reports a refusal as a refusal', () => {
    const outcome = scopeOutcome({
      scope: 'audio',
      changed: false,
      refusal: 'video_consent_not_offered',
      policy_version: null,
    })
    expect(outcome.tone).toBe('refused')
    expect(outcome.detail[0]!.key).toBe('settings.consent.refusal.videoNotOffered')
  })
})

describe('a refusal, as a sentence', () => {
  it('gives each of the four named refusals its own words', () => {
    const keys = [
      'video_consent_not_offered',
      'no_consent_on_record',
      'already_revoked',
      'no_policy_version',
    ].map(describeRefusalKey)
    expect(new Set(keys).size).toBe(4)
  })

  it('has a sentence true of all four for the case where the code was stripped', () => {
    // `useApi` strips the body off every failed request on purpose, so a
    // 409 arrives as a status with no refusal code attached.
    expect(describeRefusalKey(null)).toBe('settings.consent.refusal.unknown')
    expect(describeRefusalKey('something_new')).toBe('settings.consent.refusal.unknown')
  })
})

describe('what the withdrawal endpoint answered', () => {
  it('reads the instant it took effect and what still exists from then on', () => {
    expect(
      parseMyRevokeResult({
        revoked: true,
        refusal: null,
        effective_at: '2026-08-23T10:00:00+00:00',
        recordings_from_effective_at: 12,
        role_stays: true,
      }),
    ).toEqual({
      revoked: true,
      refusal: null,
      effective_at: '2026-08-23T10:00:00+00:00',
      recordings_from_effective_at: 12,
      role_stays: true,
    })
  })

  it('never reports a body it cannot make sense of as a withdrawal', () => {
    expect(parseMyRevokeResult(null).revoked).toBe(false)
    expect(parseMyRevokeResult({ revoked: 'true' }).revoked).toBe(false)
  })

  it('assumes the role stays when the API did not say', () => {
    // The conservative answer, and the one that is true today. A console
    // that quietly stopped saying "your role stays" would let somebody
    // believe the opposite.
    expect(parseMyRevokeResult({ revoked: true }).role_stays).toBe(true)
    expect(parseMyRevokeResult({ revoked: true, role_stays: false }).role_stays).toBe(false)
  })

  it('never reports a negative count of recordings', () => {
    expect(parseMyRevokeResult({ recordings_from_effective_at: -3 }).recordings_from_effective_at)
      .toBe(0)
  })

  it('repeats both limits after the act, not only before it', () => {
    // The moment somebody is most likely to believe more happened than did
    // is the moment they have just watched their own row change state.
    const outcome = withdrawOutcome({
      revoked: true,
      refusal: null,
      effective_at: '2026-08-23T10:00:00+00:00',
      recordings_from_effective_at: 4,
      role_stays: true,
    })
    expect(outcome.tone).toBe('done')
    expect(keysOf(outcome.detail)).toContain(ROLE_STAYS_KEY)
    expect(keysOf(outcome.detail)).toContain(GRANT_IS_IN_DISCORD_KEY)
  })

  it('says with a figure how many recordings still hold their audio', () => {
    const some = withdrawOutcome({
      revoked: true,
      refusal: null,
      effective_at: '2026-08-23T10:00:00+00:00',
      recordings_from_effective_at: 4,
      role_stays: true,
    })
    expect(keysOf(some.detail)).toContain('settings.consent.withdraw.heldMany')
    expect(some.detail[1]!.values!.count).toBe('4')

    const one = withdrawOutcome({
      revoked: true,
      refusal: null,
      effective_at: '2026-08-23T10:00:00+00:00',
      recordings_from_effective_at: 1,
      role_stays: true,
    })
    expect(keysOf(one.detail)).toContain('settings.consent.withdraw.heldOne')

    const none = withdrawOutcome({
      revoked: true,
      refusal: null,
      effective_at: '2026-08-23T10:00:00+00:00',
      recordings_from_effective_at: 0,
      role_stays: true,
    })
    expect(keysOf(none.detail)).toContain('settings.consent.withdraw.heldNone')
  })

  it('says nothing about an instant the API did not report one for', () => {
    const outcome = withdrawOutcome({
      revoked: true,
      refusal: null,
      effective_at: null,
      recordings_from_effective_at: 0,
      role_stays: true,
    })
    expect(outcome.detail[0]!.key).toBe('settings.consent.withdraw.doneDetailNoInstant')
  })

  it('reports a refusal as a refusal and never as a withdrawal', () => {
    const outcome = withdrawOutcome({
      revoked: false,
      refusal: 'already_revoked',
      effective_at: null,
      recordings_from_effective_at: 0,
      role_stays: true,
    })
    expect(outcome.tone).toBe('refused')
    expect(outcome.detail[0]!.key).toBe('settings.consent.refusal.alreadyRevoked')
  })
})

describe('when the consent endpoint is not there yet', () => {
  it('recognises the 404 that means the API predates this page', () => {
    // The console and the API ship as two images and can be deployed
    // apart. This is the failure whose handling is not a nicety: a 404
    // rendered as an empty list would tell somebody they have consented
    // nowhere, which is a false statement about their own data.
    expect(isConsentServiceMissing(failure(404))).toBe(true)
    expect(isConsentServiceMissing(failure(500))).toBe(false)
    expect(isConsentServiceMissing(failure(0))).toBe(false)
    expect(isConsentServiceMissing(null)).toBe(false)
  })

  it('has a sentence of its own for it, not the generic one', () => {
    expect(describeMyConsentErrorKey(failure(404))).toBe('settings.consent.error.notDeployed')
    expect(describeMyConsentErrorKey(failure(500))).toBe('settings.consent.error.unknown')
    expect(describeMyConsentErrorKey(failure(401))).toBe('settings.consent.error.signedOut')
    expect(describeMyConsentErrorKey(failure(0))).toBe('settings.consent.error.unreachable')
  })

  it('reports the status only where the status is the only thing known', () => {
    expect(describeMyConsentError(failure(503)).values).toEqual({ status: 503 })
    expect(describeMyConsentError(failure(404)).values).toBeUndefined()
  })
})
