/**
 * Whether a name can stand in for a snowflake without losing the
 * snowflake.
 *
 * Every decision the pickers make is here rather than in the settings
 * page, for the reason this codebase always gives: a decision embedded in
 * a template can only be tested by rendering one. The two that matter most
 * are asserted in several shapes each.
 *
 * The first is what happens to an id the mirror has never heard of. A
 * channel deleted in Discord, a role from before the last sweep, a
 * collection somebody archived: the console must show the bare id and say
 * it could not resolve it. Dropping the option would make a misconfigured
 * guild look configured, and rendering a blank would make it look empty --
 * both of them hide the one fact the administrator has to act on.
 *
 * The second is the comma-separated round-trip behind `voice_channel_ids`.
 * A parser that reorders, or a serialiser that rewrites a value nobody
 * touched, would leave the Save button lying in one direction or the
 * other: either offering to write a change that is not one, or refusing a
 * change that is.
 */
import { describe, expect, it } from 'vitest'

import {
  addToIdList,
  blankOption,
  channelChoices,
  channelKindHeading,
  controlKind,
  idListHas,
  mirrorFreshness,
  parseCollections,
  parseDirectory,
  parseIdList,
  removeFromIdList,
  resolveChoice,
  serialiseIdList,
  singleChoices,
  type DirectoryChannel,
} from '../app/utils/directory'
import type { SettingView } from '../app/utils/settings'

function view(key: string, overrides: Partial<SettingView> = {}): SettingView {
  return {
    key,
    value: null,
    default: null,
    required: false,
    integer: false,
    invalidates_consent: false,
    takes_effect: 'immediately',
    deferred_while_recording: false,
    ...overrides,
  }
}

function channel(overrides: Partial<DirectoryChannel> & { id: string }): DirectoryChannel {
  return { name: `Channel ${overrides.id}`, kind: 'voice', position: 0, ...overrides }
}

const PAYLOAD = {
  guild_id: '4711',
  synced_at: '2026-08-23T12:00:00+00:00',
  channels: [
    { id: '10', name: 'Standup', kind: 'voice', position: 3 },
    { id: '11', name: 'Announcements', kind: 'text', position: 1 },
  ],
  roles: [{ id: '77', name: 'Recorded', position: 7 }],
  members: [{ discord_user_id: '100', display_name: 'Anna Example' }],
}

describe('which control a key gets', () => {
  it('gives the recording channels a picker over channels', () => {
    expect(controlKind(view('voice_channel_ids'))).toBe('channels')
  })

  it.each(['consent_role_id', 'admin_role_id'])('gives %s a picker over roles', (key) => {
    expect(controlKind(view(key))).toBe('role')
  })

  it('gives the protocol target a picker over Outline collections', () => {
    expect(controlKind(view('document_target'))).toBe('collection')
  })

  it('leaves a key it has never heard of on the control it already had', () => {
    // The settings page renders whatever the registry returns, and the
    // registry gains keys without this module hearing about it. A key that
    // fell through to nothing at all would be a setting nobody can edit --
    // an interface that quietly forgets a feature is worse than one that
    // asks for an id.
    expect(controlKind(view('max_session_hours'))).toBe('plain')
    expect(controlKind(view('transcription_prompt'))).toBe('plain')
    expect(controlKind(view('a_key_invented_next_year'))).toBe('plain')
  })

  it('does not mistake a key that merely looks like one of the four', () => {
    expect(controlKind(view('admin_role_id_backup'))).toBe('plain')
    expect(controlKind(view('legacy_voice_channel_ids'))).toBe('plain')
  })
})

describe('the comma-separated list of channel ids', () => {
  it('reads a single id as a list of one', () => {
    // `voice_channel_ids` held exactly one id before the bot learned to
    // accept several, and every guild configured before that still stores
    // one. A parser that needed a comma would show those guilds an empty
    // picker over a value that is set.
    expect(parseIdList('10')).toEqual(['10'])
  })

  it('reads an empty value as no channels rather than one nameless one', () => {
    expect(parseIdList('')).toEqual([])
    expect(parseIdList('   ')).toEqual([])
    expect(parseIdList(null)).toEqual([])
    expect(parseIdList(undefined)).toEqual([])
  })

  it('ignores the whitespace somebody typed around the commas', () => {
    expect(parseIdList(' 10 , 11,12 ')).toEqual(['10', '11', '12'])
  })

  it('survives the trailing comma a half-finished edit leaves behind', () => {
    expect(parseIdList('10,,11,')).toEqual(['10', '11'])
  })

  it('keeps the order it was stored in', () => {
    // Sorting here would rewrite a value nobody edited, and the settings
    // page decides whether to enable Save by comparing the draft string
    // with the stored one.
    expect(parseIdList('12,10,11')).toEqual(['12', '10', '11'])
  })

  it('keeps one copy of an id that was stored twice', () => {
    expect(parseIdList('10,11,10')).toEqual(['10', '11'])
  })

  it('serialises to commas with no spaces, which is what the API stores', () => {
    expect(serialiseIdList(['10', '11'])).toBe('10,11')
    expect(serialiseIdList(['10'])).toBe('10')
    expect(serialiseIdList([])).toBe('')
  })

  it('round-trips a canonical value unchanged', () => {
    // The property that keeps Save honest: reading a stored value and
    // writing it back must produce the same bytes, or every visit to this
    // page would look like an unsaved edit.
    for (const stored of ['10', '10,11', '10,11,12', '']) {
      expect(serialiseIdList(parseIdList(stored))).toBe(stored)
    }
  })

  it('adds a channel at the end rather than in sorted position', () => {
    expect(addToIdList('12,10', '11')).toBe('12,10,11')
  })

  it('does not add a channel that is already chosen', () => {
    expect(addToIdList('10,11', '10')).toBe('10,11')
  })

  it('adds the first channel to an empty value without a leading comma', () => {
    expect(addToIdList('', '10')).toBe('10')
  })

  it('removes a channel and leaves the rest in their order', () => {
    expect(removeFromIdList('12,10,11', '10')).toBe('12,11')
  })

  it('removes the last channel down to an empty value, not to a comma', () => {
    expect(removeFromIdList('10', '10')).toBe('')
  })

  it('leaves the value alone when asked to remove something that is not in it', () => {
    expect(removeFromIdList('10,11', '99')).toBe('10,11')
  })

  it('tidies the whitespace only when something actually changed', () => {
    // `10, 11` and `10,11` configure the same two channels, so opening the
    // page must not offer to save the difference. Once a channel is added
    // or removed the value is rewritten canonically, which is a real edit
    // carrying a cosmetic one along with it.
    expect(parseIdList('10, 11')).toEqual(['10', '11'])
    expect(addToIdList('10, 11', '12')).toBe('10,11,12')
    expect(removeFromIdList('10, 11', '11')).toBe('10')
  })

  it('answers whether an id is in the list without minding the spacing', () => {
    expect(idListHas('10, 11', '11')).toBe(true)
    expect(idListHas('10,11', '1')).toBe(false)
    expect(idListHas('', '10')).toBe(false)
  })
})

describe('putting a name to an id', () => {
  const roles = [
    { id: '77', name: 'Recorded' },
    { id: '78', name: 'Moderators' },
  ]

  it('shows the name when the mirror has the row', () => {
    const choice = resolveChoice(roles, '77')
    expect(choice.name).toBe('Recorded')
    expect(choice.label).toBe('Recorded')
    expect(choice.resolved).toBe(true)
  })

  it('shows the bare id when the mirror has nothing for it', () => {
    // The single most important behaviour on the page. A role deleted in
    // Discord is a configuration problem somebody has to fix, and an
    // interface that renders it as a blank -- or drops it from the list --
    // hides the one thing worth knowing about this guild.
    const choice = resolveChoice(roles, '1289374650912837465')
    expect(choice.name).toBeNull()
    expect(choice.label).toBe('1289374650912837465')
    expect(choice.resolved).toBe(false)
  })

  it('keeps the whole id rather than shortening it', () => {
    // Snowflakes minted around the same moment share their leading digits,
    // so a truncated one identifies nothing and cannot be searched for.
    const choice = resolveChoice(roles, '1289374650912837465')
    expect(choice.id).toBe('1289374650912837465')
  })
})

describe('the list of channels to choose from', () => {
  it('groups the channels by kind, voice first', () => {
    const { groups } = channelChoices(
      [
        channel({ id: '11', name: 'Announcements', kind: 'text' }),
        channel({ id: '10', name: 'Standup', kind: 'voice' }),
      ],
      '',
    )
    expect(groups.map((group) => group.kind)).toEqual(['voice', 'text'])
    expect(groups[0]?.choices.map((choice) => choice.label)).toEqual(['Standup'])
  })

  it('keeps the order the API sent inside a group', () => {
    // The API sorts by position and then by name. Re-sorting here would
    // reshuffle the list under somebody's cursor whenever a channel moved.
    const { groups } = channelChoices(
      [
        channel({ id: '10', name: 'Standup', position: 3 }),
        channel({ id: '12', name: 'Retro', position: 4 }),
      ],
      '',
    )
    expect(groups[0]?.choices.map((choice) => choice.id)).toEqual(['10', '12'])
  })

  it('renders a kind it has never heard of rather than dropping the channel', () => {
    // Discord keeps adding channel types. A picker that only knew the
    // kinds this console was written against would silently hide a
    // recordable channel, and nothing on screen would say so.
    const { groups } = channelChoices(
      [channel({ id: '13', name: 'Watch party', kind: 'media_stage_beta' })],
      '',
    )
    const unknown = groups.find((group) => group.kind === 'media_stage_beta')
    expect(unknown?.labelKey).toBeNull()
    expect(unknown?.choices.map((choice) => choice.label)).toEqual(['Watch party'])
  })

  it('offers a chosen id the mirror does not know, marked as unresolved', () => {
    const { groups } = channelChoices([channel({ id: '10', name: 'Standup' })], '10,999')
    const stray = groups.flatMap((group) => group.choices).find((choice) => choice.id === '999')
    expect(stray?.resolved).toBe(false)
    expect(stray?.label).toBe('999')
  })

  it('reports the chosen channels in the order they are stored', () => {
    const { selected } = channelChoices(
      [channel({ id: '10', name: 'Standup' }), channel({ id: '12', name: 'Retro' })],
      '12,10',
    )
    expect(selected.map((choice) => choice.label)).toEqual(['Retro', 'Standup'])
  })

  it('reports a chosen channel that no longer exists among the chosen ones', () => {
    const { selected } = channelChoices([channel({ id: '10', name: 'Standup' })], '10,999')
    expect(selected.map((choice) => choice.resolved)).toEqual([true, false])
    expect(selected[1]?.label).toBe('999')
  })

  it('reports nothing chosen for an empty value', () => {
    expect(channelChoices([channel({ id: '10' })], '').selected).toEqual([])
  })
})

describe('the single-choice pickers', () => {
  const roles = [
    { id: '77', name: 'Recorded' },
    { id: '78', name: 'Moderators' },
  ]

  it('offers every row the mirror has', () => {
    const { choices } = singleChoices(roles, '77')
    expect(choices.map((choice) => choice.label)).toEqual(['Recorded', 'Moderators'])
  })

  it('offers the stored id as well when the mirror has no row for it', () => {
    // Without this the select would render as if nothing were configured,
    // and the first save from this page would overwrite a value the
    // administrator never saw.
    const { choices, current } = singleChoices(roles, '999')
    expect(choices.map((choice) => choice.id)).toContain('999')
    expect(current?.resolved).toBe(false)
    expect(current?.label).toBe('999')
  })

  it('has nothing current when nothing is stored', () => {
    expect(singleChoices(roles, '').current).toBeNull()
    expect(singleChoices(roles, '   ').current).toBeNull()
  })

  it('does not invent an option for an empty value', () => {
    expect(singleChoices(roles, '').choices.map((choice) => choice.id)).toEqual(['77', '78'])
  })
})

describe('the empty row of a single-choice picker', () => {
  const chosen = { id: '77', name: 'Recorded', label: 'Recorded', resolved: true }

  it('offers "not set" where an empty value is a value the API accepts', () => {
    expect(blankOption(view('document_target'), chosen)).toBe('offer')
  })

  it('refuses to offer it for a required key, which cannot be empty', () => {
    // The page never offers an action it already knows will fail. Emptying
    // a required key is refused by `validateValue` and answered 409 by the
    // API; the empty row would be a control whose only outcome is a
    // complaint.
    expect(blankOption(view('document_target', { required: true }), chosen)).toBe('none')
  })

  it('refuses to offer it for an integer key, where empty is not a number', () => {
    expect(blankOption(view('consent_role_id', { integer: true }), chosen)).toBe('none')
  })

  it('still shows a placeholder while nothing is chosen', () => {
    // Otherwise the select would open on the first role in the list and
    // look as though that were the stored value.
    expect(blankOption(view('consent_role_id', { integer: true }), null)).toBe('placeholder')
    expect(blankOption(view('document_target', { required: true }), null)).toBe('placeholder')
  })
})

describe('what a channel kind is called', () => {
  it('names the kinds this console knows', () => {
    expect(channelKindHeading('voice').labelKey).toBe('admin.settings.kindVoice')
    expect(channelKindHeading('stage_voice').labelKey).toBe('admin.settings.kindStage')
    expect(channelKindHeading('text').labelKey).toBe('admin.settings.kindText')
  })

  it('hands back the raw kind for one it does not, rather than a guess', () => {
    const heading = channelKindHeading('media_stage_beta')
    expect(heading.labelKey).toBeNull()
    expect(heading.raw).toBe('media_stage_beta')
  })
})

describe('how fresh the mirror is', () => {
  const AT = '2026-08-23T12:00:00+00:00'
  const noon = Date.parse(AT)

  it('says the bot has not swept yet when there is no timestamp', () => {
    // An empty list under no sentence at all reads as "this server has no
    // channels", which is how somebody concludes the picker is broken
    // rather than that the mirror is empty.
    const fresh = mirrorFreshness(null, noon)
    expect(fresh.key).toBe('admin.settings.mirrorNeverSwept')
    expect(fresh.stale).toBe(true)
  })

  it('names the moment of the sweep and how long ago it was', () => {
    const fresh = mirrorFreshness(AT, noon + 15 * 60 * 1000)
    expect(fresh.key).toBe('admin.settings.mirrorSyncedAgo')
    expect(fresh.params.moment).toBe('23 Aug 2026, 12:00 UTC')
    expect(fresh.params.age).toBe('15 min')
    expect(fresh.stale).toBe(false)
  })

  it('says a sweep from hours ago may be behind Discord', () => {
    const fresh = mirrorFreshness(AT, noon + 6 * 60 * 60 * 1000)
    expect(fresh.key).toBe('admin.settings.mirrorStale')
    expect(fresh.params.age).toBe('6 h')
    expect(fresh.stale).toBe(true)
  })

  it('names the moment alone before the browser has a clock', () => {
    // The page has no `now` until it has mounted, and a server render that
    // computed an age would disagree with the browser's a second later --
    // which Vue reports as a hydration mismatch on a paragraph nobody
    // edited.
    const fresh = mirrorFreshness(AT, null)
    expect(fresh.key).toBe('admin.settings.mirrorSynced')
    expect(fresh.params.moment).toBe('23 Aug 2026, 12:00 UTC')
    expect(fresh.stale).toBe(false)
  })

  it('does not claim an age for a sweep dated in the future', () => {
    const fresh = mirrorFreshness(AT, noon - 60 * 1000)
    expect(fresh.key).toBe('admin.settings.mirrorSynced')
  })

  it('admits it when the timestamp cannot be read at all', () => {
    const fresh = mirrorFreshness('the day before yesterday', noon)
    expect(fresh.key).toBe('admin.settings.mirrorSyncUnreadable')
    expect(fresh.stale).toBe(true)
  })
})

describe('reading what the directory endpoint sent', () => {
  it('takes the channels, roles and members apart', () => {
    const directory = parseDirectory(PAYLOAD)
    expect(directory.guildId).toBe('4711')
    expect(directory.syncedAt).toBe('2026-08-23T12:00:00+00:00')
    expect(directory.channels.map((row) => row.name)).toEqual(['Standup', 'Announcements'])
    expect(directory.roles.map((row) => row.name)).toEqual(['Recorded'])
    expect(directory.members).toEqual([{ id: '100', name: 'Anna Example' }])
  })

  it('reads a numeric id as the string it has to stay', () => {
    // Snowflakes exceed `Number.MAX_SAFE_INTEGER`. An id that arrived as a
    // number has already lost digits; one turned into a number here would
    // lose them on the way back out.
    const directory = parseDirectory({ channels: [{ id: '10', name: 'Standup', kind: 'voice' }] })
    expect(directory.channels[0]?.id).toBe('10')
  })

  it('keeps a channel whose kind is missing rather than dropping it', () => {
    const directory = parseDirectory({ channels: [{ id: '10', name: 'Standup' }] })
    expect(directory.channels[0]?.kind).toBe('')
  })

  it('skips a row with no id, which is a row nothing could ever be set to', () => {
    const directory = parseDirectory({ channels: [{ name: 'Nameless' }, { id: '10', name: 'A' }] })
    expect(directory.channels.map((row) => row.id)).toEqual(['10'])
  })

  it('reads a channel with no name as its own id', () => {
    const directory = parseDirectory({ channels: [{ id: '10', kind: 'voice' }] })
    expect(directory.channels[0]?.name).toBe('10')
  })

  it('answers with empty lists for a payload that is not one', () => {
    for (const payload of [null, undefined, 'no', 42, []]) {
      const directory = parseDirectory(payload)
      expect(directory.channels).toEqual([])
      expect(directory.roles).toEqual([])
      expect(directory.syncedAt).toBeNull()
    }
  })

  it('reads the Outline collections and when they were last read', () => {
    const collections = parseCollections({
      synced_at: '2026-08-23T12:00:00+00:00',
      collections: [{ id: 'c-1', name: 'Meetings' }],
    })
    expect(collections.syncedAt).toBe('2026-08-23T12:00:00+00:00')
    expect(collections.collections).toEqual([{ id: 'c-1', name: 'Meetings' }])
  })

  it('answers with no collections at all for a payload it cannot read', () => {
    expect(parseCollections(null).collections).toEqual([])
    expect(parseCollections({ collections: 'soon' }).collections).toEqual([])
  })
})
