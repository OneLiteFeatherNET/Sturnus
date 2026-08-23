/**
 * What a configuration key means, and what changing one actually does.
 *
 * All of it lives in `~/utils/settings` rather than in the page, because
 * every one of these is a decision -- whether a key may be cleared,
 * whether a change needs confirming, when a written value is actually in
 * force -- and a decision embedded in a template can only be tested by
 * rendering one.
 *
 * The wording is asserted here on purpose. "Saved" for a key that needs a
 * pod restart is the exact lie the Discord `/config` replies were built to
 * stop telling; a test that only checked a boolean would let it back in.
 */
import { describe, expect, it } from 'vitest'

import {
  SELECTED_GUILD_KEY,
  chooseGuild,
  clearability,
  confirmation,
  describeError,
  effectBadge,
  fieldHints,
  guildLabel,
  guildOptions,
  inputKind,
  keyLabel,
  missingRequired,
  orderSettings,
  parseGuilds,
  parseSettings,
  readSelectedGuild,
  summariseValue,
  validateValue,
  writeOutcome,
  writeSelectedGuild,
  type SettingView,
} from '../app/utils/settings'
import type { KeyValueStore } from '../app/utils/preferences'

/** A key view with everything harmless, so each test states only the one
 *  property it is actually about. */
function view(overrides: Partial<SettingView> & { key: string }): SettingView {
  return {
    value: null,
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

function memoryStore(initial: Record<string, string> = {}): KeyValueStore {
  const data = { ...initial }
  return {
    getItem: (key) => (key in data ? data[key]! : null),
    setItem: (key, value) => {
      data[key] = value
    },
  }
}

describe('reading the settings payload', () => {
  it('reads the keys out of a `settings` list', () => {
    const parsed = parseSettings({
      settings: [
        {
          key: 'timezone',
          value: 'Europe/Berlin',
          default: 'Europe/Berlin',
          required: false,
          integer: false,
          invalidates_consent: false,
          takes_effect: 'immediately',
          deferred_while_recording: false,
        },
      ],
    })
    expect(parsed).toHaveLength(1)
    expect(parsed[0]!.key).toBe('timezone')
    expect(parsed[0]!.value).toBe('Europe/Berlin')
  })

  it('reads the keys out of a bare list', () => {
    expect(parseSettings([{ key: 'timezone' }]).map((v) => v.key)).toEqual(['timezone'])
  })

  it('reads the keys out of an object keyed by name', () => {
    // The endpoint is described as "every key with value, default, ..." and
    // that is equally a map and a list. Accepting both costs eight lines
    // and removes a whole class of "the page renders empty and nobody
    // knows why".
    const parsed = parseSettings({ timezone: { value: 'UTC' }, policy_url: { value: null } })
    expect(parsed.map((v) => v.key).sort()).toEqual(['policy_url', 'timezone'])
  })

  it('treats an absent value as unset rather than as an empty string', () => {
    // Unset and empty are different things: one is cleared, the other is
    // a stored empty string, and only one of them falls back to a default.
    expect(parseSettings([{ key: 'policy_url' }])[0]!.value).toBeNull()
  })

  it('treats every missing flag as off rather than guessing', () => {
    const parsed = parseSettings([{ key: 'timezone' }])[0]!
    expect(parsed.required).toBe(false)
    expect(parsed.integer).toBe(false)
    expect(parsed.invalidates_consent).toBe(false)
    expect(parsed.deferred_while_recording).toBe(false)
  })

  it('drops an entry that names no key', () => {
    expect(parseSettings([{ value: 'orphan' }, { key: 'timezone' }])).toHaveLength(1)
  })

  it('yields nothing for a payload it cannot make sense of', () => {
    expect(parseSettings(null)).toEqual([])
    expect(parseSettings('nonsense')).toEqual([])
  })
})

describe('reading the guild list', () => {
  it('reads the ids the caller administers', () => {
    expect(parseGuilds({ guilds: [{ guild_id: '42' }, { guild_id: '43' }] }).map((g) => g.id)).toEqual([
      '42',
      '43',
    ])
  })

  it('yields nothing for somebody who administers nothing', () => {
    expect(parseGuilds({ guilds: [] })).toEqual([])
  })

  it('keeps a name when the API sends one', () => {
    // It does not today. Reading it if it ever appears is what lets the
    // switcher stop showing two indistinguishable snowflakes without the
    // console needing another change.
    expect(parseGuilds({ guilds: [{ guild_id: '42', name: 'OneLiteFeather' }] })[0]!.name).toBe(
      'OneLiteFeather',
    )
  })

  it('renders an id as a string even if it arrived as a number', () => {
    // A snowflake exceeding the safe integer range has already lost its
    // last digits before this function sees it. Stringifying cannot undo
    // that -- it only keeps the switcher working, and the damage then
    // surfaces as a 404 rather than as the wrong guild being edited.
    expect(parseGuilds({ guilds: [{ guild_id: 42 }] })[0]!.id).toBe('42')
  })
})

describe('naming a guild', () => {
  it('uses the name when there is one', () => {
    expect(guildLabel({ id: '42', name: 'OneLiteFeather' })).toBe('OneLiteFeather')
  })

  it('falls back to the whole id, never a shortened one', () => {
    // Two snowflakes from the same era share their leading digits. A
    // truncated id is exactly the ambiguity the switcher exists to remove.
    expect(guildLabel({ id: '1129384756123456789', name: null })).toContain('1129384756123456789')
  })
})

describe('the guilds as rows of a dropdown', () => {
  it('puts the id under the name, where the control renders subtext', () => {
    expect(guildOptions([{ id: '42', name: 'OneLiteFeather' }])).toEqual([
      { value: '42', label: 'OneLiteFeather', detail: '42' },
    ])
  })

  it('does not print the id twice for a guild that has no name', () => {
    // `guildLabel` has already put the whole snowflake in the label. A
    // `detail` repeating it renders eighteen digits above eighteen digits.
    const [option] = guildOptions([{ id: '1129384756123456789', name: null }])
    expect(option?.label).toContain('1129384756123456789')
    expect(option?.detail).toBeUndefined()
  })

  it('keeps the order the API sent', () => {
    const options = guildOptions([
      { id: '2', name: 'Second' },
      { id: '1', name: 'First' },
    ])
    expect(options.map((option) => option.value)).toEqual(['2', '1'])
  })

  it('yields nothing for somebody who administers nothing', () => {
    expect(guildOptions([])).toEqual([])
  })
})

describe('saying when a change takes effect', () => {
  it('says an immediately-read key is in force now', () => {
    const outcome = writeOutcome(view({ key: 'timezone', takes_effect: 'immediately' }), 'saved')
    expect(outcome.tone).toBe('live')
    expect(outcome.headline.toLowerCase()).toContain('in effect now')
  })

  it('says a cached key lands within about ten seconds, not that it is done', () => {
    const outcome = writeOutcome(
      view({ key: 'voice_channel_id', takes_effect: 'next_reconcile' }),
      'saved',
    )
    expect(outcome.tone).toBe('soon')
    expect(outcome.headline).toContain('ten seconds')
  })

  it('names the running recording for a key that is held during one', () => {
    const outcome = writeOutcome(
      view({
        key: 'voice_channel_id',
        takes_effect: 'next_reconcile',
        deferred_while_recording: true,
      }),
      'saved',
    )
    expect(outcome.detail).toContain('recording')
    expect(outcome.detail).toContain('never lost')
  })

  it('does not promise a recording is held back for a key that is not', () => {
    const outcome = writeOutcome(
      view({ key: 'idle_timeout_minutes', takes_effect: 'next_reconcile' }),
      'saved',
    )
    expect(outcome.detail).not.toContain('recording')
  })

  it('says outright that waiting will not apply a restart-only key', () => {
    const outcome = writeOutcome(
      view({ key: 'publish_poll_seconds', takes_effect: 'process_restart' }),
      'saved',
    )
    expect(outcome.tone).toBe('restart')
    expect(outcome.detail.toLowerCase()).toContain('restart')
  })

  it('never lets a restart-only key read as finished', () => {
    // This is the whole reason the page exists in this shape. "Saved."
    // alone, for a key nothing will pick up until somebody restarts the
    // deployment, is a lie the console must not tell.
    const outcome = writeOutcome(
      view({ key: 'publish_poll_seconds', takes_effect: 'process_restart' }),
      'saved',
    )
    expect(outcome.headline.toLowerCase()).toContain('not in force')
  })

  it('does not dangle a deferral note on a key nothing will read anyway', () => {
    // Held-until-the-recording-ends is meaningless when the value is not
    // read again until the process restarts.
    const outcome = writeOutcome(
      view({
        key: 'publish_poll_seconds',
        takes_effect: 'process_restart',
        deferred_while_recording: true,
      }),
      'saved',
    )
    expect(outcome.detail).not.toContain('ten seconds')
  })

  it('admits it does not know for a timing it has never heard of', () => {
    // A fourth `takes_effect` added to the API must not silently render as
    // the friendliest of the three.
    const outcome = writeOutcome(view({ key: 'something_new', takes_effect: 'next_full_moon' }), 'saved')
    expect(outcome.tone).toBe('unknown')
    expect(outcome.headline.toLowerCase()).toContain('not known')
  })

  it('describes a clear with the same honesty as a save', () => {
    const outcome = writeOutcome(
      view({ key: 'publish_poll_seconds', takes_effect: 'process_restart' }),
      'cleared',
    )
    expect(outcome.headline).toContain('Cleared')
    expect(outcome.detail.toLowerCase()).toContain('restart')
  })

  it('says which key it is about, in every one of the four tones', () => {
    // Grouped onto tabs, two of these panels can be open a screen apart,
    // one of them left over from a key written a minute ago. A headline
    // that names no key can be read against the wrong one -- and the
    // reading that costs something is a cheerful "in effect now" being
    // taken for the restart-only key underneath it.
    for (const timing of ['immediately', 'next_reconcile', 'process_restart', 'next_full_moon']) {
      const outcome = writeOutcome(view({ key: 'admin_role_id', takes_effect: timing }), 'saved')
      expect(outcome.headline, `${timing} names no key`).toContain('Admin role ID')
    }
  })

  it('names the key the way the heading above it names it', () => {
    // Not the raw key: the two sit centimetres apart, and a reader should
    // not have to work out that they are the same word twice.
    const outcome = writeOutcome(
      view({ key: 'merge_gap_seconds', takes_effect: 'immediately' }),
      'saved',
    )
    expect(outcome.headline).toContain(keyLabel('merge_gap_seconds'))
    expect(outcome.headline).not.toContain('merge_gap_seconds')
  })
})

describe('the badge shown before anybody edits anything', () => {
  it('marks a key that is read per use', () => {
    expect(effectBadge(view({ key: 'timezone', takes_effect: 'immediately' })).tone).toBe('live')
  })

  it('warns about a restart before the edit rather than after it', () => {
    const badge = effectBadge(view({ key: 'publish_poll_seconds', takes_effect: 'process_restart' }))
    expect(badge.tone).toBe('restart')
    expect(badge.label.toLowerCase()).toContain('restart')
  })

  it('says a cached key is held during a recording', () => {
    const badge = effectBadge(
      view({
        key: 'voice_channel_id',
        takes_effect: 'next_reconcile',
        deferred_while_recording: true,
      }),
    )
    expect(badge.label.toLowerCase()).toContain('recording')
  })
})

describe('whether a key may be cleared', () => {
  it('refuses to offer a clear for a required key', () => {
    // An interface that offers an action it knows will answer 409 is
    // worse than one that explains why it cannot.
    const verdict = clearability(view({ key: 'policy_url', required: true, value: 'https://x' }))
    expect(verdict.clearable).toBe(false)
  })

  it('explains a required key in terms of the missing default', () => {
    const verdict = clearability(view({ key: 'policy_url', required: true, value: 'https://x' }))
    expect(verdict.clearable).toBe(false)
    if (!verdict.clearable) expect(verdict.reason).toContain('no default')
  })

  it('offers a clear for a key that has a default to fall back to', () => {
    expect(
      clearability(view({ key: 'timezone', may_clear: true, value: 'UTC', default: 'Europe/Berlin' }))
        .clearable,
    ).toBe(true)
  })

  it('refuses when there is nothing stored to clear', () => {
    const verdict = clearability(view({ key: 'transcription_prompt', may_clear: true, value: null }))
    expect(verdict.clearable).toBe(false)
  })

  it('refuses a key the API will not clear even though it is not required', () => {
    // `voice_channel_id` is the deprecated spelling of the recording
    // channels. It is required of nobody -- so a console reading
    // clearability off `required` puts a live Clear button beside it --
    // and clearing it would take a guild that has not moved to
    // `voice_channel_ids` yet out of service, so the API answers 409.
    // Inferring the button from `required` renders the refusal
    // "this key is required" on a field the same page called optional.
    const verdict = clearability(
      view({ key: 'voice_channel_id', required: false, may_clear: false, value: '42' }),
    )
    expect(verdict.clearable).toBe(false)
    if (!verdict.clearable) expect(verdict.reason).not.toContain('required')
  })

  it('reads the rule off the payload rather than deriving it', () => {
    // The flag is the endpoint's own answer, serialised. A payload that
    // omits it is an API this console does not know, and the safe read of
    // an unknown API is "do not offer the button": explaining a missing
    // control costs a support question, offering one the server refuses
    // costs a 409 in somebody's face.
    const parsed = parseSettings([
      { key: 'timezone', value: 'UTC', may_clear: true },
      { key: 'voice_channel_id', value: '42', may_clear: false },
      { key: 'policy_url', value: 'https://x' },
    ])
    expect(parsed.map((v) => v.may_clear)).toEqual([true, false, false])
  })
})

describe('confirming a change that costs somebody their consent', () => {
  it('demands a confirmation for a key that invalidates consent', () => {
    expect(confirmation(view({ key: 'policy_version', invalidates_consent: true }))).not.toBeNull()
  })

  it('says in plain words that people stop being recorded', () => {
    const prompt = confirmation(view({ key: 'policy_version', invalidates_consent: true }))!
    expect(prompt.consequence.toLowerCase()).toContain('not recorded')
  })

  it('says how each person gets back to being recorded', () => {
    const prompt = confirmation(view({ key: 'policy_version', invalidates_consent: true }))!
    expect(prompt.consequence).toContain('/consent grant')
  })

  it('warns that a meeting in progress is affected too', () => {
    // The packet filter re-checks within the consent cache's five second
    // TTL, so this lands mid-session. Somebody bumping a policy version
    // between meetings should know it does not wait for the next one.
    const prompt = confirmation(view({ key: 'policy_version', invalidates_consent: true }))!
    expect(prompt.consequence.toLowerCase()).toContain('running right now')
  })

  it('asks for nothing when the key does not touch consent', () => {
    expect(confirmation(view({ key: 'timezone' }))).toBeNull()
  })
})

describe('checking a value before it is sent', () => {
  it('trims the surrounding whitespace', () => {
    // A trailing space in a pasted snowflake is invisible and fatal.
    const result = validateValue(view({ key: 'timezone' }), '  Europe/Berlin  ')
    expect(result.ok).toBe(true)
    if (result.ok) expect(result.value).toBe('Europe/Berlin')
  })

  it('accepts a whole number for an integer key', () => {
    expect(validateValue(view({ key: 'idle_timeout_minutes', integer: true }), '15').ok).toBe(true)
  })

  it('accepts an id far beyond the safe integer range', () => {
    // `admin_role_id` is an integer key *and* a Discord snowflake. Anything
    // that validates by parsing a JavaScript number would accept this and
    // hand back a value ending in the wrong digits.
    const result = validateValue(view({ key: 'admin_role_id', integer: true }), '1129384756123456789')
    expect(result.ok).toBe(true)
    if (result.ok) expect(result.value).toBe('1129384756123456789')
  })

  it('rejects a decimal for an integer key', () => {
    expect(validateValue(view({ key: 'max_session_hours', integer: true }), '4.5').ok).toBe(false)
  })

  it('rejects a negative number for an integer key', () => {
    expect(validateValue(view({ key: 'max_session_hours', integer: true }), '-4').ok).toBe(false)
  })

  it('rejects letters for an integer key', () => {
    expect(validateValue(view({ key: 'max_session_hours', integer: true }), 'four').ok).toBe(false)
  })

  it('rejects zero, which the store requires to be positive', () => {
    expect(validateValue(view({ key: 'empty_grace_seconds', integer: true }), '0').ok).toBe(false)
  })

  it('says what is wrong rather than that something is', () => {
    const result = validateValue(view({ key: 'max_session_hours', integer: true }), '4.5')
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.message).toContain('Whole number')
  })

  it('refuses an empty value for a required key', () => {
    const result = validateValue(view({ key: 'policy_url', required: true }), '   ')
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.message.toLowerCase()).toContain('required')
  })

  it('refuses an empty value for an integer key', () => {
    expect(validateValue(view({ key: 'merge_gap_seconds', integer: true }), '').ok).toBe(false)
  })

  it('allows an empty value for optional free text', () => {
    // An empty `transcription_prompt` is a real choice -- it asks Whisper
    // for no vocabulary bias at all -- and is not the same as clearing the
    // key, which restores the default prompt.
    expect(validateValue(view({ key: 'transcription_prompt', default: 'x' }), '').ok).toBe(true)
  })

  it('accepts arbitrary text for a text key', () => {
    expect(validateValue(view({ key: 'document_target' }), 'collection-id').ok).toBe(true)
  })
})

describe('the hints beside a field', () => {
  it('names the default so it can be compared with what is set', () => {
    expect(fieldHints(view({ key: 'timezone', default: 'Europe/Berlin' })).join(' ')).toContain(
      'Europe/Berlin',
    )
  })

  it('says a required key has no default to fall back to', () => {
    expect(fieldHints(view({ key: 'policy_url', required: true })).join(' ')).toContain('no default')
  })

  it('states the integer rule the store actually enforces', () => {
    const hints = fieldHints(view({ key: 'merge_gap_seconds', integer: true, default: '15' })).join(' ')
    expect(hints).toContain('Whole number')
    expect(hints).toContain('greater than zero')
  })

  it('shortens a default too long to sit on one line', () => {
    const long = 'x'.repeat(400)
    expect(fieldHints(view({ key: 'transcription_prompt', default: long })).join(' ').length).toBeLessThan(
      200,
    )
  })
})

describe('shortening a value for display', () => {
  it('leaves a short value alone', () => {
    expect(summariseValue('Europe/Berlin', 40)).toBe('Europe/Berlin')
  })

  it('marks a shortened value as shortened', () => {
    expect(summariseValue('abcdefghij', 5)).toBe('abcde…')
  })
})

describe('choosing the input for a key', () => {
  it('gives an integer key the numeric input', () => {
    expect(inputKind(view({ key: 'merge_gap_seconds', integer: true }))).toBe('integer')
  })

  it('gives a long value room to be read', () => {
    // The default transcription prompt is a two hundred character German
    // sentence. In a one-line field it can only be edited by scrolling.
    expect(inputKind(view({ key: 'transcription_prompt', default: 'x'.repeat(200) }))).toBe('multiline')
  })

  it('gives a value with a line break room too', () => {
    expect(inputKind(view({ key: 'notes', value: 'one\ntwo' }))).toBe('multiline')
  })

  it('keeps a short value on one line', () => {
    expect(inputKind(view({ key: 'timezone', value: 'Europe/Berlin' }))).toBe('text')
  })
})

describe('naming a key for a reader', () => {
  it('unpacks the underscores', () => {
    expect(keyLabel('policy_version')).toBe('Policy version')
  })

  it('keeps the initialisms upper case', () => {
    expect(keyLabel('admin_role_id')).toBe('Admin role ID')
    expect(keyLabel('policy_url')).toBe('Policy URL')
  })

  it('survives a key it has never seen', () => {
    expect(keyLabel('something_entirely_new')).toBe('Something entirely new')
  })
})

describe('the order the keys are shown in', () => {
  it('puts a required key with nothing set at the very top', () => {
    // Those are the ones stopping the guild from being watched at all.
    const ordered = orderSettings([
      view({ key: 'timezone', value: 'UTC' }),
      view({ key: 'policy_url', required: true, value: null }),
    ])
    expect(ordered.map((v) => v.key)).toEqual(['policy_url', 'timezone'])
  })

  it('puts a satisfied required key above an optional one', () => {
    const ordered = orderSettings([
      view({ key: 'audio_retention_days', value: '30' }),
      view({ key: 'policy_url', required: true, value: 'https://x' }),
    ])
    expect(ordered.map((v) => v.key)).toEqual(['policy_url', 'audio_retention_days'])
  })

  it('sorts alphabetically inside a rank, so the list never reshuffles itself', () => {
    const ordered = orderSettings([view({ key: 'timezone' }), view({ key: 'document_target' })])
    expect(ordered.map((v) => v.key)).toEqual(['document_target', 'timezone'])
  })

  it('leaves the list it was given untouched', () => {
    const given = [view({ key: 'timezone' }), view({ key: 'document_target' })]
    orderSettings(given)
    expect(given.map((v) => v.key)).toEqual(['timezone', 'document_target'])
  })
})

describe('the required keys that are still missing', () => {
  it('names a required key with nothing set', () => {
    // Until every one of these has a value the bot does not watch that
    // guild at all, and this page is the only place somebody finds that
    // out before a meeting records nothing.
    expect(
      missingRequired([
        view({ key: 'policy_url', required: true, value: null }),
        view({ key: 'timezone', value: 'UTC' }),
      ]),
    ).toEqual(['policy_url'])
  })

  it('says nothing about a required key that is set', () => {
    expect(missingRequired([view({ key: 'policy_url', required: true, value: 'https://x' })])).toEqual([])
  })

  it('does not count an optional key that happens to be unset', () => {
    expect(missingRequired([view({ key: 'transcription_prompt', value: null })])).toEqual([])
  })
})

describe('turning a failed request into a sentence', () => {
  it('prefers the API’s own reason for a rejected value', () => {
    // The server's validation is the authority. Client-side checking is a
    // convenience and is allowed to be less strict than the store.
    expect(describeError({ status: 400, data: { error: 'not a valid IANA timezone' } })).toContain(
      'not a valid IANA timezone',
    )
  })

  it('reads a reason out of a `detail` field', () => {
    expect(describeError({ status: 400, data: { detail: 'must be positive' } })).toContain(
      'must be positive',
    )
  })

  it('reads a reason sent as a bare string body', () => {
    expect(describeError({ status: 400, data: 'must be positive' })).toContain('must be positive')
  })

  it('still says something useful for a 400 with no reason at all', () => {
    expect(describeError({ status: 400, data: {} }).length).toBeGreaterThan(0)
  })

  it('explains a 403 in terms of the role that grants it', () => {
    expect(describeError({ status: 403 })).toContain('admin_role_id')
  })

  it('explains a 409 as the required key it is', () => {
    expect(describeError({ statusCode: 409 }).toLowerCase()).toContain('required')
  })

  it('explains a 401 as a session that has ended', () => {
    expect(describeError({ status: 401 }).toLowerCase()).toContain('sign in')
  })

  it('explains a 404 as a guild or key the API does not know', () => {
    expect(describeError({ status: 404 }).toLowerCase()).toContain('no longer')
  })

  it('reads the status off a nested response as well', () => {
    expect(describeError({ response: { status: 403 } })).toContain('admin_role_id')
  })

  it('says the request never arrived when there is no status at all', () => {
    expect(describeError(new Error('fetch failed')).toLowerCase()).toContain('could not reach')
  })

  it('names an unexpected status rather than inventing an explanation', () => {
    expect(describeError({ status: 503 })).toContain('503')
  })
})

describe('remembering which guild is being edited', () => {
  it('keeps the guild chosen last time when it is still administered', () => {
    expect(chooseGuild([{ id: '42', name: null }, { id: '43', name: null }], '43')).toBe('43')
  })

  it('falls back to the first when the remembered guild is gone', () => {
    // Administrator status was revoked, or the bot left that server. The
    // stored id must never leave the page editing something invisible.
    expect(chooseGuild([{ id: '42', name: null }], '99')).toBe('42')
  })

  it('chooses nothing when there is nothing to choose', () => {
    expect(chooseGuild([], '42')).toBeNull()
  })

  it('reads back the guild it stored', () => {
    const store = memoryStore()
    expect(writeSelectedGuild(store, '42')).toBe(true)
    expect(readSelectedGuild(store)).toBe('42')
  })

  it('stores the choice under a key of its own, not the sidebar’s', () => {
    const store = memoryStore()
    writeSelectedGuild(store, '42')
    expect(store.getItem(SELECTED_GUILD_KEY)).toBe('42')
  })

  it('reads nothing rather than throwing when site data is blocked', () => {
    const throwing: KeyValueStore = {
      getItem() {
        throw new Error('site data is blocked')
      },
      setItem() {
        throw new Error('site data is blocked')
      },
    }
    expect(readSelectedGuild(throwing)).toBeNull()
    expect(writeSelectedGuild(throwing, '42')).toBe(false)
  })

  it('reads nothing during a server render, where no browser storage exists', () => {
    expect(readSelectedGuild(null)).toBeNull()
  })
})
