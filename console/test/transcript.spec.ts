/**
 * A meeting's words, and the four different reasons there might be none.
 *
 * Three of them are an empty `blocks` and they are not the same news: one
 * is a wait, one is a finished result, and one is a meeting that has not
 * happened yet. A tab that met all three with "nothing here" is a tab
 * people report as broken — which is why the endpoint carries
 * `pending_tracks` at all, and this file is where that field earns its
 * place.
 *
 * The fourth is not an empty transcript: `audio_available: false` is the
 * retention sweep having taken the recordings and left the minutes. Two
 * other tabs of the recording page depend on it, and the strictness of
 * `transcriptAudioGone` is the property that keeps a failed request from
 * being reported to somebody as data loss.
 */
import { describe, expect, it } from 'vitest'

import {
  transcriptAttribution,
  transcriptAudioErased,
  transcriptEmpty,
  transcriptExternalNames,
  transcriptOffset,
  transcriptPartial,
  transcriptPath,
  type SessionTranscript,
  type TranscriptBlock,
  type TranscriptSpeaker,
} from '../app/utils/transcript'

function speaker(over: Partial<TranscriptSpeaker> = {}): TranscriptSpeaker {
  return {
    discord_user_id: '100',
    display_name: 'anna',
    external_user_id: null,
    external_display_name: null,
    ...over,
  }
}

function block(over: Partial<TranscriptBlock> = {}): TranscriptBlock {
  return {
    discord_user_id: '100',
    display_name: 'anna',
    started_at: '2026-08-21T14:05:09+00:00',
    text: 'wir sind uns einig',
    ...over,
  }
}

function transcript(over: Partial<SessionTranscript> = {}): SessionTranscript {
  return {
    session_id: '4711',
    started_at: '2026-08-21T14:05:00+00:00',
    ended_at: '2026-08-21T15:05:00+00:00',
    audio_available: true,
    pending_tracks: 0,
    participants: [speaker()],
    blocks: [block()],
    ...over,
  }
}

describe('whether the recordings have been erased', () => {
  it('is true only when the endpoint said so', () => {
    expect(transcriptAudioErased(transcript({ audio_available: false }), 2)).toBe(true)
    expect(transcriptAudioErased(transcript({ audio_available: true }), 2)).toBe(false)
  })

  it('is false for a transcript that has not arrived', () => {
    // The worst sentence this page could render is "your audio has been
    // deleted" because a request timed out. Unknown is not gone.
    expect(transcriptAudioErased(null, 2)).toBe(false)
    expect(transcriptAudioErased(undefined, 2)).toBe(false)
  })

  it('is false for a session nobody consented to', () => {
    // The flag counts jobs whose audio is not deleted, so a session with
    // no jobs answers `false` as well. That is the same answer to the
    // endpoint and entirely different news to a reader: "retention
    // deleted your recording" against "nobody in this meeting had
    // consented before it began", and only one of them is a loss.
    expect(transcriptAudioErased(transcript({ audio_available: false }), 0)).toBe(false)
  })
})

describe('why there are no words', () => {
  it('is nothing at all when there are some', () => {
    expect(transcriptEmpty(transcript())).toBeNull()
  })

  it('is "still being recorded" for a session with no end', () => {
    // Its jobs are not enqueued until it closes, so there is nothing to
    // assemble and never was. This is a different answer from a queue that
    // is still running, and both arrive as zero blocks.
    const open = transcript({ ended_at: null, blocks: [], pending_tracks: 3 })
    expect(transcriptEmpty(open)?.heading).toEqual({
      key: 'recordings.transcriptRecordingHeading',
    })
  })

  it('is "still being transcribed" while speakers are in the queue', () => {
    const waiting = transcript({ blocks: [], pending_tracks: 2 })
    expect(transcriptEmpty(waiting)).toEqual({
      heading: { key: 'recordings.transcriptPendingHeading' },
      detail: { key: 'recordings.transcriptPendingDetail', params: { count: 2 } },
    })
  })

  it('counts through a param, so each language picks its own plural', () => {
    // `i18n/README.md`: never an `if` in a module. One speaker and five
    // are one sentence with a hole in it.
    const one = transcriptEmpty(transcript({ blocks: [], pending_tracks: 1 }))
    expect(one?.detail.params?.count).toBe(1)
  })

  it('is "nobody said anything" once the queue is empty', () => {
    // Every consenting speaker has been through the transcriber and none
    // of it produced words. A finished result, not a wait.
    expect(transcriptEmpty(transcript({ blocks: [], pending_tracks: 0 }))).toEqual({
      heading: { key: 'recordings.transcriptSilentHeading' },
      detail: { key: 'recordings.transcriptSilentDetail' },
    })
  })
})

describe('a transcript that is not all of it', () => {
  it('says so when it has words and speakers still in the queue', () => {
    // It reads as finished, and somebody concluding from it that a
    // colleague said nothing has been misled by an omission.
    expect(transcriptPartial(transcript({ pending_tracks: 1 }))).toEqual({
      key: 'recordings.transcriptPartial',
      params: { count: 1 },
    })
  })

  it('says nothing when the transcript is whole', () => {
    expect(transcriptPartial(transcript({ pending_tracks: 0 }))).toBeNull()
  })

  it('says nothing when there are no words at all', () => {
    // An empty transcript is already explaining itself at the top of its
    // own state; a second sentence saying the same thing is noise.
    expect(transcriptPartial(transcript({ blocks: [], pending_tracks: 2 }))).toBeNull()
  })
})

describe('how far into the meeting a turn was spoken', () => {
  it('is measured from the start of the session', () => {
    // The same clock the transport runs on, so a number taken off the
    // transcript can be found in the audio.
    expect(transcriptOffset(transcript(), block({ started_at: '2026-08-21T14:07:30+00:00' }))).toBe(
      150,
    )
  })

  it('never runs before the meeting it is in', () => {
    // A negative offset is clock skew rendered as a fact.
    expect(transcriptOffset(transcript(), block({ started_at: '2026-08-21T14:00:00+00:00' }))).toBe(
      0,
    )
  })

  it('is nothing at all when either instant is unreadable', () => {
    // "The very beginning" is a claim, and this module does not make
    // claims it cannot measure.
    expect(transcriptOffset(transcript(), block({ started_at: 'not a date' }))).toBeNull()
    expect(
      transcriptOffset(transcript({ started_at: 'not a date' }), block()),
    ).toBeNull()
  })
})

describe('who a block is attributed to', () => {
  it('collects the name the protocol prints, for the people who have one', () => {
    const named = transcriptExternalNames(
      transcript({
        participants: [
          speaker({ discord_user_id: '100', external_display_name: 'Anna A.' }),
          speaker({ discord_user_id: '200', display_name: 'bo' }),
        ],
      }),
    )
    expect(named).toEqual({ '100': 'Anna A.' })
  })

  it('keeps nobody out of the map twice', () => {
    // An entry holding `null` would be a speaker the roster has to check
    // again before rendering.
    const named = transcriptExternalNames(
      transcript({ participants: [speaker({ external_display_name: '   ' })] }),
    )
    expect(named).toEqual({})
  })

  it('is the speaker own name when there is nothing else to say', () => {
    // A name is somebody's own word and is never translated, so the plain
    // case is the string itself rather than a key.
    expect(transcriptAttribution(speaker(), null)).toBe('anna')
  })

  it('joins the two names with a sentence when they differ', () => {
    expect(transcriptAttribution(speaker(), 'Anna A.')).toEqual({
      key: 'recordings.transcriptAlsoNamed',
      params: { name: 'anna', external: 'Anna A.' },
    })
  })

  it('says one name once when both are the same', () => {
    expect(transcriptAttribution(speaker({ display_name: 'Anna A.' }), 'Anna A.')).toBe('Anna A.')
  })

  it('falls back to the id for a speaker with no name left', () => {
    // Somebody who has left the guild has no display name to look up, and
    // a block attributed to an empty string is a block nobody can place.
    expect(transcriptAttribution(speaker({ display_name: '  ' }), null)).toBe('100')
  })
})

describe('where the words are read from', () => {
  it('is under the session they belong to', () => {
    expect(transcriptPath('4711')).toBe('/sessions/4711/transcript')
  })

  it('escapes the id, because a slash in one addresses a different endpoint', () => {
    expect(transcriptPath('4711/../queue')).toBe('/sessions/4711%2F..%2Fqueue/transcript')
  })
})
