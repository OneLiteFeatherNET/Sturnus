<script setup lang="ts">
/**
 * One speaker at a time: their audio, their spectrogram, and what their
 * file is.
 *
 * **Why this is not the transport on the meeting tab.** That one plays the
 * conversation — every speaker on one clock, which is the only way a
 * discussion makes sense. This answers a different question: *what did
 * this one person say*, which the shared transport cannot reach without
 * muting everybody else first. Two questions, two tabs.
 *
 * **The file's own measurements live here rather than in a metadata
 * table.** A sample rate, a channel count and a stored size are only ever
 * asked about one way — "why does this track sound wrong" — and that is
 * asked while listening to that track. Set beside the audio they describe
 * they are an answer; collected into a table on a tab of their own they
 * are a row of numbers with nothing to compare against.
 *
 * **A measurement that was never taken is an em dash, never a zero.** All
 * three are null for every track written before migration 0013.
 * `trackFileFacts` keeps the three slots and puts the absence in the one
 * that is missing, so a gap reads as a gap rather than as `0 kB`.
 *
 * The speech measurements — how much audio, how much of it was speech, how
 * many segments — are deliberately *not* here. They are on the meeting
 * tab, beside the transport, because "who talked how much" is a fact about
 * the conversation. This tab measures the files.
 */
import {
  audioUrl,
  trackFileFacts,
  trackLabel,
  type RecordedSession,
} from '~/utils/recordings'

const props = withDefaults(
  defineProps<{
    session: RecordedSession
    /**
     * Whether there is still audio behind these tracks.
     *
     * `false` once retention has swept the recordings, and then the
     * players and the pictures go and the measurements stay. That is not
     * a technicality: the notice above the tab bar promises that what was
     * written from a recording survives it, and a list that dropped the
     * measurements along with the audio would make that sentence false on
     * the one page it is displayed on. Mounting an `<audio>` for a file
     * that is gone would give eight speakers eight failing players and no
     * explanation.
     */
    playable?: boolean
  }>(),
  { playable: true },
)

const say = useSay()

// The public base, never the internal one: an `<audio>` element loads in a
// browser, and the cluster address a server-side render would use
// addresses nothing the listener can reach.
const base = useRuntimeConfig().public.apiBase

/** Playback position per track, so each spectrogram can show its own
 *  playhead without the tracks having to know about each other. */
const positions = ref<Record<string, number>>({})
const players = new Map<string, HTMLAudioElement>()

/**
 * A ref callback per track, cached so its identity is stable.
 *
 * An inline arrow returns a fresh closure on every render, and the
 * template calls it on every render. Vue treats a new ref function as a
 * new binding, so a `timeupdate` — four a second, per playing track — tore
 * down and re-seated *every* track's ref and rebuilt every `<audio>` on
 * the page. `MultiTrackPlayer` caches its binders for exactly this reason.
 */
const binders = new Map<string, (el: unknown) => void>()

function bindPlayer(trackId: string) {
  let existing = binders.get(trackId)
  if (!existing) {
    existing = (el: unknown) => {
      // Duck-typed rather than `instanceof HTMLAudioElement`, so the check
      // holds wherever this runs.
      if (el && typeof (el as HTMLAudioElement).play === 'function') {
        players.set(trackId, el as HTMLAudioElement)
      } else {
        players.delete(trackId)
      }
    }
    binders.set(trackId, existing)
  }
  return existing
}

function onTime(trackId: string, event: Event) {
  positions.value = {
    ...positions.value,
    [trackId]: (event.target as HTMLAudioElement).currentTime,
  }
}

/** Clicking a spectrogram moves that track's own player to that moment. */
function seek(trackId: string, seconds: number) {
  const player = players.get(trackId)
  if (!player) return
  player.currentTime = seconds
  positions.value = { ...positions.value, [trackId]: seconds }
}
</script>

<template>
  <ul class="flex flex-col gap-4">
    <li
      v-for="track in props.session.tracks"
      :key="track.discord_user_id"
      class="rounded-xl p-4"
      :style="{ background: 'var(--surface-raised)' }"
    >
      <h3 class="text-sm font-medium">{{ trackLabel(track) }}</h3>

      <!-- Named, because eight identical "audio" controls in a row tell a
           screen-reader user nothing about which speaker they are on.
           `preload="none"`: nothing is fetched until somebody presses play
           on this particular speaker. -->
      <template v-if="props.playable">
        <audio
          :ref="bindPlayer(track.discord_user_id)"
          :src="audioUrl(base, props.session.id, track.discord_user_id)"
          class="mt-3 w-full"
          controls
          preload="none"
          :aria-label="$t('recordings.trackAlone', { name: trackLabel(track) })"
          @timeupdate="onTime(track.discord_user_id, $event)"
        />

        <TrackSpectrogram
          :session-id="props.session.id"
          :discord-user-id="track.discord_user_id"
          :position="positions[track.discord_user_id] ?? null"
          @seek="(seconds) => seek(track.discord_user_id, seconds)"
        />
      </template>

      <!-- What the file is, under the thing it describes. Three slots
           always, in one order, so a missing measurement is a gap in a row
           rather than a shorter row somebody has to count. -->
      <dl class="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs">
        <div v-for="fact in trackFileFacts(track)" :key="fact.labelKey" class="flex gap-1.5">
          <dt :style="{ color: 'var(--text-muted)' }">{{ $t(fact.labelKey) }}</dt>
          <dd class="tabular-nums">{{ say(fact.value) }}</dd>
        </div>
      </dl>
    </li>
  </ul>
</template>
