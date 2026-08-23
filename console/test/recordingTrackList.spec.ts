/**
 * What is left of a track once its audio has been swept.
 *
 * The notice above the tab bar promises that everything written from a
 * recording survives the recording: the transcript, the protocol, and the
 * measurements. A list that dropped the measurements along with the audio
 * would make that sentence false on the one page it is displayed on — and
 * a list that kept the `<audio>` elements would give an eight-speaker
 * meeting eight failing players and no explanation. Both are one prop, and
 * only a render can tell which side of it a template is on.
 *
 * The other property here is `null` never becoming `0`. `~/utils/recordings`
 * proves the module returns nothing for a measurement nobody took; this
 * proves the em dash reaches the screen rather than being swallowed by a
 * `v-if` that treats an absence as a falsy number.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { mount } from '@vue/test-utils'
import { computed, ref } from 'vue'
import { createI18n, useI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import RecordingTrackList from '../app/components/RecordingTrackList.vue'
import { useSay } from '../app/composables/useSay'
import type { RecordedSession, SessionTrack } from '../app/utils/recordings'

function load(locale: string) {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

function track(over: Partial<SessionTrack> = {}): SessionTrack {
  return {
    discord_user_id: '100',
    display_name: 'anna',
    audio_seconds: 600,
    speech_seconds: 150,
    segment_count: 12,
    sample_rate: 48000,
    channels: 2,
    stored_bytes: 12_440_000,
    ...over,
  }
}

function session(tracks: SessionTrack[]): RecordedSession {
  return {
    id: '4711',
    started_at: '2026-08-21T14:05:00+00:00',
    ended_at: '2026-08-21T15:05:00+00:00',
    duration_seconds: 3600,
    channel_id: '9870',
    channel_name: 'standup',
    title: null,
    description: null,
    document_url: null,
    other_participants: [],
    tracks,
    tags: [],
  }
}

function render(tracks: SessionTrack[], playable = true) {
  vi.stubGlobal('ref', ref)
  vi.stubGlobal('computed', computed)
  vi.stubGlobal('useRuntimeConfig', () => ({ public: { apiBase: '/api' } }))
  vi.stubGlobal('useI18n', () => ({
    ...useI18n(),
    locales: computed(() => [{ code: 'en', language: 'en-GB' }]),
  }))
  vi.stubGlobal('useSay', useSay)
  const i18n = createI18n({
    legacy: false,
    locale: 'en',
    fallbackLocale: 'en',
    messages: { en: load('en'), de: load('de') },
  })
  return mount(RecordingTrackList, {
    props: { session: session(tracks), playable },
    global: {
      plugins: [i18n],
      // The spectrogram fetches on mount and is a component of its own,
      // tested elsewhere. What matters here is whether one is asked for.
      stubs: { TrackSpectrogram: { template: '<div data-spectrogram />' } },
    },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('a track that still has its audio', () => {
  it('gets a player and a picture', () => {
    const list = render([track()])
    expect(list.findAll('audio')).toHaveLength(1)
    expect(list.findAll('[data-spectrogram]')).toHaveLength(1)
  })

  it('names its player, so eight in a row are eight different speakers', () => {
    expect(render([track()]).get('audio').attributes('aria-label')).toBe('anna on their own')
  })

  it('fetches nothing until somebody presses play', () => {
    expect(render([track()]).get('audio').attributes('preload')).toBe('none')
  })

  it('says what the file is, beside the audio it describes', () => {
    const text = render([track()]).text()
    expect(text).toContain('48 kHz')
    expect(text).toContain('2 channels')
    expect(text).toContain('12.4 MB')
  })
})

describe('a track whose audio has been swept', () => {
  it('keeps the measurements, because the notice promises it does', () => {
    const list = render([track()], false)
    expect(list.text()).toContain('48 kHz')
    expect(list.text()).toContain('12.4 MB')
  })

  it('mounts no player for a file that is not there', () => {
    // Eight failing `<audio>` elements is what "looking broken" means.
    const list = render([track()], false)
    expect(list.findAll('audio')).toHaveLength(0)
    expect(list.findAll('[data-spectrogram]')).toHaveLength(0)
  })
})

describe('a measurement nobody took', () => {
  it('reads as an absence and never as a zero', () => {
    // Every track written before migration 0013 has all three null, and
    // `0 kB` is a claim about somebody's recording this console cannot
    // support.
    const list = render([track({ sample_rate: null, channels: null, stored_bytes: null })])
    expect(list.text()).toContain('—')
    expect(list.text()).not.toContain('0 kB')
    expect(list.text()).not.toContain('0 kHz')
  })

  it('leaves the three labels in place, so the gap is a gap', () => {
    const list = render([track({ sample_rate: null, channels: null, stored_bytes: null })])
    expect(list.findAll('dt')).toHaveLength(3)
    expect(list.text()).toContain('Sample rate')
  })
})
