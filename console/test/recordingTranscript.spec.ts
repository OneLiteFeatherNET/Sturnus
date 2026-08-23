/**
 * Whether the three empty transcripts read as three different things.
 *
 * `~/utils/transcript` decides which of them a payload is, and is tested
 * there. What a module cannot show is that the sentence it chose reaches
 * the screen: a template with the heading wired up and the detail left on
 * the floor renders one word of the answer, and a template that fell
 * through to a single "nothing here" would pass every module test in the
 * suite. That failure is the reason `pending_tracks` is on the endpoint at
 * all, so it is worth a render.
 *
 * The locale files are the real ones, so a key that does not exist renders
 * as itself and fails here rather than in front of a reader.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { mount } from '@vue/test-utils'
import { computed } from 'vue'
import { createI18n, useI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import RecordingTranscript from '../app/components/RecordingTranscript.vue'
import { useSay } from '../app/composables/useSay'
import type { SessionTranscript } from '../app/utils/transcript'

function load(locale: string) {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

function transcript(over: Partial<SessionTranscript> = {}): SessionTranscript {
  return {
    session_id: '4711',
    started_at: '2026-08-21T14:05:00+00:00',
    ended_at: '2026-08-21T15:05:00+00:00',
    audio_available: true,
    pending_tracks: 0,
    participants: [
      {
        discord_user_id: '100',
        display_name: 'anna',
        external_user_id: null,
        external_display_name: null,
      },
    ],
    blocks: [
      {
        discord_user_id: '100',
        display_name: 'anna',
        started_at: '2026-08-21T14:07:30+00:00',
        text: 'wir sind uns einig',
      },
    ],
    ...over,
  }
}

function render(over: Partial<SessionTranscript> = {}, locale = 'en') {
  vi.stubGlobal('computed', computed)
  vi.stubGlobal('useI18n', () => ({
    ...useI18n(),
    locales: computed(() => [
      { code: 'en', language: 'en-GB' },
      { code: 'de', language: 'de-DE' },
    ]),
  }))
  vi.stubGlobal('useSay', useSay)
  const i18n = createI18n({
    legacy: false,
    locale,
    fallbackLocale: 'en',
    messages: { en: load('en'), de: load('de') },
  })
  return mount(RecordingTranscript, {
    props: { transcript: transcript(over) },
    global: { plugins: [i18n] },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('a transcript with words in it', () => {
  it('renders one turn per block, in order', () => {
    const view = render()
    expect(view.findAll('ol > li')).toHaveLength(1)
    expect(view.text()).toContain('wir sind uns einig')
  })

  it('anchors each turn to the clock the transport runs on', () => {
    // Two minutes thirty into the meeting. Without it the transcript is a
    // wall of prose with no way back into the recording it came from.
    expect(render().get('ol > li').text()).toContain('2:30')
  })

  it('says nothing about a queue when there is nothing left in it', () => {
    expect(render().text()).not.toContain('still being transcribed')
  })

  it('warns that it is not all of it when speakers are still in the queue', () => {
    // A partial transcript reads as finished, and somebody concluding
    // from it that a colleague said nothing has been misled.
    expect(render({ pending_tracks: 2 }).text()).toContain('still being transcribed')
  })

  it('says who is written under another name in the protocol', () => {
    // Somebody holding both documents otherwise finds two names for one
    // colleague and no explanation.
    const view = render({
      participants: [
        {
          discord_user_id: '100',
          display_name: 'anna',
          external_user_id: 'u-1',
          external_display_name: 'Anna A.',
        },
      ],
    })
    expect(view.text()).toContain('anna is written as Anna A.')
  })

  it('leaves the roster out when nobody is named twice', () => {
    // Four lines between the reader and the words, answering a question
    // nobody asked.
    expect(render().text()).not.toContain('In the protocol:')
  })
})

describe('a transcript with no words in it', () => {
  it('says the meeting has not finished, when it has not', () => {
    // Its jobs are not enqueued until it closes. Nothing failed and
    // nothing is waiting; there is simply nothing to assemble yet.
    const view = render({ ended_at: null, blocks: [], pending_tracks: 3 })
    expect(view.text()).toContain('still being recorded')
    expect(view.text()).toContain('Transcription starts when the session closes')
    expect(view.findAll('ol > li')).toHaveLength(0)
  })

  it('says the words are coming, when speakers are still in the queue', () => {
    const view = render({ blocks: [], pending_tracks: 2 })
    expect(view.text()).toContain('Still being transcribed')
    expect(view.text()).toContain('2 speakers have not been through the transcriber')
  })

  it('counts one speaker in the singular', () => {
    // The plural branch is the locale file's to choose, and a sentence
    // reading "1 speakers" is the console admitting nobody looked.
    expect(render({ blocks: [], pending_tracks: 1 }).text()).toContain(
      'One speaker has not been through the transcriber',
    )
  })

  it('says nobody spoke, when the queue is empty and nothing came out', () => {
    // A finished result rather than a wait. The distinction the two extra
    // fields on the endpoint exist for.
    const view = render({ blocks: [], pending_tracks: 0 })
    expect(view.text()).toContain('Nothing was said that could be heard')
    expect(view.text()).not.toContain('Still being transcribed')
  })

  it('never meets all three with the same sentence', () => {
    const recording = render({ ended_at: null, blocks: [], pending_tracks: 3 }).text()
    const waiting = render({ blocks: [], pending_tracks: 2 }).text()
    const silent = render({ blocks: [], pending_tracks: 0 }).text()
    expect(new Set([recording, waiting, silent]).size).toBe(3)
  })
})

describe('the transcript in German', () => {
  it('is German, and not the key nor the English', () => {
    const view = render({ blocks: [], pending_tracks: 0 }, 'de')
    expect(view.text()).toContain('Es wurde nichts Hörbares gesagt')
    expect(view.text()).not.toMatch(/recordings\.[a-zA-Z]+/)
  })
})
