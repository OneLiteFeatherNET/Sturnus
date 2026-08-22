<script setup lang="ts">
/**
 * One speaker's track, drawn.
 *
 * The point of this view is not decoration. A listener deciding whether a
 * recording is worth opening, and an operator deciding whether a recording
 * failed, are asking the same question — *where is the speech* — and it is
 * the one question a list of numbers answers badly. Speech has a shape
 * nothing else in a voice channel has: harmonic stacks under about 4 kHz
 * moving at syllable rate. An empty track is flat. The difference is
 * obvious in a picture and invisible in a duration.
 *
 * It is also how the six-times-speed defect would have been caught in an
 * afternoon rather than in a production investigation: audio played at the
 * wrong rate puts its energy in visibly the wrong bands.
 *
 * **Drawn at native resolution and scaled by CSS.** The canvas is exactly
 * `columns` by `bins` device pixels — the size the API already chose — and
 * `image-rendering` is left to smooth it. Drawing rectangles at display
 * size instead would be tens of thousands of fill calls for the same
 * picture.
 *
 * **Fetched only when asked for.** A session with eight speakers is eight
 * of these, and each one costs the server a full pass over an encrypted
 * track. So nothing loads until the viewer asks for it by pressing the
 * control below. (An earlier version of this note claimed the loading was
 * also gated on the component being on screen. There is no observer here
 * and there never was; it is gated on the press alone.)
 *
 * **Seeking works from the keyboard.** The picture answers "where is the
 * speech", and clicking it moves the player to that moment — which for a
 * long time was the only way to do so, anywhere in the console, and left
 * the accessible name of the element instructing a screen-reader user to
 * click something they could not reach. It is now a slider: focusable,
 * driven by the arrow keys, and announcing where it is rather than how to
 * operate a mouse.
 */
import {
  decodeMagnitudes,
  formatSeconds,
  seekTarget,
  spectrogramUrl,
  type SpectrogramResponse,
} from '~/utils/recordings'

const props = defineProps<{
  sessionId: string
  discordUserId: string
  /** Where playback currently is, in seconds, or `null` if not playing. */
  position?: number | null
}>()

const emit = defineEmits<{ seek: [seconds: number] }>()

const api = useApi()
const canvas = ref<HTMLCanvasElement | null>(null)
const picture = ref<SpectrogramResponse | null>(null)
const status = ref<'idle' | 'loading' | 'ready' | 'failed'>('idle')

async function load() {
  if (status.value === 'loading' || status.value === 'ready') return
  status.value = 'loading'
  try {
    picture.value = await api<SpectrogramResponse>(
      spectrogramUrl(props.sessionId, props.discordUserId),
    )
    status.value = 'ready'
    await nextTick()
    draw()
  } catch {
    // One track that will not draw must not take the page with it. The
    // audio is still playable and the numbers are still true; only the
    // picture is missing, and a row that says so is more use than an
    // empty box that does not.
    status.value = 'failed'
  }
}

/** Asking again after a failure. The failed state used to carry no
 *  control at all, so one transient 500 put that track's picture out of
 *  reach for the life of the page. */
async function retry() {
  status.value = 'idle'
  await load()
}

/**
 * The palette, as a lookup table built once.
 *
 * Dark to cyan through a warm midtone, so that a quiet harmonic is
 * visible against the floor rather than lost in it. Computed rather than
 * interpolated per pixel: 76 800 cells is 76 800 interpolations otherwise,
 * and there are only 256 possible answers.
 */
const RAMP = (() => {
  const table = new Uint8Array(256 * 3)
  for (let v = 0; v < 256; v += 1) {
    const t = v / 255
    // Three stops: near-black, magenta-ish mid, brand cyan at the top.
    const r = t < 0.5 ? Math.round(2 * t * 150) : Math.round(150 - (t - 0.5) * 2 * 116)
    const g = t < 0.5 ? Math.round(2 * t * 40) : Math.round(40 + (t - 0.5) * 2 * 190)
    const b = t < 0.5 ? Math.round(20 + 2 * t * 110) : Math.round(130 + (t - 0.5) * 2 * 110)
    table[v * 3] = r
    table[v * 3 + 1] = g
    table[v * 3 + 2] = b
  }
  return table
})()

function draw() {
  const element = canvas.value
  const data = picture.value
  if (!element || !data) return
  const magnitudes = decodeMagnitudes(data)
  if (!magnitudes) {
    // A payload that is not the size it says it is. Refusing to draw beats
    // drawing a picture assembled from the wrong offsets, which would be
    // wrong in a way that still looks like a spectrogram.
    status.value = 'failed'
    return
  }

  element.width = data.columns
  element.height = data.bins
  const context = element.getContext('2d')
  if (!context) return

  const image = context.createImageData(data.columns, data.bins)
  for (let row = 0; row < data.bins; row += 1) {
    // Row 0 is the lowest frequency and belongs at the *bottom*, which is
    // where every spectrogram ever drawn puts it.
    const y = data.bins - 1 - row
    for (let column = 0; column < data.columns; column += 1) {
      const value = magnitudes[row * data.columns + column] ?? 0
      const target = (y * data.columns + column) * 4
      image.data[target] = RAMP[value * 3] ?? 0
      image.data[target + 1] = RAMP[value * 3 + 1] ?? 0
      image.data[target + 2] = RAMP[value * 3 + 2] ?? 0
      image.data[target + 3] = 255
    }
  }
  context.putImageData(image, 0, 0)
}

/** Where the playhead sits, as a percentage across the picture. */
const playheadPercent = computed(() => {
  const data = picture.value
  const at = props.position
  if (!data || at === null || at === undefined || data.duration_seconds <= 0) return null
  return Math.min(100, Math.max(0, (at / data.duration_seconds) * 100))
})

/** Time labels under the picture: start, middle, end. */
const ticks = computed(() => {
  const data = picture.value
  if (!data) return []
  return [0, 0.25, 0.5, 0.75, 1].map((fraction) => ({
    percent: fraction * 100,
    label: formatSeconds(fraction * data.duration_seconds),
  }))
})

/** The top of the frequency axis, which is half the sample rate. */
const topFrequency = computed(() => {
  const data = picture.value
  return data ? Math.round((data.hz_per_bin * data.bins) / 100) / 10 : 0
})

function onClick(event: MouseEvent) {
  const data = picture.value
  if (!data) return
  const box = (event.currentTarget as HTMLElement).getBoundingClientRect()
  if (box.width <= 0) return
  const fraction = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width))
  emit('seek', fraction * data.duration_seconds)
}

/** Where the slider says it is: the player's position, or the start. */
const at = computed(() => {
  const data = picture.value
  if (!data) return 0
  const here = props.position ?? 0
  return Math.min(Math.max(0, here), data.duration_seconds)
})

/**
 * Seeking with the keyboard.
 *
 * Where each key lands is `seekTarget`, tested without rendering
 * anything. What is left here is the one thing that needs an event:
 * `preventDefault` for the keys that were handled and for no others —
 * swallowing Tab or a browser shortcut to save writing out the list is
 * how a control becomes a trap.
 */
function onKey(event: KeyboardEvent) {
  const data = picture.value
  if (!data) return
  const target = seekTarget(event.key, at.value, data.duration_seconds)
  if (target === null) return
  event.preventDefault()
  emit('seek', target)
}
</script>

<template>
  <div class="mt-3">
    <!-- One control that stays mounted through both of its states rather
         than two that replace each other. The button used to disappear
         the moment it was pressed, which dropped the keyboard to the top
         of the document — eight tracks down a page, that is the whole
         page traversed again to get back. -->
    <button
      v-if="status === 'idle' || status === 'loading'"
      type="button"
      class="w-full rounded-lg border border-dashed px-3 py-4 text-xs transition-colors hover:bg-[var(--surface-raised)] disabled:cursor-progress"
      :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
      :disabled="status === 'loading'"
      @click="load()"
    >
      {{
        status === 'loading'
          ? 'Drawing this track…'
          : 'Show spectrogram — see where the speech is without listening'
      }}
    </button>

    <div
      v-else-if="status === 'failed'"
      class="rounded-lg border border-dashed px-3 py-3 text-center text-xs"
      :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
    >
      <p>This track could not be drawn. The audio above is unaffected.</p>
      <button
        type="button"
        class="mt-1 rounded-lg px-2 py-1 font-medium underline transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ color: 'var(--color-brand-cyan)' }"
        @click="retry()"
      >
        Try again
      </button>
    </div>

    <template v-else>
      <!-- A slider, because that is what it is: it reports where playback
           is and it moves it. `role="img"` here used to declare the whole
           thing a static picture while a click handler contradicted it,
           and the accessible name told a screen-reader user to click
           something that was not in the tab order at all. -->
      <div
        class="relative cursor-pointer overflow-hidden rounded-lg focus-visible:outline focus-visible:outline-2"
        :style="{ background: 'var(--surface-sunken)', outlineColor: 'var(--color-brand-cyan)' }"
        role="slider"
        tabindex="0"
        aria-label="Position in this track"
        aria-orientation="horizontal"
        aria-valuemin="0"
        :aria-valuemax="Math.round(picture?.duration_seconds ?? 0)"
        :aria-valuenow="Math.round(at)"
        :aria-valuetext="`${formatSeconds(at)} of ${formatSeconds(picture?.duration_seconds ?? 0)}`"
        @click="onClick"
        @keydown="onKey"
      >
        <!-- Hidden from assistive technology: it is a grid of pixels, and
             what it means is the sentence underneath it, which is
             readable. Announcing "image" here would add a name to the
             slider and no information. -->
        <canvas ref="canvas" class="block h-28 w-full" aria-hidden="true" />
        <div
          v-if="playheadPercent !== null"
          class="pointer-events-none absolute inset-y-0 w-px"
          :style="{ left: `${playheadPercent}%`, background: 'var(--color-brand-cyan)' }"
        />
      </div>

      <div
        class="relative mt-1 h-4 text-[10px] tabular-nums"
        :style="{ color: 'var(--text-muted)' }"
      >
        <span
          v-for="tick in ticks"
          :key="tick.percent"
          class="absolute -translate-x-1/2"
          :style="{ left: `${Math.min(97, Math.max(3, tick.percent))}%` }"
        >
          {{ tick.label }}
        </span>
      </div>

      <p class="text-[10px]" :style="{ color: 'var(--text-muted)' }">
        Frequency runs bottom to top, 0 to {{ topFrequency }} kHz. Speech is the banded region
        low down; a flat picture is a track with nothing on it.
      </p>
    </template>
  </div>
</template>
