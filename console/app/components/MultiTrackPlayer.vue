<script setup lang="ts">
/**
 * One session, one transport, one `<audio>` element per consenting speaker.
 *
 * The elements are never mixed down and never given their own controls.
 * Everything that moves playback -- play, pause, the scrubber -- goes
 * through the transport in `~/utils/transport`, which is where the reason
 * is written down: separate elements are separate clocks, and a meeting
 * whose speakers have slid apart sounds like a bad recording rather than
 * like a bug.
 *
 * Solo and mute reach the transport too, and it turns tracks down instead
 * of stopping them. That costs bandwidth for audio nobody is hearing. It
 * buys the property that makes the whole format worth keeping: unmuting
 * lands you exactly where everybody else already is.
 *
 * This component is only ever rendered for a session the listener opened.
 * That is the lazy loading: ten sessions on a page would otherwise mean
 * every track of every one of them, which is hundreds of megabytes for a
 * page somebody may only be scanning for a protocol link.
 */
import {
  audioUrl,
  formatCount,
  formatMeasurement,
  formatSeconds,
  formatShare,
  sessionLength,
  speechShare,
  trackLabel,
  type RecordedSession,
} from '~/utils/recordings'
import { createTransport, isAudible, type TransportSnapshot } from '~/utils/transport'

const props = defineProps<{ session: RecordedSession }>()

// The public base, never the internal one: an `<audio>` element loads in a
// browser, and the cluster address a server-side render would use
// addresses nothing the listener can reach.
const base = useRuntimeConfig().public.apiBase

const transport = createTransport({
  duration: sessionLength(props.session),
  onChange: (next) => publish(next),
})
const state = shallowRef<TransportSnapshot>(transport.snapshot())

function publish(next: TransportSnapshot) {
  state.value = next
}

/** Per track: whether its audio can be played, and whether it failed. */
const loading = ref<Record<string, boolean>>(
  Object.fromEntries(props.session.tracks.map((track) => [track.discord_user_id, true])),
)
const failed = ref<Record<string, boolean>>({})

/** The longest thing there is to play: the session, or a track that
 *  outlives the recorded length because it was padded. */
const longest = ref(sessionLength(props.session) ?? 0)

/**
 * A ref callback per track, cached so its identity is stable.
 *
 * An inline arrow would be a new function on every render, and Vue treats
 * a new ref function as a new binding -- unmounting and remounting the
 * element several times a second. The transport also ignores a repeated
 * attach of the same element, so this is a belt and braces the audio needs.
 */
const binders = new Map<string, (el: unknown) => void>()

function binder(id: string) {
  let existing = binders.get(id)
  if (!existing) {
    existing = (el: unknown) => {
      // Duck-typed rather than `instanceof HTMLAudioElement`: the check
      // has to hold wherever this runs, and it is the interface the
      // transport drives anyway.
      if (el && typeof (el as HTMLAudioElement).play === 'function') {
        transport.attach(id, el as HTMLAudioElement)
      } else {
        transport.detach(id)
      }
    }
    binders.set(id, existing)
  }
  return existing
}

function onMetadata(id: string, event: Event) {
  const element = event.target as HTMLAudioElement
  loading.value = { ...loading.value, [id]: false }
  if (Number.isFinite(element.duration) && element.duration > longest.value) {
    longest.value = element.duration
    transport.setDuration(longest.value)
  }
}

function onTimeUpdate(id: string, event: Event) {
  transport.report(id, (event.target as HTMLAudioElement).currentTime)
}

function onFailure(id: string) {
  // One track that will not load must not take the session with it: the
  // other voices are still worth hearing, and a row that says so is more
  // use than a player that silently plays one speaker short.
  loading.value = { ...loading.value, [id]: false }
  failed.value = { ...failed.value, [id]: true }
  transport.detach(id)
}

function onScrub(event: Event) {
  transport.seek(Number((event.target as HTMLInputElement).value))
}

function onVolume(event: Event) {
  transport.setVolume(Number((event.target as HTMLInputElement).value))
}

const scrubMax = computed(() => Math.max(1, state.value.duration ?? longest.value))
const remaining = computed(() => Math.max(0, scrubMax.value - state.value.position))
</script>

<template>
  <div class="mt-4 rounded-xl border p-4" :style="{ borderColor: 'var(--border)' }">
    <div class="flex flex-wrap items-center gap-3">
      <button
        type="button"
        class="flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-[var(--surface)] transition-opacity hover:opacity-85"
        :style="{ background: 'var(--color-brand-cyan)' }"
        :aria-label="state.playing ? 'Pause every track' : 'Play every track'"
        @click="transport.toggle()"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
          <path v-if="state.playing" d="M7 5h4v14H7V5Zm6 0h4v14h-4V5Z" />
          <path v-else d="M8 5v14l11-7L8 5Z" />
        </svg>
      </button>

      <span class="w-16 shrink-0 text-sm tabular-nums" :style="{ color: 'var(--text-muted)' }">
        {{ formatSeconds(state.position) }}
      </span>

      <input
        type="range"
        min="0"
        step="0.1"
        class="h-1.5 min-w-40 flex-1 cursor-pointer appearance-none rounded-full accent-[var(--color-brand-cyan)]"
        :style="{ background: 'var(--surface-sunken)' }"
        :max="scrubMax"
        :value="state.position"
        aria-label="Position in the recording"
        @input="onScrub"
      >

      <span class="w-16 shrink-0 text-right text-sm tabular-nums" :style="{ color: 'var(--text-muted)' }">
        −{{ formatSeconds(remaining) }}
      </span>

      <label class="flex shrink-0 items-center gap-2 text-sm" :style="{ color: 'var(--text-muted)' }">
        <span class="sr-only">Volume</span>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M4 9v6h4l5 4V5L8 9H4Zm12.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4Z" />
        </svg>
        <input
          type="range"
          min="0"
          max="1"
          step="0.05"
          class="h-1.5 w-24 cursor-pointer appearance-none rounded-full accent-[var(--color-brand-cyan)]"
          :style="{ background: 'var(--surface-sunken)' }"
          :value="state.volume"
          @input="onVolume"
        >
      </label>

      <button
        v-if="state.soloed.length > 0"
        type="button"
        class="shrink-0 rounded-lg px-3 py-1.5 text-sm transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ color: 'var(--text-muted)' }"
        @click="transport.clearSolo()"
      >
        Hear everyone
      </button>
    </div>

    <ul class="mt-4 flex flex-col gap-2">
      <li
        v-for="track in session.tracks"
        :key="track.discord_user_id"
        class="rounded-lg p-3 transition-opacity"
        :style="{ background: 'var(--surface-raised)' }"
        :class="isAudible(track.discord_user_id, state) ? '' : 'opacity-55'"
      >
        <div class="flex flex-wrap items-center gap-3">
          <span class="min-w-32 flex-1 truncate text-sm font-medium">
            {{ trackLabel(track) }}
          </span>

          <span
            v-if="failed[track.discord_user_id]"
            class="text-xs"
            :style="{ color: 'var(--color-brand-red)' }"
          >
            Audio unavailable
          </span>
          <span
            v-else-if="loading[track.discord_user_id]"
            class="text-xs"
            :style="{ color: 'var(--text-muted)' }"
          >
            Loading…
          </span>

          <div class="flex shrink-0 items-center gap-1">
            <button
              type="button"
              class="rounded-md px-2 py-1 text-xs font-semibold transition-colors"
              :style="
                state.soloed.includes(track.discord_user_id)
                  ? { background: 'var(--color-brand-cyan)', color: 'var(--surface)' }
                  : { background: 'var(--surface)', color: 'var(--text-muted)' }
              "
              :aria-pressed="state.soloed.includes(track.discord_user_id)"
              :aria-label="`Hear ${trackLabel(track)} alone`"
              @click="transport.toggleSolo(track.discord_user_id)"
            >
              Solo
            </button>
            <button
              type="button"
              class="rounded-md px-2 py-1 text-xs font-semibold transition-colors"
              :style="
                state.muted.includes(track.discord_user_id)
                  ? { background: 'var(--color-brand-red)', color: 'var(--surface)' }
                  : { background: 'var(--surface)', color: 'var(--text-muted)' }
              "
              :aria-pressed="state.muted.includes(track.discord_user_id)"
              :aria-label="`Mute ${trackLabel(track)}`"
              @click="transport.toggleMute(track.discord_user_id)"
            >
              Mute
            </button>
          </div>
        </div>

        <dl class="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          <div class="flex gap-1">
            <dt>Audio</dt>
            <dd class="tabular-nums">{{ formatMeasurement(track.audio_seconds) }}</dd>
          </div>
          <div class="flex gap-1">
            <dt>Speech</dt>
            <dd class="tabular-nums">{{ formatMeasurement(track.speech_seconds) }}</dd>
          </div>
          <div class="flex gap-1">
            <dt>Segments</dt>
            <dd class="tabular-nums">{{ formatCount(track.segment_count) }}</dd>
          </div>
          <div class="flex gap-1">
            <dt>Share</dt>
            <dd class="tabular-nums">{{ formatShare(speechShare(track)) }}</dd>
          </div>
        </dl>

        <audio
          :ref="binder(track.discord_user_id)"
          :src="audioUrl(base, session.id, track.discord_user_id)"
          class="hidden"
          preload="metadata"
          @loadedmetadata="onMetadata(track.discord_user_id, $event)"
          @timeupdate="onTimeUpdate(track.discord_user_id, $event)"
          @ended="transport.markEnded(track.discord_user_id)"
          @error="onFailure(track.discord_user_id)"
        />
      </li>
    </ul>
  </div>
</template>
