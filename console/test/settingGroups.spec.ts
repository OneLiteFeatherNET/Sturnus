/**
 * Which tab a configuration key belongs on, and what a tab says about
 * what is inside it.
 *
 * Two of these are worth more than the grouping itself, and both are the
 * kind of failure a screenshot does not show:
 *
 * **A key nobody has filed is still reachable.** Keys arrive in other
 * pull requests. A mapping that answered "no group" by dropping the key
 * would hide a setting from the only page that can change it, and the
 * page would look complete while doing it. So the catch-all is asserted
 * here by name, with a key this console has never heard of.
 *
 * **A tab says when something behind it needs attention.** A required key
 * with no value is what stops the bot watching a guild at all. In a flat
 * list it was at least on the screen; behind a tab it is behind a tab, so
 * the tab has to carry the mark — and it has to be *words*, because a
 * coloured dot is not read out to anybody who is not looking at it.
 */
import { describe, expect, it } from 'vitest'

import {
  SETTING_GROUPS,
  groupOfKey,
  groupSettings,
  groupTabLabel,
} from '../app/utils/settingGroups'
import type { SettingView } from '../app/utils/settings'

/** A key view with everything harmless, so each test states only the one
 *  property it is actually about. */
function view(overrides: Partial<SettingView> & { key: string }): SettingView {
  return {
    value: 'something',
    default: null,
    required: false,
    may_clear: false,
    integer: false,
    invalidates_consent: false,
    takes_effect: 'immediately',
    deferred_while_recording: false,
    ...overrides,
  }
}

/** A required key with nothing set — the state a tab has to advertise. */
const unset = (key: string) => view({ key, required: true, value: null })

describe('which group a key belongs to', () => {
  it.each([
    ['voice_channel_ids', 'recording'],
    ['voice_channel_id', 'recording'],
    ['empty_grace_seconds', 'recording'],
    ['idle_timeout_minutes', 'recording'],
    ['max_session_hours', 'recording'],
    ['consent_role_id', 'consent'],
    ['policy_version', 'consent'],
    ['policy_url', 'consent'],
    ['video_consent_offered', 'consent'],
    ['audio_retention_days', 'consent'],
    ['transcription_language', 'transcription'],
    ['transcription_prompt', 'transcription'],
    ['document_provider', 'publishing'],
    ['document_target', 'publishing'],
    ['publish_poll_seconds', 'publishing'],
    ['merge_gap_seconds', 'publishing'],
    ['timezone', 'publishing'],
    ['admin_role_id', 'administration'],
  ])('files %s under %s', (key, group) => {
    expect(groupOfKey(key)).toBe(group)
  })

  it('keeps the deprecated spelling beside the one that replaced it', () => {
    // `voice_channel_id` and `voice_channel_ids` are one setting with two
    // names. Filing them on different tabs would let a guild move off the
    // deprecated key without ever seeing the two side by side.
    expect(groupOfKey('voice_channel_id')).toBe(groupOfKey('voice_channel_ids'))
  })

  it('files a key it has never heard of rather than losing it', () => {
    // The failure this exists to stop: a settings page that silently omits
    // a setting is worse than an ugly one, and the API grows keys on its
    // own schedule.
    expect(groupOfKey('spectrograms_by_default')).toBe('other')
    expect(groupOfKey('a_key_from_next_year')).toBe('other')
  })

  it('names every group exactly once, catch-all last', () => {
    const ids = SETTING_GROUPS.map((group) => group.id)
    expect(new Set(ids).size).toBe(ids.length)
    expect(ids.at(-1)).toBe('other')
  })

  it('claims no key twice', () => {
    // Two groups claiming one key would render it on two tabs, each with
    // its own draft and its own Save. Whichever was pressed last would win
    // and the other would sit there looking unsaved.
    const claimed = SETTING_GROUPS.flatMap((group) => group.keys)
    expect(new Set(claimed).size).toBe(claimed.length)
  })

  it('gives every group a translation key rather than a sentence', () => {
    for (const group of SETTING_GROUPS) {
      expect(group.labelKey).toMatch(/^admin\.settings\.group[A-Z]/)
    }
  })
})

describe('the groups a payload actually produces', () => {
  it('renders no tab for a group whose keys the API has not sent', () => {
    // A deployment whose API predates a key must not be shown an empty
    // tab: an empty panel reads as a page that failed to load.
    const groups = groupSettings([view({ key: 'timezone' })])
    expect(groups.map((group) => group.id)).toEqual(['publishing'])
  })

  it('keeps the groups in one order regardless of what arrived', () => {
    const groups = groupSettings([
      view({ key: 'admin_role_id' }),
      view({ key: 'timezone' }),
      view({ key: 'voice_channel_ids' }),
    ])
    expect(groups.map((group) => group.id)).toEqual(['recording', 'publishing', 'administration'])
  })

  it('shows an unknown key on the catch-all tab', () => {
    const groups = groupSettings([view({ key: 'spectrograms_by_default' })])
    expect(groups.map((group) => group.id)).toEqual(['other'])
    expect(groups[0]?.views.map((held) => held.key)).toEqual(['spectrograms_by_default'])
  })

  it('files the two keys that arrived most recently', () => {
    // Filed because what they do is known, not because their names were
    // read for a hint. `spectrograms_by_default` above is the difference:
    // nobody has said what it belongs with, so nobody guessed.
    expect(groupOfKey('admin_audio_download_offered')).toBe('consent')
    expect(groupOfKey('max_parallel_tracks')).toBe('transcription')
  })

  it('puts the catch-all after the groups it knows', () => {
    const groups = groupSettings([view({ key: 'brand_new_key' }), view({ key: 'timezone' })])
    expect(groups.map((group) => group.id)).toEqual(['publishing', 'other'])
  })

  it('keeps a required key with nothing set at the top of its own group', () => {
    // The ranking `orderSettings` applies to the flat list applies inside
    // each group too: the keys stopping the guild being watched are still
    // the first thing on the tab.
    const groups = groupSettings([
      view({ key: 'timezone' }),
      view({ key: 'document_provider' }),
      unset('document_target'),
    ])
    expect(groups[0]?.views.map((held) => held.key)).toEqual([
      'document_target',
      'document_provider',
      'timezone',
    ])
  })

  it('loses no key at all', () => {
    const keys = [
      'voice_channel_ids',
      'consent_role_id',
      'transcription_prompt',
      'document_target',
      'admin_role_id',
      'something_nobody_here_has_seen',
    ]
    const groups = groupSettings(keys.map((key) => view({ key })))
    expect(groups.flatMap((group) => group.views.map((held) => held.key)).sort()).toEqual(
      [...keys].sort(),
    )
  })

  it('leaves the list it was given untouched', () => {
    const given = [view({ key: 'timezone' }), view({ key: 'document_provider' })]
    const before = given.map((held) => held.key)
    groupSettings(given)
    expect(given.map((held) => held.key)).toEqual(before)
  })
})

describe('what a tab says about what is behind it', () => {
  it('names the group and nothing else when everything inside it is set', () => {
    const groups = groupSettings([view({ key: 'timezone' })])
    expect(groupTabLabel(groups[0]!)).toEqual({ key: 'admin.settings.groupPublishing' })
  })

  it('marks a tab holding a required key with no value', () => {
    // The failure the flat list did not have: a key that stops the bot
    // watching this guild, on a tab nobody opened.
    const groups = groupSettings([unset('document_target'), view({ key: 'timezone' })])
    expect(groupTabLabel(groups[0]!)).toEqual({
      key: 'admin.settings.groupNeedsAttention',
      params: { group: { key: 'admin.settings.groupPublishing' }, count: 1 },
    })
  })

  it('counts every unset required key on the tab rather than reporting one', () => {
    const groups = groupSettings([unset('policy_version'), unset('policy_url')])
    expect(groups[0]?.needsAttention).toEqual(['policy_url', 'policy_version'])
    expect(groupTabLabel(groups[0]!).params?.count).toBe(2)
  })

  it('says nothing about a required key that has a value', () => {
    const groups = groupSettings([view({ key: 'policy_url', required: true, value: 'https://x' })])
    expect(groups[0]?.needsAttention).toEqual([])
  })

  it('marks the catch-all tab too, so a new required key is not hidden twice', () => {
    // A key that is both new and required is the worst case: unfiled and
    // load-bearing. The catch-all is a group like any other here.
    const groups = groupSettings([unset('a_required_key_from_next_year')])
    expect(groupTabLabel(groups[0]!)).toEqual({
      key: 'admin.settings.groupNeedsAttention',
      params: { group: { key: 'admin.settings.groupOther' }, count: 1 },
    })
  })

  it('marks only the tab the missing key is on', () => {
    const groups = groupSettings([unset('policy_url'), view({ key: 'timezone' })])
    const marked = groups.filter((group) => group.needsAttention.length > 0)
    expect(marked.map((group) => group.id)).toEqual(['consent'])
  })
})
