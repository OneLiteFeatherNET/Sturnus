/**
 * One transport, many voices.
 *
 * A Sturnus session is not a recording. It is one recording per consenting
 * speaker, kept apart on purpose: that is the single property that makes
 * this format worth the storage, because it is the one thing an ordinary
 * meeting recording cannot give you -- one person at a time, clearly, with
 * everybody else turned down.
 *
 * Keeping them apart in the player means several `<audio>` elements, and
 * several `<audio>` elements are several independent clocks. Left alone
 * they slide apart: each decodes on its own schedule, each starts when its
 * own buffer says so, and after twenty minutes one speaker answers a
 * question the other has not asked yet. So there is exactly one transport
 * and the elements obey it -- play, pause and seek reach all of them, and
 * a track that has wandered is pulled back onto the clock.
 *
 * Two decisions carry the rest:
 *
 * - **Solo and mute change gain, never playback.** A muted track that was
 *   paused would be somewhere else entirely the moment it was unmuted, and
 *   the listener would hear the desynchronisation as a bad recording
 *   rather than as a bug. Silence costs bandwidth here; correctness is
 *   worth it.
 * - **Gain is derived, never stored.** Every change recomputes each
 *   track's volume from the mix, so releasing a solo cannot forget a mute
 *   that was chosen before it, and a track that mounts late arrives at the
 *   volume the mix already implies.
 *
 * None of this touches the DOM. That is why it is a module and not code
 * inside a component: the property it defends is inaudible until it is
 * broken, and a test is the only place it can be checked before somebody
 * loses an afternoon to a meeting that will not line up.
 */

/**
 * The slice of `HTMLAudioElement` the transport actually drives.
 *
 * Narrower than the real thing, and named separately, so the interesting
 * behaviour can be tested against a handful of plain objects instead of a
 * media stack that only exists in a browser.
 */
export interface MediaHandle {
  currentTime: number
  volume: number
  play(): Promise<void> | void
  pause(): void
}

/** Who is turned down, and who is being listened to alone. */
export interface MixState {
  readonly muted: readonly string[]
  readonly soloed: readonly string[]
}

/** Everything a component needs to render the player, in one flat value. */
export interface TransportSnapshot extends MixState {
  readonly ids: readonly string[]
  readonly ended: readonly string[]
  readonly playing: boolean
  readonly position: number
  readonly volume: number
  readonly duration: number | null
}

/**
 * How far a track may drift before it is dragged back, in seconds.
 *
 * A quarter of a second: below what anybody notices as an echo between two
 * voices, and far enough above ordinary decoder jitter that the repair
 * does not fire constantly. Assigning `currentTime` restarts decoding and
 * is itself audible, so repairing drift nobody can hear would be worse
 * than the drift.
 */
export const DEFAULT_DRIFT_TOLERANCE = 0.25

export interface TransportOptions {
  /** Seconds of slip tolerated before a track is realigned. */
  driftTolerance?: number
  /** The session's length, used to keep a seek inside the recording. */
  duration?: number | null
  /** Called after anything observable changed, with the new snapshot. */
  onChange?: (snapshot: TransportSnapshot) => void
}

export interface Transport {
  /** Puts an element under this transport, in step with everything else. */
  attach(id: string, media: MediaHandle): void
  /** Removes an element, silencing it on the way out. */
  detach(id: string): void
  play(): void
  pause(): void
  toggle(): void
  /** Moves every track to `seconds`, clamped to the recording. */
  seek(seconds: number): void
  /** What one element believes the time is; see the note on drift. */
  report(id: string, seconds: number): void
  /** That element reached its end and has no more to contribute. */
  markEnded(id: string): void
  toggleMute(id: string): void
  toggleSolo(id: string): void
  clearSolo(): void
  setVolume(level: number): void
  setDuration(seconds: number | null): void
  snapshot(): TransportSnapshot
}

/**
 * What a track should be turned to, given the mix.
 *
 * Solo wins over mute, the way every mixing desk does it. The cost is that
 * mute is not absolute -- soloing a muted speaker makes them audible
 * again. The alternative costs more: clicking solo and hearing nothing at
 * all leaves somebody staring at a silent player with no way to tell which
 * of two buttons betrayed them.
 */
export function gainFor(id: string, mix: MixState): number {
  if (mix.soloed.length > 0) return mix.soloed.includes(id) ? 1 : 0
  return mix.muted.includes(id) ? 0 : 1
}

/** The same question, phrased the way a template wants to ask it. */
export function isAudible(id: string, mix: MixState): boolean {
  return gainFor(id, mix) > 0
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high)
}

export function createTransport(options: TransportOptions = {}): Transport {
  const tolerance = options.driftTolerance ?? DEFAULT_DRIFT_TOLERANCE
  const notify = options.onChange
  // Insertion-ordered, which is what makes "the first track still running"
  // a stable answer for who holds the clock.
  const media = new Map<string, MediaHandle>()
  const muted = new Set<string>()
  const soloed = new Set<string>()
  const ended = new Set<string>()
  let duration = options.duration ?? null
  let playing = false
  let position = 0
  let volume = 1

  function mix(): MixState {
    return { muted: [...muted], soloed: [...soloed] }
  }

  function snapshot(): TransportSnapshot {
    return {
      ids: [...media.keys()],
      ended: [...ended],
      muted: [...muted],
      soloed: [...soloed],
      playing,
      position,
      volume,
      duration,
    }
  }

  function changed() {
    notify?.(snapshot())
  }

  function applyGains() {
    const state = mix()
    for (const [id, handle] of media) {
      handle.volume = gainFor(id, state) * volume
    }
  }

  /** Only when it actually slipped: see DEFAULT_DRIFT_TOLERANCE. */
  function alignIfDrifted(handle: MediaHandle, seconds: number) {
    if (Math.abs(handle.currentTime - seconds) > tolerance) {
      handle.currentTime = seconds
    }
  }

  /**
   * Whichever attached track is still running, in the order they arrived.
   *
   * Tracks are per speaker and need not be the same length -- somebody who
   * left early has a shorter one. A clock that has ended would freeze the
   * position and drag everybody still talking back to the moment that one
   * person stopped, so the job moves on.
   */
  function clockId(): string | undefined {
    for (const id of media.keys()) {
      if (!ended.has(id)) return id
    }
    return media.keys().next().value
  }

  function startAll() {
    for (const handle of media.values()) {
      alignIfDrifted(handle, position)
      void handle.play()
    }
  }

  function play() {
    playing = true
    startAll()
    changed()
  }

  function pause() {
    playing = false
    for (const handle of media.values()) handle.pause()
    changed()
  }

  return {
    attach(id, handle) {
      // Idempotent on purpose. A Vue template ref is re-invoked on every
      // patch of its element, and this component re-renders several times
      // a second while playing; re-seating the same element each time
      // would restart its decoder and turn the player into a stutter.
      if (media.get(id) === handle) return
      media.set(id, handle)
      // A track that mounts late joins the meeting where it is being held,
      // at the volume the mix already implies. This is what lets audio be
      // loaded only when a session is opened without the listener having
      // to press play a second time.
      handle.currentTime = position
      handle.volume = gainFor(id, mix()) * volume
      if (playing) void handle.play()
      changed()
    },

    detach(id) {
      const handle = media.get(id)
      if (!handle) return
      // An element that leaves the transport and keeps playing is a voice
      // nothing on screen can stop.
      handle.pause()
      media.delete(id)
      ended.delete(id)
      changed()
    },

    play,

    pause,

    // A method rather than something the template works out, because a
    // handler bound to `transport.toggle` must not depend on how it was
    // reached.
    toggle() {
      if (playing) pause()
      else play()
    },

    seek(seconds) {
      position = clamp(seconds, 0, duration ?? Number.POSITIVE_INFINITY)
      // A seek is an instruction, not a repair: it lands on every track
      // regardless of how close that track already was, because "close
      // enough" is a judgement about drift and this is a decision made by
      // the person listening.
      for (const handle of media.values()) handle.currentTime = position
      // Seeking gives a track that had run out something left to play.
      ended.clear()
      if (playing) startAll()
      changed()
    },

    report(id, seconds) {
      const clock = clockId()
      if (id === clock) {
        position = seconds
        for (const [otherId, handle] of media) {
          if (otherId === clock || ended.has(otherId)) continue
          alignIfDrifted(handle, position)
        }
        changed()
        return
      }
      const handle = media.get(id)
      if (handle && !ended.has(id)) alignIfDrifted(handle, position)
    },

    markEnded(id) {
      if (!media.has(id)) return
      ended.add(id)
      // Nothing left anywhere: the transport is stopped, not merely quiet,
      // so the button says play rather than pause.
      if (ended.size === media.size) playing = false
      changed()
    },

    toggleMute(id) {
      if (muted.has(id)) muted.delete(id)
      else muted.add(id)
      applyGains()
      changed()
    },

    toggleSolo(id) {
      if (soloed.has(id)) soloed.delete(id)
      else soloed.add(id)
      applyGains()
      changed()
    },

    clearSolo() {
      soloed.clear()
      applyGains()
      changed()
    },

    setVolume(level) {
      volume = clamp(level, 0, 1)
      applyGains()
      changed()
    },

    setDuration(seconds) {
      duration = seconds
      changed()
    },

    snapshot,
  }
}
