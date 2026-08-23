/**
 * Whether a row of the recordings list is the thing this page was
 * rebuilt to be.
 *
 * The decisions are all in `~/utils/recordings` and tested there: what an
 * unnamed channel reads as, how many speakers are named, that the day and
 * the clock are two different strings. What a render has to prove is the
 * half a pure module cannot reach — that the row is *one link* and not a
 * card with an "Open" button in it, that nothing on it plays anything,
 * and that the decisions actually arrive on screen where the module put
 * them.
 *
 * The last of those is the one worth writing down. `channelNaming` can be
 * right about the id being subordinate and the template can still set the
 * id as the heading; that failure is exactly the one this page is being
 * rebuilt to fix, and only a render sees it.
 *
 * The locale files are the real ones, for the reason `uiComponents.spec`
 * gives: a template asking for `recordings.hasProtcol` renders the key at
 * somebody, and nothing but a render catches it.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { mount, type VueWrapper } from '@vue/test-utils'
import { computed, ref } from 'vue'
import { createI18n, useI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import RecordingRow from '../app/components/RecordingRow.vue'
import { useSay } from '../app/composables/useSay'
import type { RecordedSession, SessionTrack } from '../app/utils/recordings'

function load(locale: string) {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

function track(over: Partial<SessionTrack> = {}): SessionTrack {
  return {
    discord_user_id: '308000000000000001',
    display_name: 'Ada',
    audio_seconds: 600,
    speech_seconds: 150,
    segment_count: 12,
    ...over,
  }
}

function session(over: Partial<RecordedSession> = {}): RecordedSession {
  return {
    id: '7f2b',
    started_at: '2026-08-21T14:05:09Z',
    ended_at: '2026-08-21T15:05:09Z',
    duration_seconds: 3600,
    channel_id: '987000000000000002',
    channel_name: 'standup',
    document_url: 'https://outline.example/doc/7f2b',
    other_participants: [{ discord_user_id: '4', display_name: 'Bo' }],
    tracks: [track()],
    tags: ['retro'],
    ...over,
  }
}

/** Nuxt auto-imports these; vitest runs without Nuxt. */
function stubAutoImports() {
  vi.stubGlobal('ref', ref)
  vi.stubGlobal('computed', computed)
  vi.stubGlobal('useI18n', () => ({
    ...useI18n(),
    locales: computed(() => [{ code: 'en', language: 'en-GB' }]),
  }))
  vi.stubGlobal('useSay', useSay)
}

function render(over: Partial<RecordedSession> = {}) {
  stubAutoImports()
  const i18n = createI18n({
    legacy: false,
    locale: 'en',
    fallbackLocale: 'en',
    messages: { en: load('en'), de: load('de') },
  })
  return mount(RecordingRow, {
    props: { session: session(over), timeZone: 'UTC' },
    global: {
      plugins: [i18n],
      // A real anchor, because what is being checked is that the row *is*
      // one — a stub that rendered a `div` would hide the whole point.
      stubs: { NuxtLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } },
    },
  })
}

const linksOf = (row: VueWrapper) => row.findAll('a')

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('a row of the recordings list', () => {
  it('is one link, and it is the whole row', () => {
    // Not a card with an Open button on it. One link is one stop for the
    // keyboard, a hit target the width of the list, and the only shape a
    // row can have if the protocol on it is not to be a link inside a
    // link — which is not something a browser can express.
    const row = render()
    expect(linksOf(row)).toHaveLength(1)
    expect(linksOf(row)[0]!.attributes('href')).toBe('/recordings/7f2b')
    expect(row.get('a').text()).toContain('#standup')
  })

  it('plays nothing, and offers nothing to expand', () => {
    // The whole cut. A row used to mount a multi-track player in place;
    // audio, spectrograms and the protocol link all live on the
    // recording's own page now.
    const row = render()
    expect(row.find('audio').exists()).toBe(false)
    expect(row.find('[aria-expanded]').exists()).toBe(false)
    expect(row.findAll('button')).toHaveLength(0)
  })

  it('heads the row with the channel and not with the date', () => {
    const row = render()
    expect(row.get('h2').text()).toContain('#standup')
  })

  it('sets the day apart from the clock, and machine-readably', () => {
    // The left edge of the list is scanned for the day. `2026-08-21
    // 14:05` as one string makes the eye read eleven characters to find
    // the two it wants.
    const stamp = render().get('time')
    expect(stamp.attributes('datetime')).toBe('2026-08-21T14:05:09Z')
    expect(stamp.findAll('span').map((span) => span.text())).toEqual(['2026-08-21', '14:05'])
  })

  it('never heads a row with a Discord id', () => {
    // The complaint this page was rebuilt for. `channelLabel` answers
    // "Channel 987000000000000002" and eighteen digits set as a heading
    // read as the meeting's name.
    const row = render({ channel_name: null })
    expect(row.get('h2').text()).toContain('Unnamed channel')
    expect(row.get('h2').text()).not.toContain('987000000000000002')
  })

  it('keeps the id underneath, where it is plainly not a name', () => {
    const row = render({ channel_name: null })
    expect(row.text()).toContain('987000000000000002')
  })

  it('says in words whether a protocol exists, rather than only in a colour', () => {
    expect(render().text()).toContain('Protocol')
    expect(render({ document_url: null }).text()).toContain('No protocol')
  })

  it('names who was recorded, and says so when nobody was', () => {
    expect(render().text()).toContain('Ada')
    expect(render({ tracks: [] }).text()).toContain('nobody consented')
  })

  it('does not enumerate the people who were in the channel unrecorded', () => {
    // A fact about one session rather than a way of telling two apart. It
    // is on the recording's own page, where there is room to say why they
    // have no audio.
    expect(render().text()).not.toContain('Bo')
  })

  it("carries the reader's own labels, because they are what the field above filters by", () => {
    expect(render().text()).toContain('retro')
  })

  it('marks a session that is still being recorded', () => {
    expect(render({ ended_at: null, duration_seconds: null }).text()).toContain('Recording now')
  })

  it('says a length it does not know rather than printing a plausible zero', () => {
    const row = render({ ended_at: null, duration_seconds: null })
    expect(row.text()).toContain('length unknown')
  })
})
