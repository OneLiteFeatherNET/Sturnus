/**
 * How a recording is divided, and which part of it a bare link opens.
 *
 * Two of these properties are load-bearing for links that already exist in
 * other people's documents, and neither is visible from a render:
 *
 * - **The plain address stays plain.** `~/utils/uiTabs` drops the query
 *   parameter for the *first* tab, so reordering the list silently turns
 *   `/recordings/4711` into a synonym for a different panel — and every
 *   protocol, chat message and bookmark pointing at that address follows
 *   it.
 * - **No tab is disabled.** A disabled first tab moves the default;
 *   `tabFromQuery` also falls back off a disabled tab, so `?tab=tracks` in
 *   somebody's bookmark would quietly open something else.
 *
 * The rest of the file is the division itself, pinned so that adding a
 * fifth tab is a diff a reviewer is asked about rather than one that
 * arrives with a component.
 */
import { describe, expect, it } from 'vitest'

import {
  RECORDING_DEFAULT_TAB,
  RECORDING_TABS,
  recordingTabQuery,
  recordingTabs,
} from '../app/utils/recordingTabs'
import { TAB_QUERY, queryForTab, tabFromQuery } from '../app/utils/uiTabs'

const TABS = recordingTabs((key) => key)

describe('the four parts of a recording', () => {
  it('are the meeting, the tracks, the transcript and the details', () => {
    // Named rather than counted: a fifth tab is a design decision, and a
    // test that only counted them would let one arrive unremarked.
    expect(RECORDING_TABS.map((tab) => tab.id)).toEqual([
      'meeting',
      'tracks',
      'transcript',
      'details',
    ])
  })

  it('has no tab for metadata, because the facts are elsewhere', () => {
    // The session's own facts are in the header above the bar; the audio
    // measurements are in the row of the track they describe. Neither is a
    // tab, and this is the check that fails if one comes back.
    expect(RECORDING_TABS.map((tab) => tab.id)).not.toContain('metadata')
  })

  it('has no tab for the re-queue panel, which most readers cannot see', () => {
    // It lives on `details`. A tab that renders nothing for everybody who
    // does not administer the guild is an empty tab for almost everybody,
    // and it could only appear after a round trip — which would move every
    // tab beside it and change what a bare address opens.
    expect(RECORDING_TABS.map((tab) => tab.id)).not.toContain('queue')
  })

  it('carries a key for every label rather than a sentence', () => {
    // `i18n/README.md`: a module under `app/utils` returns keys and the
    // template calls `$t`. A label written out here is a German page with
    // an English hole in it.
    for (const tab of RECORDING_TABS) {
      expect(tab.labelKey).toMatch(/^recordings\.tab[A-Z]/)
    }
  })

  it('translates its labels through whatever the page hands it', () => {
    expect(recordingTabs((key) => `<${key}>`)[0]).toEqual({
      id: 'meeting',
      label: '<recordings.tabMeeting>',
    })
  })

  it('leaves every tab openable', () => {
    // `tabFromQuery` falls back off a disabled tab and `moveTab` skips
    // one. Both would redirect a link somebody had already shared.
    for (const tab of TABS) expect(tab.disabled).toBeUndefined()
  })
})

describe('what a bare recording address opens', () => {
  it('is the meeting', () => {
    // Somebody arriving from the list has already been shown the date, the
    // channel, who was in it and whether a protocol exists — everything
    // except the audio. Somebody arriving from a protocol has read the
    // words. Both came for the recording.
    expect(RECORDING_DEFAULT_TAB).toBe('meeting')
  })

  it('is the tab `uiTabs` would choose with nothing in the address', () => {
    // The default is the first tab, and it is decided in one place. Two
    // statements of it would be two things to keep in step.
    expect(tabFromQuery(undefined, TABS)).toBe(RECORDING_DEFAULT_TAB)
    expect(RECORDING_TABS[0]?.id).toBe(RECORDING_DEFAULT_TAB)
  })

  it('is what an unreadable tab name falls back to', () => {
    // A typo in an address bar, or a bookmark from before a rename. An
    // error page would turn a slip into a dead end.
    expect(tabFromQuery('nonsense', TABS)).toBe('meeting')
    expect(tabFromQuery(['transcript', 'tracks'], TABS)).toBe('transcript')
  })
})

describe('a link from one panel to another', () => {
  it('names the tab it points at', () => {
    expect(recordingTabQuery({}, 'transcript')).toEqual({ [TAB_QUERY]: 'transcript' })
  })

  it('leaves the default tab out of the address', () => {
    // So that `/recordings/4711` never becomes a synonym for
    // `/recordings/4711?tab=meeting`, and the canonical address of a
    // recording stays the one people paste.
    expect(recordingTabQuery({ [TAB_QUERY]: 'tracks' }, 'meeting')).toEqual({})
  })

  it('keeps everything else in the address', () => {
    // The audio tabs point at the transcript, and a link that dropped the
    // rest of the query would move the reader somewhere they did not ask
    // to be.
    expect(recordingTabQuery({ from: '2026-08-01' }, 'tracks')).toEqual({
      from: '2026-08-01',
      [TAB_QUERY]: 'tracks',
    })
  })

  it('says exactly what `uiTabs` would say, given the same list', () => {
    // The wrapper exists so a page does not have to invent labels to ask
    // an arithmetic question. It must not become a second answer.
    expect(recordingTabQuery({ page: '2' }, 'details')).toEqual(
      queryForTab({ page: '2' }, TABS, 'details'),
    )
  })
})
