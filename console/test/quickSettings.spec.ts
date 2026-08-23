/**
 * What the dashboard band shows, and to whom.
 *
 * Three failures are worth a build breaking over, and none of them is
 * visible in a render of the happy case.
 *
 * The first is a band that says something false about somebody's own data.
 * A failed read of `/api/me/consents` and a genuinely empty list produce the
 * same nothing in a naive template, and only one of them means "nothing of
 * you is being recorded anywhere".
 *
 * The second is a dashboard broken by a settings key. The band names the
 * keys it would like to show; the registry decides which exist. A name
 * nothing serves has to contribute nothing -- not a blank row, and certainly
 * not an exception on the page everybody lands on.
 *
 * The third is the quiet one: a control here that writes a key the full page
 * would have made somebody confirm first. Skipping a warning the other page
 * insists on is worse than not offering the control, and it is invisible
 * until the meeting somebody was in stops being recorded.
 */
import { describe, expect, it } from 'vitest'

import {
  BOT_SETTINGS_PATH,
  QUICK_SETTING_KEYS,
  bandIsEmpty,
  consentBand,
  dashboardBand,
  mayClearHere,
  mayWriteHere,
  selectQuickSettings,
} from '../app/utils/quickSettings'
import type { SettingView } from '../app/utils/settings'

function view(key: string, over: Partial<SettingView> = {}): SettingView {
  return {
    key,
    value: 'x',
    default: 'x',
    required: false,
    integer: false,
    invalidates_consent: false,
    takes_effect: 'immediately',
    deferred_while_recording: false,
    ...over,
  }
}

const LANGUAGE = view('transcription_language', { value: 'de', default: 'de' })
const RETENTION = view('audio_retention_days', { value: '30', default: '30', integer: true })
const CHANNEL = view('voice_channel_id', { value: '4711', default: null, required: true })

describe('which settings the band offers', () => {
  it('shows only the keys the registry actually served', () => {
    // The whole payload of a real guild is dozens of keys. The band is the
    // handful a server changes often rather than once; everything else is a
    // click away and stays there.
    const { shown } = selectQuickSettings([
      view('policy_url'),
      LANGUAGE,
      view('timezone'),
      RETENTION,
      view('transcription_prompt'),
    ])
    expect(shown.map((each) => each.key)).toEqual([
      'transcription_language',
      'audio_retention_days',
    ])
  })

  it('renders them in this module\'s order rather than the payload\'s', () => {
    // The API sorts its keys alphabetically, which would put the retention
    // window above the recording channels. A band that reshuffles itself
    // when the API changes its sort order is a band nobody can build a habit
    // on.
    const { shown } = selectQuickSettings([RETENTION, LANGUAGE, CHANNEL])
    expect(shown.map((each) => each.key)).toEqual([
      'voice_channel_id',
      'transcription_language',
      'audio_retention_days',
    ])
  })

  it('is simply missing a key the API does not know, rather than breaking', () => {
    // `voice_channel_ids` is the plural key that arrives with the change
    // letting a guild name more than one recording channel. Against today's
    // API it does not exist, and the band still has to render -- this is the
    // property that keeps a rename upstream from taking the dashboard down.
    expect(QUICK_SETTING_KEYS).toContain('voice_channel_ids')
    const { shown, withheld } = selectQuickSettings([LANGUAGE])
    expect(shown.map((each) => each.key)).toEqual(['transcription_language'])
    expect(withheld).toEqual([])
  })

  it('offers nothing at all when the registry serves none of them', () => {
    expect(selectQuickSettings([view('policy_url'), view('timezone')])).toEqual({
      shown: [],
      withheld: [],
    })
  })

  it('shows whichever spelling of the recording-channel key is real', () => {
    // Both spellings are listed, and exactly one of them is ever served, so
    // the band works either side of that deploy without another change here.
    const plural = selectQuickSettings([view('voice_channel_ids'), LANGUAGE])
    expect(plural.shown.map((each) => each.key)).toEqual([
      'voice_channel_ids',
      'transcription_language',
    ])
  })
})

describe('a key whose write would need confirming', () => {
  const CONSENT_KEY = view('transcription_language', { invalidates_consent: true })

  it('is withheld rather than offered without the confirmation', () => {
    // The full page asks before writing a key that invalidates consent:
    // every consent naming the old value stops counting, mid-meeting, within
    // the consent cache's TTL. This band has no room for that dialogue, so
    // it does not offer the key.
    const { shown, withheld } = selectQuickSettings([CONSENT_KEY, RETENTION])
    expect(shown.map((each) => each.key)).toEqual(['audio_retention_days'])
    expect(withheld).toEqual(['transcription_language'])
  })

  it('is named rather than dropped silently, so the band can say where it lives', () => {
    // An absence nobody explains reads as a control that failed to render.
    expect(selectQuickSettings([CONSENT_KEY]).withheld).toEqual(['transcription_language'])
    expect(BOT_SETTINGS_PATH).toBe('/admin/bot-settings')
  })

  it('is refused by the write guard as well as by the selection', () => {
    // Two gates on one rule, because the second is what stops a control that
    // is somehow on screen from writing anyway.
    expect(mayWriteHere(CONSENT_KEY)).toBe(false)
    expect(mayWriteHere(RETENTION)).toBe(true)
  })

  it('follows the flag rather than a list of key names', () => {
    // A key that starts invalidating consent tomorrow leaves the band on its
    // own, with no edit here.
    expect(mayWriteHere(view('audio_retention_days', { invalidates_consent: true }))).toBe(false)
  })
})

describe('clearing from the band', () => {
  it('is offered exactly where the full page offers it', () => {
    // `clearability` is this console's mirror of the API's `may_clear`, and
    // asking it rather than re-deciding is what keeps the two pages from
    // disagreeing about whether a key can be emptied.
    expect(mayClearHere(RETENTION)).toBe(true)
    expect(mayClearHere(CHANNEL)).toBe(false)
  })

  it('is not offered for a key with nothing stored', () => {
    expect(mayClearHere(view('transcription_language', { value: null }))).toBe(false)
  })
})

describe('the consent half', () => {
  it('says nothing at all to somebody with no consent record anywhere', () => {
    // Not an empty box and not "you have consented nowhere": a heading over
    // an empty space on the landing page is a worse answer than no heading.
    expect(consentBand({ failed: false, records: 0 })).toBe('silent')
  })

  it('names the failure rather than rendering an absence as an answer', () => {
    // `/api/me/consents` answers 404 until it is deployed, and the console
    // ships as a separate image. An empty list drawn in its place would tell
    // somebody they have consented nowhere.
    expect(consentBand({ failed: true, records: 0 })).toBe('unavailable')
  })

  it('prefers the failure even when records were already on hand', () => {
    // A refreshed read that failed leaves stale rows in the ref. Drawing
    // them under no warning is how somebody acts on a state that has
    // already changed.
    expect(consentBand({ failed: true, records: 3 })).toBe('unavailable')
  })

  it('shows the records when there are any', () => {
    expect(consentBand({ failed: false, records: 1 })).toBe('records')
  })
})

describe('who sees what', () => {
  it('gives a plain participant their consent and no server settings', () => {
    const band = dashboardBand({
      consentFailed: false,
      consentRecords: 2,
      administeredGuilds: 0,
    })
    expect(band).toEqual({ consent: 'records', guildSettings: false })
    expect(bandIsEmpty(band)).toBe(false)
  })

  it('gives an administrator both halves', () => {
    expect(
      dashboardBand({ consentFailed: false, consentRecords: 1, administeredGuilds: 2 }),
    ).toEqual({ consent: 'records', guildSettings: true })
  })

  it('gives an administrator the settings half even with no consent of their own', () => {
    // Administering a server and taking part in its meetings are different
    // things, and somebody who only does the first still has settings to
    // change.
    const band = dashboardBand({
      consentFailed: false,
      consentRecords: 0,
      administeredGuilds: 1,
    })
    expect(band).toEqual({ consent: 'silent', guildSettings: true })
    expect(bandIsEmpty(band)).toBe(false)
  })

  it('gives somebody who is neither nothing at all', () => {
    // The case the whole composition exists for: no band, no separator, no
    // space where one would have been.
    const band = dashboardBand({
      consentFailed: false,
      consentRecords: 0,
      administeredGuilds: 0,
    })
    expect(band).toEqual({ consent: 'silent', guildSettings: false })
    expect(bandIsEmpty(band)).toBe(true)
  })

  it('still speaks to somebody whose consent could not be read and who administers nothing', () => {
    // `[]` from `/api/guilds` is an answer; a 404 from the consent endpoint
    // is not, and the band exists solely to say so.
    const band = dashboardBand({
      consentFailed: true,
      consentRecords: 0,
      administeredGuilds: 0,
    })
    expect(bandIsEmpty(band)).toBe(false)
  })
})
