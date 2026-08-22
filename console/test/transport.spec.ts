/**
 * Driving several audio elements as one instrument.
 *
 * These tests exist because the property they pin is invisible until it is
 * broken: a multi-track player whose tracks have slipped apart sounds like
 * a bad recording, not like a bug, and by the time somebody notices they
 * are three sessions deep and no longer sure what they heard.
 *
 * Every track is a fake handle here. The transport never touches the DOM --
 * that is the whole reason it is a module rather than code inside a
 * component.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  DEFAULT_DRIFT_TOLERANCE,
  createTransport,
  gainFor,
  isAudible,
  type MediaHandle,
} from '../app/utils/transport'

interface FakeTrack extends MediaHandle {
  plays: number
  pauses: number
  playing: boolean
}

function fakeTrack(at = 0): FakeTrack {
  return {
    currentTime: at,
    volume: 1,
    plays: 0,
    pauses: 0,
    playing: false,
    play() {
      this.plays += 1
      this.playing = true
    },
    pause() {
      this.pauses += 1
      this.playing = false
    },
  }
}

describe('the mix', () => {
  it('lets a track through when nothing is muted and nothing is soloed', () => {
    expect(gainFor('ada', { muted: [], soloed: [] })).toBe(1)
  })

  it('silences a muted track', () => {
    expect(gainFor('ada', { muted: ['ada'], soloed: [] })).toBe(0)
  })

  it('silences everybody except the soloed track', () => {
    expect(gainFor('grace', { muted: [], soloed: ['ada'] })).toBe(0)
  })

  it('lets the soloed track through', () => {
    expect(gainFor('ada', { muted: [], soloed: ['ada'] })).toBe(1)
  })

  it('lets every soloed track through when several are soloed', () => {
    const mix = { muted: [], soloed: ['ada', 'grace'] }
    expect(gainFor('ada', mix)).toBe(1)
    expect(gainFor('grace', mix)).toBe(1)
    expect(gainFor('alan', mix)).toBe(0)
  })

  it('lets a soloed track through even when it is also muted', () => {
    // Solo wins over mute, the way every mixing desk does it. The cost is
    // that mute is not absolute; the alternative -- clicking solo on a
    // muted track and hearing nothing at all -- leaves somebody staring at
    // a silent player with no clue which of two buttons betrayed them.
    expect(gainFor('ada', { muted: ['ada'], soloed: ['ada'] })).toBe(1)
  })

  it('answers audibility as the same question phrased for a template', () => {
    expect(isAudible('ada', { muted: [], soloed: [] })).toBe(true)
    expect(isAudible('ada', { muted: ['ada'], soloed: [] })).toBe(false)
  })
})

describe('the transport', () => {
  let ada: FakeTrack
  let grace: FakeTrack

  beforeEach(() => {
    ada = fakeTrack()
    grace = fakeTrack()
  })

  function twoTracks() {
    const transport = createTransport()
    transport.attach('ada', ada)
    transport.attach('grace', grace)
    return transport
  }

  it('starts every attached track from one press of play', () => {
    const transport = twoTracks()
    transport.play()
    expect(ada.playing).toBe(true)
    expect(grace.playing).toBe(true)
    expect(transport.snapshot().playing).toBe(true)
  })

  it('pauses every attached track from one press of pause', () => {
    const transport = twoTracks()
    transport.play()
    transport.pause()
    expect(ada.playing).toBe(false)
    expect(grace.playing).toBe(false)
    expect(transport.snapshot().playing).toBe(false)
  })

  it('applies a seek to every track, not only the one that was scrubbed', () => {
    const transport = twoTracks()
    transport.seek(42)
    expect(ada.currentTime).toBe(42)
    expect(grace.currentTime).toBe(42)
    expect(transport.snapshot().position).toBe(42)
  })

  it('seeks a track that had wandered even a little off, unlike drift repair', () => {
    // A deliberate seek is an instruction, not a correction: it lands on
    // every track regardless of how close that track already was.
    const transport = twoTracks()
    grace.currentTime = 42.01
    transport.seek(42)
    expect(grace.currentTime).toBe(42)
  })

  it('refuses to seek before the beginning', () => {
    const transport = twoTracks()
    transport.seek(-30)
    expect(ada.currentTime).toBe(0)
  })

  it('refuses to seek past a known duration', () => {
    const transport = createTransport({ duration: 90 })
    transport.attach('ada', ada)
    transport.seek(1000)
    expect(ada.currentTime).toBe(90)
  })

  it('seeks freely when no duration is known yet', () => {
    const transport = twoTracks()
    transport.seek(1000)
    expect(ada.currentTime).toBe(1000)
  })

  it('drops a track that arrives late into the position everyone else holds', () => {
    // Lazy loading depends on this: a track whose element mounts after the
    // listener already pressed play has to join the meeting where it is
    // being held, not at the beginning.
    const transport = twoTracks()
    transport.seek(30)
    transport.play()

    const alan = fakeTrack()
    transport.attach('alan', alan)
    expect(alan.currentTime).toBe(30)
    expect(alan.playing).toBe(true)
  })

  it('leaves a track that arrives while paused stopped, but in position', () => {
    const transport = twoTracks()
    transport.seek(30)

    const alan = fakeTrack()
    transport.attach('alan', alan)
    expect(alan.currentTime).toBe(30)
    expect(alan.playing).toBe(false)
  })

  it('ignores an element that is already attached under the same name', () => {
    // A Vue template ref fires again on every patch of its element, and
    // the player re-renders several times a second while playing.
    const transport = twoTracks()
    transport.play()
    const playsSoFar = ada.plays
    transport.attach('ada', ada)
    expect(ada.plays).toBe(playsSoFar)
  })

  it('replaces an element that was remounted under the same name', () => {
    const transport = twoTracks()
    transport.seek(15)
    const replacement = fakeTrack()
    transport.attach('ada', replacement)
    expect(replacement.currentTime).toBe(15)
    expect(transport.snapshot().ids).toEqual(['ada', 'grace'])
  })

  it('stops driving a track once it is detached', () => {
    const transport = twoTracks()
    transport.detach('grace')
    transport.seek(12)
    expect(ada.currentTime).toBe(12)
    expect(grace.currentTime).toBe(0)
  })

  it('silences a detached track rather than leaving it playing unattended', () => {
    const transport = twoTracks()
    transport.play()
    transport.detach('grace')
    expect(grace.playing).toBe(false)
  })
})

describe('muting and soloing', () => {
  let ada: FakeTrack
  let grace: FakeTrack
  let alan: FakeTrack

  function threeTracks() {
    ada = fakeTrack()
    grace = fakeTrack()
    alan = fakeTrack()
    const transport = createTransport()
    transport.attach('ada', ada)
    transport.attach('grace', grace)
    transport.attach('alan', alan)
    return transport
  }

  it('mutes by turning a track down, never by stopping it', () => {
    // The rule the whole player rests on. A muted track that stopped
    // advancing would be somewhere else entirely the moment it came back.
    const transport = threeTracks()
    transport.play()
    transport.toggleMute('grace')

    expect(grace.volume).toBe(0)
    expect(grace.playing).toBe(true)
    expect(grace.pauses).toBe(0)
  })

  it('leaves the position of a muted track exactly where it was', () => {
    const transport = threeTracks()
    transport.seek(17)
    grace.currentTime = 17.4
    transport.toggleMute('grace')
    expect(grace.currentTime).toBe(17.4)
  })

  it('restores a track that is unmuted', () => {
    const transport = threeTracks()
    transport.toggleMute('grace')
    transport.toggleMute('grace')
    expect(grace.volume).toBe(1)
  })

  it('turns every other track down when one is soloed', () => {
    const transport = threeTracks()
    transport.toggleSolo('ada')
    expect(ada.volume).toBe(1)
    expect(grace.volume).toBe(0)
    expect(alan.volume).toBe(0)
  })

  it('keeps every track running while one is soloed', () => {
    const transport = threeTracks()
    transport.play()
    transport.toggleSolo('ada')
    expect(grace.playing).toBe(true)
    expect(alan.playing).toBe(true)
  })

  it('restores everybody when the solo is released', () => {
    const transport = threeTracks()
    transport.toggleSolo('ada')
    transport.toggleSolo('ada')
    expect(ada.volume).toBe(1)
    expect(grace.volume).toBe(1)
    expect(alan.volume).toBe(1)
  })

  it('leaves an explicitly muted track muted when a solo is released', () => {
    // Gain is derived from the mix on every change and never stored, so
    // releasing a solo cannot forget a mute that was chosen before it.
    const transport = threeTracks()
    transport.toggleMute('alan')
    transport.toggleSolo('ada')
    transport.toggleSolo('ada')
    expect(grace.volume).toBe(1)
    expect(alan.volume).toBe(0)
  })

  it('clears every solo at once', () => {
    const transport = threeTracks()
    transport.toggleSolo('ada')
    transport.toggleSolo('grace')
    transport.clearSolo()
    expect(alan.volume).toBe(1)
    expect(transport.snapshot().soloed).toEqual([])
  })

  it('brings a track that arrives during a solo in already silenced', () => {
    const transport = threeTracks()
    transport.toggleSolo('ada')
    const late = fakeTrack()
    transport.attach('late', late)
    expect(late.volume).toBe(0)
  })

  it('scales every audible track by the master volume', () => {
    const transport = threeTracks()
    transport.setVolume(0.5)
    transport.toggleMute('alan')
    expect(ada.volume).toBe(0.5)
    expect(alan.volume).toBe(0)
  })

  it('keeps the master volume inside what an element accepts', () => {
    const transport = threeTracks()
    transport.setVolume(4)
    expect(ada.volume).toBe(1)
    transport.setVolume(-1)
    expect(ada.volume).toBe(0)
  })
})

describe('keeping the tracks together', () => {
  it('takes its position from the first track that reports one', () => {
    const ada = fakeTrack()
    const transport = createTransport()
    transport.attach('ada', ada)
    transport.report('ada', 12.5)
    expect(transport.snapshot().position).toBe(12.5)
  })

  it('pulls a track that has drifted back onto the clock', () => {
    // Separate elements decode separately; over a long meeting they slide
    // apart by fractions of a second until one speaker answers a question
    // before it was asked.
    const ada = fakeTrack()
    const grace = fakeTrack()
    const transport = createTransport({ driftTolerance: 0.2 })
    transport.attach('ada', ada)
    transport.attach('grace', grace)

    transport.report('ada', 30)
    grace.currentTime = 31
    transport.report('grace', 31)

    expect(grace.currentTime).toBe(30)
  })

  it('leaves a track alone while it is close enough', () => {
    // Assigning currentTime restarts decoding, which is audible. Repairing
    // drift that nobody can hear would be worse than the drift.
    const ada = fakeTrack()
    const grace = fakeTrack()
    const transport = createTransport({ driftTolerance: 0.2 })
    transport.attach('ada', ada)
    transport.attach('grace', grace)

    transport.report('ada', 30)
    grace.currentTime = 30.1
    transport.report('grace', 30.1)

    expect(grace.currentTime).toBe(30.1)
  })

  it('tolerates a quarter of a second of drift by default', () => {
    expect(DEFAULT_DRIFT_TOLERANCE).toBe(0.25)
  })

  it('hands the clock to another track when the first one runs out', () => {
    // Tracks are per speaker and need not be the same length. A clock that
    // ended would freeze the position and drag everybody still speaking
    // back to the moment one person stopped.
    const ada = fakeTrack()
    const grace = fakeTrack()
    const transport = createTransport()
    transport.attach('ada', ada)
    transport.attach('grace', grace)

    transport.report('ada', 40)
    transport.markEnded('ada')
    transport.report('grace', 55)

    expect(transport.snapshot().position).toBe(55)
  })

  it('leaves a track that has ended where it stopped', () => {
    const ada = fakeTrack()
    const grace = fakeTrack()
    const transport = createTransport()
    transport.attach('ada', ada)
    transport.attach('grace', grace)

    ada.currentTime = 40
    transport.markEnded('ada')
    transport.report('grace', 55)

    expect(ada.currentTime).toBe(40)
  })

  it('stops the transport once the last track has ended', () => {
    const ada = fakeTrack()
    const transport = createTransport()
    transport.attach('ada', ada)
    transport.play()
    transport.markEnded('ada')
    expect(transport.snapshot().playing).toBe(false)
  })

  it('brings an ended track back when the listener seeks', () => {
    const ada = fakeTrack()
    const grace = fakeTrack()
    const transport = createTransport()
    transport.attach('ada', ada)
    transport.attach('grace', grace)
    transport.play()
    transport.markEnded('ada')

    transport.seek(10)

    expect(ada.currentTime).toBe(10)
    expect(ada.playing).toBe(true)
    expect(transport.snapshot().ended).toEqual([])
  })

  it('realigns a track that slipped while everything was paused', () => {
    const ada = fakeTrack()
    const grace = fakeTrack()
    const transport = createTransport({ driftTolerance: 0.2 })
    transport.attach('ada', ada)
    transport.attach('grace', grace)
    transport.seek(20)
    grace.currentTime = 25

    transport.play()

    expect(grace.currentTime).toBe(20)
  })

  it('tells its listener whenever anything changed', () => {
    // The component mirrors the snapshot into a ref; without this it would
    // render a transport that has already moved on.
    const onChange = vi.fn()
    const transport = createTransport({ onChange })
    transport.attach('ada', fakeTrack())
    transport.play()
    transport.toggleMute('ada')
    expect(onChange).toHaveBeenCalledTimes(3)
  })
})
