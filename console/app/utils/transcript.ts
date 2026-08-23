/**
 * A meeting's words, and the four different reasons there might be none.
 *
 * The transcript endpoint answers 200 with an empty `blocks` in three
 * entirely separate situations, and a tab that met all three with "nothing
 * here" is a tab people report as broken. The API carries the two fields
 * that tell them apart on purpose (`sturnus.console.statistics` says so in
 * its own words), and this module is where that becomes a sentence:
 *
 * - **The session has not ended.** Its jobs are not enqueued until it
 *   closes, so there is nothing to assemble yet and never was.
 * - **`pending_tracks > 0`.** Speakers are still going through the
 *   transcriber. The words are coming.
 * - **`pending_tracks === 0`.** Every consenting speaker has been through
 *   it and none of it produced words. Nobody said anything the engine
 *   could hear.
 *
 * The fourth is not an empty transcript at all and is the one most worth
 * saying out loud: **`audio_available === false` means the recordings have
 * been erased and these words survived them.** The retention window is
 * about the audio and not about the minutes. Two tabs of this page are
 * audio and would otherwise render as a dead player and a row of 404s;
 * they ask {@link transcriptAudioGone} instead and say what happened.
 *
 * `transcriptAudioErased` is strict about that for two reasons. A
 * transcript that has not arrived, or one whose request failed, is
 * *unknown* — and a page that treated unknown as gone would tell somebody
 * their recording had been deleted because a request timed out. And a
 * session nobody consented to answers `audio_available: false` as well,
 * because the flag counts undeleted jobs and there are none; that is the
 * same answer to the endpoint and entirely different news to a reader.
 *
 * Every export starts with `transcript` or `TRANSCRIPT`: Nuxt auto-imports
 * every export under `app/utils` into one namespace, and a collision there
 * is resolved silently by file order.
 */
import type { Message } from './message'

/**
 * One person as the transcript names them.
 *
 * Both external fields are carried and both are `null` for somebody who
 * never linked an account. They are what the published protocol prints, so
 * a transcript tab that could not show the same name as the document would
 * be showing a different meeting.
 */
export interface TranscriptSpeaker {
  discord_user_id: string
  display_name: string
  external_user_id: string | null
  external_display_name: string | null
}

/** One speaker's uninterrupted turn. `started_at` and not a duration: a
 *  block is anchored to a moment in the meeting, which is what lets it be
 *  lined up against the clock the transport runs on. */
export interface TranscriptBlock {
  discord_user_id: string
  display_name: string
  started_at: string
  text: string
}

export interface SessionTranscript {
  session_id: string
  started_at: string
  ended_at: string | null
  /** `false` once retention has swept every one of this session's
   *  recordings. The blocks below survive that. */
  audio_available: boolean
  /** How many speakers are still waiting to be transcribed. What tells
   *  "nobody spoke" from "not decoded yet", both of which are no blocks. */
  pending_tracks: number
  participants: TranscriptSpeaker[]
  blocks: TranscriptBlock[]
}

/** Where a session's words are read from. The id is escaped: a string
 *  allowed to contain a slash is a string allowed to address a different
 *  endpoint. */
export function transcriptPath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/transcript`
}

/**
 * Whether this session's audio has been erased, as opposed to never having
 * existed or never having been asked about.
 *
 * Three things have to be true, and each of them rules out a wrong
 * sentence the page would otherwise render:
 *
 * - **The transcript has arrived.** `null` is a request that failed or has
 *   not landed, and neither is evidence about anybody's recordings.
 *   Rendering "the audio has been erased" because a fetch went wrong would
 *   be the console reporting data loss that did not happen, which is the
 *   single worst thing this page could say.
 * - **The endpoint said `false` and not merely something falsy.**
 * - **There were tracks in the first place.** `audio_available` is
 *   computed as "any job whose audio is not deleted", so a session nobody
 *   consented to — no jobs at all — answers `false` as well. The API's own
 *   docstring says the two are the same answer *to it*; they are not the
 *   same news to a reader. "Retention deleted your recording" and "nobody
 *   in this meeting had consented before it began" are different
 *   sentences, and only one of them is about something that was lost.
 */
export function transcriptAudioErased(
  transcript: SessionTranscript | null | undefined,
  recordedTracks: number,
): boolean {
  if (recordedTracks <= 0) return false
  return transcript?.audio_available === false
}

/**
 * Why there are no words, or `null` when there are some.
 *
 * A heading and a detail rather than one sentence, because the three
 * answers are three different pieces of news: one is a wait, one is a
 * finished result, and one is a meeting that has not happened yet.
 */
export interface TranscriptEmptiness {
  heading: Message
  detail: Message
}

export function transcriptEmpty(transcript: SessionTranscript): TranscriptEmptiness | null {
  if (transcript.blocks.length > 0) return null
  if (transcript.ended_at === null) {
    return {
      heading: { key: 'recordings.transcriptRecordingHeading' },
      detail: { key: 'recordings.transcriptRecordingDetail' },
    }
  }
  if (transcript.pending_tracks > 0) {
    return {
      heading: { key: 'recordings.transcriptPendingHeading' },
      detail: {
        key: 'recordings.transcriptPendingDetail',
        params: { count: transcript.pending_tracks },
      },
    }
  }
  return {
    heading: { key: 'recordings.transcriptSilentHeading' },
    detail: { key: 'recordings.transcriptSilentDetail' },
  }
}

/**
 * That this is not all of it, when it is not all of it.
 *
 * A transcript with words in it *and* speakers still in the queue reads as
 * finished, and somebody who concludes from it that a colleague said
 * nothing has been misled by an omission the page could have mentioned.
 * `null` whenever the transcript is whole, or whenever it is empty — an
 * empty one is already saying this at the top of its own state.
 */
export function transcriptPartial(transcript: SessionTranscript): Message | null {
  if (transcript.blocks.length === 0) return null
  if (transcript.pending_tracks <= 0) return null
  return {
    key: 'recordings.transcriptPartial',
    params: { count: transcript.pending_tracks },
  }
}

function instantOf(value: string | null): number | null {
  if (!value) return null
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? null : parsed
}

/**
 * How far into the meeting a block was spoken, in seconds, or `null`.
 *
 * The same clock the transport runs on, so a reader can take a number off
 * the transcript and find that moment in the player. Clamped at zero: a
 * block cannot begin before the session it is in, and a negative offset is
 * a clock skew rendered as a fact.
 *
 * `null` rather than zero when either instant is unreadable — "the very
 * beginning" is a claim, and this module does not make claims it cannot
 * measure.
 */
export function transcriptOffset(
  transcript: SessionTranscript,
  block: TranscriptBlock,
): number | null {
  const start = instantOf(transcript.started_at)
  const at = instantOf(block.started_at)
  if (start === null || at === null) return null
  return Math.max(0, Math.floor((at - start) / 1000))
}

/**
 * The name the protocol prints for each speaker who has linked an account.
 *
 * Keyed by Discord id, and only the people who have one: a map entry
 * holding `null` would be a speaker the roster has to check twice. This
 * exists so the transcript can say "anna, written as Anna A. in the
 * protocol" — without it, somebody comparing the two documents finds two
 * different names for one colleague and no explanation.
 */
export function transcriptExternalNames(transcript: SessionTranscript): Record<string, string> {
  const named: Record<string, string> = {}
  for (const speaker of transcript.participants) {
    const external = speaker.external_display_name?.trim()
    if (external) named[speaker.discord_user_id] = external
  }
  return named
}

/**
 * How a speaker is introduced at the top of the transcript.
 *
 * A name is somebody's own word and is never translated, so the plain case
 * is the string itself; a speaker with a second name needs a sentence to
 * join the two, and that is a key.
 */
export function transcriptAttribution(
  speaker: TranscriptSpeaker,
  external: string | null | undefined,
): string | Message {
  const name = speaker.display_name.trim() || speaker.discord_user_id
  const other = external?.trim()
  if (!other || other === name) return name
  return { key: 'recordings.transcriptAlsoNamed', params: { name, external: other } }
}
