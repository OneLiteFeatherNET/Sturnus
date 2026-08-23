/**
 * The three things about setup intents that an interface has to get right.
 *
 * Every assertion here is one of the three properties `docs/operations.md`
 * §6.2.14 spells out, or a consequence of one:
 *
 * - a failure is terminal, so the page has to offer another ask rather than
 *   a wait;
 * - the newest ask wins, so a superseded request is not a failure and a
 *   request that is not the one this browser submitted has to say so;
 * - `pending` past a tick means the bot is not there, so an empty channel
 *   list is two opposite instructions depending on `bot.has_arrived`.
 *
 * None of them can be seen in a rendered page: every one of them looks
 * identical on screen to the state it must not be confused with, which is
 * exactly why they are decided in a module rather than in a template.
 */
import { describe, expect, it } from 'vitest'

import { ApiError } from '../app/utils/apiError'
import {
  APPLIED,
  FAILED,
  MAX_ROLE_NAME,
  PENDING,
  POLL_LIMIT,
  SUPERSEDED,
  describeSetupError,
  draftProblem,
  isSettled,
  parseInvite,
  parseSetupState,
  pickerState,
  reportRequest,
  requestBody,
  requestTone,
  requesterLabel,
  resubmitNote,
  shouldPoll,
  setupPath,
  storedChannels,
  submittedChannels,
} from '../app/utils/onboarding'

/** What `GET /api/guilds/{id}/setup` answers for a guild nobody has asked
 *  about, on a server the bot has swept. */
const ARRIVED = {
  guild_id: '1',
  bot: { has_arrived: true, seen_at: '2026-08-23T10:00:00+00:00' },
  request: null,
}

function withRequest(request: Record<string, unknown>) {
  return { ...ARRIVED, request: { id: '7', requested_by: '99', ...request } }
}

const CHANNELS = [
  { id: '10', name: 'Standup' },
  { id: '11', name: 'Retro' },
]

describe('reading the payload', () => {
  it('keeps every snowflake a string', () => {
    // The one defect that cannot be seen: `9134756382910273645` parsed as a
    // number is `9134756382910274000`, which looks like an id and names
    // nothing. Numbers are accepted from the wire and stringified rather
    // than refused, because refusing would blank a page over a field an
    // older API sent the other way.
    const state = parseSetupState({
      guild_id: '9134756382910273645',
      bot: { has_arrived: true, seen_at: null },
      request: { id: 7, requested_by: '99', channel_ids: ['10', '11'] },
    })
    expect(state.guildId).toBe('9134756382910273645')
    expect(state.request?.id).toBe('7')
    expect(state.request?.channelIds).toEqual(['10', '11'])
  })

  it('treats a payload that does not mention the bot as one that has not arrived', () => {
    // Silence reads as "not known to have arrived", which makes the page
    // wait. The other reading would have it announce that a server nobody
    // has looked at has no voice channels.
    expect(parseSetupState({}).botHasArrived).toBe(false)
    expect(parseSetupState(null).botHasArrived).toBe(false)
  })

  it('reads a request with no status as still pending', () => {
    expect(parseSetupState(withRequest({})).request?.status).toBe(PENDING)
  })

  it('drops a request with no id, which is not a request', () => {
    expect(parseSetupState({ ...ARRIVED, request: {} }).request).toBeNull()
  })

  it('reads an invite that this deployment cannot build', () => {
    // `url: null` is a deployment without `STURNUS_DISCORD_CLIENT_ID`, and
    // the permissions still arrive: they are what somebody ticks in
    // Discord's own URL generator instead.
    const invite = parseInvite({
      client_id: null,
      url: null,
      permissions: '269487104',
      scopes: ['bot', 'applications.commands'],
    })
    expect(invite.url).toBeNull()
    expect(invite.permissions).toBe('269487104')
    expect(invite.scopes).toEqual(['bot', 'applications.commands'])
  })
})

describe('what an empty channel list is', () => {
  it('is "nobody has looked yet" while nothing has been mirrored', () => {
    expect(pickerState({ botHasArrived: false, directoryFailed: false, channelCount: 0 })).toBe(
      'waiting',
    )
  })

  it('is "this server has no voice channels" once something has', () => {
    // The whole distinction. These two calls differ in one boolean and the
    // instructions they produce are opposites: wait ten seconds, or go and
    // create a channel in Discord.
    expect(pickerState({ botHasArrived: true, directoryFailed: false, channelCount: 0 })).toBe(
      'empty',
    )
  })

  it('is still "nobody has looked yet" when the directory call also failed', () => {
    // A directory that answered nothing for a guild nothing has been
    // mirrored for answered correctly. Reporting it as broken would send
    // somebody to check an API that is working.
    expect(pickerState({ botHasArrived: false, directoryFailed: true, channelCount: 0 })).toBe(
      'waiting',
    )
  })

  it('says so when the names could not be read at all', () => {
    expect(pickerState({ botHasArrived: true, directoryFailed: true, channelCount: 0 })).toBe(
      'unreadable',
    )
  })

  it('offers the picker when there is something to pick', () => {
    expect(pickerState({ botHasArrived: true, directoryFailed: false, channelCount: 2 })).toBe(
      'ready',
    )
  })
})

describe('the channels a guild already records', () => {
  it('separates the ones the mirror can name from the ones it cannot', () => {
    expect(storedChannels('10,404', CHANNELS)).toEqual({ recorded: ['10'], stale: ['404'] })
  })

  it('never carries a stale stored id into a request', () => {
    // The failure this prevents is total rather than partial: the applier
    // refuses a channel a request names that it cannot see, a refusal is a
    // problem, and one problem settles the whole intent as `failed`. A
    // guild with one deleted channel in `voice_channel_ids` would have
    // every setup request it ever made fail over a room nobody asked about.
    const stored = storedChannels('10,404', CHANNELS)
    expect(submittedChannels(stored, ['11'])).toEqual(['10', '11'])
  })

  it('holds an empty list for a guild that records nothing yet', () => {
    expect(storedChannels(null, CHANNELS)).toEqual({ recorded: [], stale: [] })
  })

  it('does not name a channel twice when an already-recorded one is ticked', () => {
    const stored = storedChannels('10', CHANNELS)
    expect(submittedChannels(stored, ['10', '11'])).toEqual(['10', '11'])
  })
})

describe('what the form will not send', () => {
  it('refuses a request that names no channel, which the API refuses too', () => {
    expect(draftProblem({ channelIds: [], consentRoleName: '' })?.key).toBe(
      'admin.onboarding.needChannel',
    )
  })

  it('refuses a role name Discord would refuse, while somebody is still typing', () => {
    const problem = draftProblem({
      channelIds: ['10'],
      consentRoleName: 'x'.repeat(MAX_ROLE_NAME + 1),
    })
    expect(problem?.key).toBe('admin.onboarding.roleTooLong')
  })

  it('accepts a name of exactly the limit', () => {
    expect(
      draftProblem({ channelIds: ['10'], consentRoleName: 'x'.repeat(MAX_ROLE_NAME) }),
    ).toBeNull()
  })

  it('accepts a blank name, which is how a guild keeps the role it has', () => {
    expect(draftProblem({ channelIds: ['10'], consentRoleName: '   ' })).toBeNull()
  })

  it('sends a blank name as null rather than as an empty string', () => {
    // Absent and null both mean "do not name one". An empty string is
    // refused by the API outright, and omitting something must never be
    // the destructive path.
    expect(requestBody({ channelIds: ['10'], consentRoleName: '  ' })).toEqual({
      channel_ids: ['10'],
      consent_role_name: null,
    })
  })

  it('trims a name somebody pasted with a space on the end', () => {
    expect(requestBody({ channelIds: ['10'], consentRoleName: ' Consent ' })).toEqual({
      channel_ids: ['10'],
      consent_role_name: 'Consent',
    })
  })
})

describe('what a status means', () => {
  it('reads a pending row on a server the bot has swept as waiting', () => {
    expect(requestTone(PENDING, true)).toBe('waiting')
  })

  it('reads a pending row on a server the bot is not in as stalled', () => {
    // `pending` past a tick is not slowness. The guild has no gateway
    // object to iterate, so the row will never be attempted at all.
    expect(requestTone(PENDING, false)).toBe('stalled')
  })

  it('never reads a superseded row as a failure', () => {
    // The single most consequential line in this file. Nothing went wrong
    // to a superseded request: it was replaced before the bot reached it
    // and never acted on, and drawing it in the failure colour would send
    // somebody to check a permission that was never tested.
    expect(requestTone(SUPERSEDED, true)).toBe('neutral')
    expect(requestTone(FAILED, true)).toBe('bad')
    expect(requestTone(APPLIED, true)).toBe('good')
  })

  it('renders an outcome written by a newer bot rather than guessing at it', () => {
    // `outcome` is text and not a database enum precisely so that a word
    // this build has never seen is a row a reader can ignore. A console
    // that narrowed it back to four would hand that property back.
    expect(requestTone('quarantined', true)).toBe('unknown')
    expect(isSettled('quarantined')).toBe(true)
    expect(isSettled(PENDING)).toBe(false)
  })
})

describe('what the panel says', () => {
  const context = { viewer: '99', submitted: null }

  it('says nothing at all when nobody has ever asked', () => {
    expect(reportRequest(parseSetupState(ARRIVED), context)).toBeNull()
  })

  it('carries what the bot itself wrote on a failure and on nothing else', () => {
    const failed = reportRequest(
      parseSetupState(withRequest({ status: FAILED, error: 'I am missing Manage Roles' })),
      context,
    )
    expect(failed?.error).toBe('I am missing Manage Roles')

    // An `error` beside any other outcome is a row written by hand, and
    // rendering it would make an applied setup look like a broken one.
    const applied = reportRequest(
      parseSetupState(withRequest({ status: APPLIED, error: 'left over' })),
      context,
    )
    expect(applied?.error).toBeNull()
  })

  it('says a failure is the end of that request, not a wait', () => {
    const report = reportRequest(parseSetupState(withRequest({ status: FAILED })), context)
    expect(report?.notes.map((note) => note.key)).toEqual([
      'admin.onboarding.failedTerminal',
      'admin.onboarding.roleOrder',
    ])
  })

  it('says a superseded request was replaced, not that it went wrong', () => {
    const report = reportRequest(parseSetupState(withRequest({ status: SUPERSEDED })), context)
    expect(report?.tone).toBe('neutral')
    expect(report?.notes.map((note) => note.key)).toEqual(['admin.onboarding.supersededNote'])
    expect(report?.badge.key).toBe('admin.onboarding.status.neutral')
  })

  it('says the bot is not there when a pending row is sitting in an empty mirror', () => {
    const report = reportRequest(
      parseSetupState({ ...withRequest({}), bot: { has_arrived: false, seen_at: null } }),
      context,
    )
    expect(report?.tone).toBe('stalled')
    expect(report?.notes.map((note) => note.key)).toEqual(['admin.onboarding.stalledNote'])
  })

  it('names the outcome it has never heard of rather than hiding it', () => {
    const report = reportRequest(parseSetupState(withRequest({ status: 'quarantined' })), context)
    expect(report?.badge).toEqual({
      key: 'admin.onboarding.status.unknown',
      params: { status: 'quarantined' },
    })
  })

  it('says when the request on screen is not the one this browser submitted', () => {
    // The supersede rule as it is actually experienced. A colleague pressed
    // the button thirty seconds later; `GET` answers with theirs, because
    // theirs is what the guild will be configured from. Everything below
    // then describes a request this reader did not make, and reading it as
    // their own is how somebody concludes their channel list was applied.
    const report = reportRequest(parseSetupState(withRequest({ id: '8', status: APPLIED })), {
      viewer: '99',
      submitted: '7',
    })
    expect(report?.notes[0]?.key).toBe('admin.onboarding.replacedYours')
    // The outcome is still the guild's honest answer and stays as it is.
    expect(report?.tone).toBe('good')
  })

  it('says nothing about replacement when the request is the one submitted', () => {
    const report = reportRequest(parseSetupState(withRequest({ status: APPLIED })), {
      viewer: '99',
      submitted: '7',
    })
    expect(report?.notes[0]?.key).toBe('admin.onboarding.appliedNote')
  })
})

describe('who asked', () => {
  const members = [{ id: '99', name: 'Anna' }]

  it('says "you" where it was this reader', () => {
    const request = parseSetupState(withRequest({})).request!
    expect(requesterLabel(request, '99', members).key).toBe('admin.onboarding.askedByYou')
  })

  it('names the other administrator where the mirror knows them', () => {
    const request = parseSetupState(withRequest({})).request!
    expect(requesterLabel(request, '1', members)).toEqual({
      key: 'admin.onboarding.askedBy',
      params: { who: 'Anna' },
    })
  })

  it('falls back to the bare id, which is what this console does everywhere', () => {
    // `guild_member` holds the consent role's and the admin role's members
    // and nobody else, so an administrator outside both is a legitimate
    // miss rather than a fault.
    const request = parseSetupState(withRequest({ requested_by: '404' })).request!
    expect(requesterLabel(request, '1', members)).toEqual({
      key: 'admin.onboarding.askedByUnresolved',
      params: { id: '404' },
    })
  })
})

describe('what pressing the button again does', () => {
  it('warns that a second ask replaces a pending one rather than queueing', () => {
    expect(resubmitNote(parseSetupState(withRequest({})))?.key).toBe(
      'admin.onboarding.resubmitReplaces',
    )
  })

  it('invites another ask after a failure, which is the only way forward', () => {
    expect(resubmitNote(parseSetupState(withRequest({ status: FAILED })))?.key).toBe(
      'admin.onboarding.resubmitAfterFailure',
    )
  })

  it('says nothing before anybody has asked at all', () => {
    expect(resubmitNote(parseSetupState(ARRIVED))).toBeNull()
  })
})

describe('how long the page keeps watching', () => {
  it('watches a pending request', () => {
    expect(shouldPoll(parseSetupState(withRequest({})), 0)).toBe(true)
  })

  it('watches a guild the bot has not reached even with nothing asked', () => {
    // This is what makes the page come alive on its own the moment the bot
    // joins, rather than leaving somebody to guess when to press Refresh.
    const state = parseSetupState({ ...ARRIVED, bot: { has_arrived: false, seen_at: null } })
    expect(shouldPoll(state, 0)).toBe(true)
  })

  it('stops once the request has settled', () => {
    expect(shouldPoll(parseSetupState(withRequest({ status: APPLIED })), 0)).toBe(false)
    expect(shouldPoll(parseSetupState(withRequest({ status: SUPERSEDED })), 0)).toBe(false)
  })

  it('gives up rather than polling a forgotten tab for the life of the browser', () => {
    // The wait this bounds is a human one -- somebody has to open Discord
    // and add the bot -- and not the ten-second tick.
    const state = parseSetupState({ ...ARRIVED, bot: { has_arrived: false, seen_at: null } })
    expect(shouldPoll(state, POLL_LIMIT)).toBe(false)
  })

  it('has nothing to watch before the first answer arrives', () => {
    expect(shouldPoll(null, 0)).toBe(false)
  })
})

describe('where the requests go', () => {
  it('addresses a guild by the string its id is', () => {
    expect(setupPath('9134756382910273645')).toBe('/guilds/9134756382910273645/setup')
  })
})

describe('why a call did not work', () => {
  it('tells "the API said no" from "the API could not be reached"', () => {
    expect(describeSetupError(new ApiError('/setup', { status: 0 })).key).toBe(
      'admin.onboarding.errorUnreachable',
    )
    expect(describeSetupError(new ApiError('/setup', { status: 400 })).key).toBe(
      'admin.onboarding.errorRefused',
    )
    expect(describeSetupError(new ApiError('/setup', { status: 404 })).key).toBe(
      'admin.onboarding.errorGone',
    )
    expect(describeSetupError(new ApiError('/setup', { status: 401 })).key).toBe(
      'admin.onboarding.errorSession',
    )
  })

  it('names a status it has no sentence for', () => {
    expect(describeSetupError(new ApiError('/setup', { status: 503 }))).toEqual({
      key: 'admin.onboarding.errorStatus',
      params: { status: '503' },
    })
  })

  it('treats something that is not an API error at all as unreachable', () => {
    expect(describeSetupError(new Error('boom')).key).toBe('admin.onboarding.errorUnreachable')
  })
})
