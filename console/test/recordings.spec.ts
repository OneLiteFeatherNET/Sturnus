/**
 * How a session and its tracks are put into words.
 *
 * The formatting lives in a module for the same reason the transport does:
 * the distinction that matters most here -- a measurement that is zero
 * against one that was never taken -- is a decision, and a decision
 * embedded in a template can only be checked by rendering one.
 */
import { describe, expect, it } from 'vitest'

import {
  audioUrl,
  channelLabel,
  decodeMagnitudes,
  formatCount,
  formatMeasurement,
  formatSeconds,
  formatShare,
  formatTimestamp,
  hasProtocol,
  isInProgress,
  recordingPath,
  sessionLength,
  spectrogramUrl,
  speechShare,
  trackCoverage,
  trackLabel,
  type RecordedSession,
  type SessionTrack,
  type SpectrogramResponse,
} from '../app/utils/recordings'

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
    document_url: 'https://outline.example/doc/standup-abc',
    other_participants: [],
    tracks: [],
    ...over,
  }
}

describe('a length in words', () => {
  it('writes a minute and a second as a clock', () => {
    expect(formatSeconds(61)).toBe('1:01')
  })

  it('pads the seconds so two lengths line up in a column', () => {
    expect(formatSeconds(9)).toBe('0:09')
  })

  it('grows an hours field only once there are hours', () => {
    expect(formatSeconds(599)).toBe('9:59')
    expect(formatSeconds(3600)).toBe('1:00:00')
    expect(formatSeconds(3661)).toBe('1:01:01')
  })

  it('drops the fraction rather than rounding a length up', () => {
    // A track of 59.9 seconds has not reached a minute, and saying it has
    // makes two numbers that should agree disagree by one.
    expect(formatSeconds(59.9)).toBe('0:59')
  })

  it('writes nothing before the beginning', () => {
    expect(formatSeconds(-5)).toBe('0:00')
  })
})

describe('a measurement that may never have been taken', () => {
  it('writes an em dash when nothing was measured', () => {
    expect(formatMeasurement(null)).toBe('—')
  })

  it('writes a measured silence as a length, because that is what it is', () => {
    // `null` means the worker never looked; `0` means it looked and this
    // person said nothing. Rendering both as "0s" would erase the
    // difference between a missing measurement and a silent participant --
    // and the second one is often the thing somebody came to check.
    expect(formatMeasurement(0)).toBe('0:00')
  })

  it('writes a measured length as a clock', () => {
    expect(formatMeasurement(150)).toBe('2:30')
  })

  it('counts nothing as an em dash and none as a zero', () => {
    expect(formatCount(null)).toBe('—')
    expect(formatCount(0)).toBe('0')
    expect(formatCount(12)).toBe('12')
  })
})

describe('how much of a track is speech', () => {
  it('is the speech over the audio', () => {
    expect(speechShare(track({ audio_seconds: 600, speech_seconds: 150 }))).toBe(0.25)
  })

  it('is unknown when the speech was never measured', () => {
    expect(speechShare(track({ speech_seconds: null }))).toBeNull()
  })

  it('is unknown when the audio was never measured', () => {
    expect(speechShare(track({ audio_seconds: null }))).toBeNull()
  })

  it('is unknown when there is no audio to be a share of', () => {
    // Not zero: a share of nothing is not a small share, it is no answer.
    expect(speechShare(track({ audio_seconds: 0, speech_seconds: 0 }))).toBeNull()
  })

  it('is zero for somebody who was recorded and never spoke', () => {
    expect(speechShare(track({ audio_seconds: 600, speech_seconds: 0 }))).toBe(0)
  })

  it('never exceeds the whole track', () => {
    // The two numbers come from different stages -- one from the padded
    // track, one from what was actually transcribed -- so a bug upstream
    // could put speech above audio. A bar past its own end is a worse way
    // to learn that than a bar at its end.
    expect(speechShare(track({ audio_seconds: 100, speech_seconds: 140 }))).toBe(1)
  })

  it('writes a share as a percentage, and no share as an em dash', () => {
    expect(formatShare(0.25)).toBe('25%')
    expect(formatShare(0)).toBe('0%')
    expect(formatShare(null)).toBe('—')
  })
})

describe('naming a speaker and a channel', () => {
  it('uses the display name that was recorded with the track', () => {
    expect(trackLabel(track({ display_name: 'Ada' }))).toBe('Ada')
  })

  it('falls back to the account id when nobody recorded a name', () => {
    // Somebody who left the guild has no name left to look up. An unnamed
    // row would be a track nobody can attribute.
    expect(trackLabel(track({ display_name: null, discord_user_id: '3080' }))).toBe('3080')
  })

  it('falls back to the account id for a name that is only whitespace', () => {
    expect(trackLabel(track({ display_name: '  ', discord_user_id: '3080' }))).toBe('3080')
  })

  it('writes a channel the way Discord does', () => {
    expect(channelLabel(session({ channel_name: 'standup' }))).toBe('#standup')
  })

  it('names a channel by id when its name was never captured', () => {
    expect(channelLabel(session({ channel_name: null, channel_id: '9870' }))).toBe('Channel 9870')
  })
})

describe('how long a session was', () => {
  it('trusts the duration the API measured', () => {
    expect(sessionLength(session({ duration_seconds: 3600 }))).toBe(3600)
  })

  it('treats a measured zero as a duration rather than as an absence', () => {
    // A meeting that ended the second it started is a real thing, and
    // `duration_seconds || fallback` would quietly replace it.
    expect(sessionLength(session({ duration_seconds: 0 }))).toBe(0)
  })

  it('works the duration out from the two timestamps when it is missing', () => {
    const length = sessionLength(
      session({
        duration_seconds: null,
        started_at: '2026-08-21T14:00:00Z',
        ended_at: '2026-08-21T14:30:00Z',
      }),
    )
    expect(length).toBe(1800)
  })

  it('has no answer for a session that has not ended', () => {
    expect(sessionLength(session({ duration_seconds: null, ended_at: null }))).toBeNull()
  })

  it('has no answer when a timestamp cannot be read', () => {
    expect(sessionLength(session({ duration_seconds: null, ended_at: 'sometime' }))).toBeNull()
  })

  it('knows a session that is still running', () => {
    expect(isInProgress(session({ ended_at: null }))).toBe(true)
    expect(isInProgress(session({ ended_at: '2026-08-21T15:05:09Z' }))).toBe(false)
  })
})

describe('whether a protocol exists', () => {
  it('sees a protocol when there is a link to one', () => {
    expect(hasProtocol(session({ document_url: 'https://outline.example/doc/x' }))).toBe(true)
  })

  it('sees none when the field is null', () => {
    expect(hasProtocol(session({ document_url: null }))).toBe(false)
  })

  it('sees none in an empty link, which is not a document', () => {
    expect(hasProtocol(session({ document_url: '   ' }))).toBe(false)
  })
})

describe('when a session started', () => {
  it('writes a timestamp in the zone it is asked for', () => {
    expect(formatTimestamp('2026-08-21T14:05:09Z', 'UTC')).toBe('2026-08-21 14:05')
  })

  it('moves the same instant into the viewer\'s zone', () => {
    // Central European summer time is two hours ahead of UTC. The console
    // renders UTC on the server and the viewer's own zone after hydration,
    // which is why this has to be a parameter and not an ambient default.
    expect(formatTimestamp('2026-08-21T14:05:09Z', 'Europe/Berlin')).toBe('2026-08-21 16:05')
  })

  it('writes an em dash for a timestamp it cannot read', () => {
    expect(formatTimestamp('sometime', 'UTC')).toBe('—')
  })

  it('falls back to UTC for a zone the browser will not accept', () => {
    // `resolvedOptions().timeZone` has returned surprises before, and a
    // page that throws while formatting a date shows nothing at all.
    expect(formatTimestamp('2026-08-21T14:05:09Z', 'Mars/Olympus')).toBe('2026-08-21 14:05')
  })
})

describe('where a track is streamed from', () => {
  it('addresses one speaker inside one session', () => {
    expect(audioUrl('/api', '7f2b', '3080')).toBe('/api/sessions/7f2b/tracks/3080/audio')
  })

  it('tolerates a base that was written with a trailing slash', () => {
    expect(audioUrl('/api/', '7f2b', '3080')).toBe('/api/sessions/7f2b/tracks/3080/audio')
  })

  it('escapes both ids rather than letting one reshape the path', () => {
    // Ids are strings from an API, and a string that is allowed to contain
    // a slash is allowed to address a different endpoint.
    expect(audioUrl('/api', 'a/b', 'c?d')).toBe('/api/sessions/a%2Fb/tracks/c%3Fd/audio')
  })
})

describe('the canonical address of a recording', () => {
  it('is one path per session, so a link lands on the recording itself', () => {
    expect(recordingPath('4711')).toBe('/recordings/4711')
  })

  it('escapes the id, because a string that may contain a slash may address another page', () => {
    expect(recordingPath('a/b')).toBe('/recordings/a%2Fb')
  })
})

describe('the spectrogram endpoint', () => {
  it('sits beside the audio, under the same authorisation', () => {
    expect(spectrogramUrl('1', '2')).toBe('/sessions/1/tracks/2/spectrogram')
  })

  it('escapes both ids for the same reason audioUrl does', () => {
    expect(spectrogramUrl('a/b', 'c/d')).toBe('/sessions/a%2Fb/tracks/c%2Fd/spectrogram')
  })
})

describe('decoding a spectrogram payload', () => {
  const picture = (bytes: number[], bins: number, columns: number): SpectrogramResponse => ({
    columns,
    bins,
    sample_rate: 16000,
    hz_per_bin: 62.5,
    duration_seconds: 10,
    magnitudes: btoa(String.fromCharCode(...bytes)),
  })

  it('returns the matrix when the payload is the size it promised', () => {
    const decoded = decodeMagnitudes(picture([1, 2, 3, 4], 2, 2))
    expect(decoded).toEqual(new Uint8Array([1, 2, 3, 4]))
  })

  it('refuses a payload that is not the promised size', () => {
    // Drawing it anyway would assemble a picture from the wrong offsets --
    // wrong in a way that still looks like a spectrogram, which is worse
    // than not drawing one.
    expect(decodeMagnitudes(picture([1, 2, 3], 2, 2))).toBeNull()
  })

  it('refuses a payload that is not base64 at all', () => {
    expect(decodeMagnitudes({ ...picture([1], 1, 1), magnitudes: 'not base64!!' })).toBeNull()
  })
})

describe('how much of a session a track covers', () => {
  const session = (duration: number | null): RecordedSession => ({
    id: '1',
    started_at: '2026-08-01T10:00:00+00:00',
    ended_at: '2026-08-01T10:10:00+00:00',
    duration_seconds: duration,
    channel_id: '9',
    channel_name: 'standup',
    document_url: null,
    other_participants: [],
    tracks: [],
  })
  const track = (audio: number | null): SessionTrack => ({
    discord_user_id: '2',
    display_name: 'Anna',
    audio_seconds: audio,
    speech_seconds: null,
    segment_count: null,
  })

  it('is the share of the meeting that track has audio for', () => {
    expect(trackCoverage(session(600), track(300))).toEqual({ share: 0.5 })
  })

  it('never exceeds the whole, however the two numbers were measured', () => {
    expect(trackCoverage(session(600), track(900))).toEqual({ share: 1 })
  })

  it('has no answer when the track was never measured', () => {
    expect(trackCoverage(session(600), track(null))).toBeNull()
  })

  it('has no answer when the session has no length to measure against', () => {
    const open = { ...session(null), ended_at: null }
    expect(trackCoverage(open, track(300))).toBeNull()
  })
})
