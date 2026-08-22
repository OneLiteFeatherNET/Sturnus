<script setup lang="ts">
/**
 * Asking for a session to be transcribed again, and watching it happen.
 *
 * **Only administrators see anything here.** The status call answers 404
 * for everybody else — which is also what it answers for a session that
 * does not exist — so this component renders nothing at all rather than a
 * greyed-out control that confirms the endpoint exists. A disabled button
 * is an invitation to ask why; nothing is not.
 *
 * **The button is not the interesting part; the progress is.** A re-queue
 * is not instantaneous: the jobs go back to `pending` and a worker picks
 * them up when it gets to them. A control that reports nothing after
 * being pressed is one people press twice, and the second press lands
 * while the first redo is `running` — which is exactly the state the
 * server refuses. So the queue is polled while it is moving and the
 * speakers are shown changing state.
 *
 * **Polling stops when the work does.** Every poll is a database read, and
 * a page left open overnight must not keep making them. The loop is driven
 * by `isQueueBusy`, which reads the jobs rather than the session status —
 * a session flips to `documented` only after the document is written, so
 * stopping at the last `done` would stop one step early and never show the
 * finished document.
 *
 * **And it stops on the way out, including mid-request.** `clearTimeout`
 * alone was not enough: a timer that had already fired was past clearing,
 * and its continuation — which runs after an `await` — installed a fresh
 * one that nothing was left to cancel. Navigating away during any of the
 * three seconds a poll is in flight left a loop making twenty database
 * reads a minute for the life of the tab, per panel, invisibly. So every
 * continuation asks `mounted` before it acts.
 */
import {
  isQueueBusy,
  queueProgress,
  queueSpeakerLabel,
  queueStatusPath,
  queueStatusWords,
  requeuePath,
  type QueueSnapshot,
  type RequeueOutcome,
} from '~/utils/recordings'
import { ApiError } from '~/utils/apiError'

const props = defineProps<{ sessionId: string }>()

const api = useApi()
const snapshot = ref<QueueSnapshot | null>(null)
/** `null` until the first load settles; `false` means "not an administrator". */
const visible = ref<boolean | null>(null)
const working = ref(false)
const outcome = ref<RequeueOutcome | null>(null)
const failure = ref<string | null>(null)

/** How often the queue is re-read while it is moving. Three seconds is
 *  fast enough to feel live and slow enough that a forgotten tab is not a
 *  load-generator. */
const POLL_MS = 3000
let timer: ReturnType<typeof setTimeout> | null = null
/** Whether this panel is still on the page. Checked after every `await`,
 *  because that is where an unmount can happen without the code that
 *  resumes afterwards knowing about it. */
let mounted = true

async function readStatus(): Promise<void> {
  try {
    const fresh = await api<QueueSnapshot>(queueStatusPath(props.sessionId))
    if (!mounted) return
    snapshot.value = fresh
    failure.value = null
    // A 200 is the only proof that this person administers this guild,
    // and it is what makes the panel appear.
    visible.value = true
  } catch (error) {
    if (!mounted) return
    // 404 is the ordinary answer for "you do not administer this guild".
    // It is not a failure to report; it is the reason this panel is not
    // for this person.
    if (error instanceof ApiError && error.status === 404) {
      visible.value = false
      return
    }
    // Any other failure leaves `visible` exactly as it was, and that is
    // the whole point. Setting it to `true` here — which this used to do
    // — meant one 500 revealed the Transcription section, explanatory
    // text and all, to somebody who should never learn the endpoint
    // exists. A transient fault must not be a way to ask "am I looking
    // at a real session in a guild with a queue".
    failure.value = 'The transcription queue could not be read.'
  }
}

function scheduleIfBusy() {
  if (timer !== null) {
    clearTimeout(timer)
    timer = null
  }
  if (snapshot.value && isQueueBusy(snapshot.value)) {
    timer = setTimeout(async () => {
      await readStatus()
      // The timer that started this has already fired, so `clearTimeout`
      // in `onBeforeUnmount` could not have stopped what runs here.
      if (!mounted) return
      scheduleIfBusy()
    }, POLL_MS)
  }
}

async function requeue() {
  if (working.value) return
  working.value = true
  failure.value = null
  outcome.value = null
  try {
    outcome.value = await api<RequeueOutcome>(requeuePath(props.sessionId), { method: 'POST' })
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      // A refusal, not a fault: the request was fine and the session is
      // in a state a redo is not safe from. The server sends the reason
      // in the body, but `$fetch` has already discarded it by the time it
      // reaches here, so the fresh status below carries it instead.
      failure.value = null
    } else {
      failure.value = 'The re-queue could not be started.'
    }
  } finally {
    if (mounted) {
      working.value = false
      await readStatus()
      if (mounted) scheduleIfBusy()
    }
  }
}

/** Reading the queue again after a failure, without reloading the page.
 *  The failed state used to carry no control at all: one transient fault
 *  and the panel was a sentence saying so until somebody pressed F5. */
async function retry() {
  await readStatus()
  if (mounted) scheduleIfBusy()
}

onMounted(async () => {
  await readStatus()
  scheduleIfBusy()
})

onBeforeUnmount(() => {
  mounted = false
  if (timer !== null) clearTimeout(timer)
})

const progress = computed(() => (snapshot.value ? queueProgress(snapshot.value) : null))
const busy = computed(() => (snapshot.value ? isQueueBusy(snapshot.value) : false))

const STATUS_COLOUR: Record<string, string> = {
  done: 'var(--color-brand-green)',
  running: 'var(--color-brand-cyan)',
  dead: 'var(--color-brand-red)',
}
function statusColour(status: string): string {
  return STATUS_COLOUR[status] ?? 'var(--text-muted)'
}
</script>

<template>
  <!-- Nothing at all for somebody who does not administer this guild. -->
  <section
    v-if="visible"
    class="rounded-2xl border p-5"
    :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
  >
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-base font-semibold">Transcription</h2>
        <p class="mt-1 max-w-2xl text-sm" :style="{ color: 'var(--text-muted)' }">
          Re-running a transcription clears every speaker's existing text and writes a new
          protocol over the old one. Do it when the first pass got the audio wrong — not to
          tidy up wording.
        </p>
      </div>

      <button
        v-if="snapshot"
        type="button"
        class="shrink-0 rounded-lg px-3 py-1.5 text-sm font-medium transition-opacity disabled:cursor-not-allowed disabled:opacity-50"
        :style="{
          background: snapshot.can_requeue ? 'var(--color-brand-cyan)' : 'var(--surface-raised)',
          color: snapshot.can_requeue ? 'var(--surface)' : 'var(--text-muted)',
        }"
        :disabled="!snapshot.can_requeue || working || busy"
        @click="requeue()"
      >
        {{ working ? 'Starting…' : busy ? 'Running…' : 'Transcribe again' }}
      </button>
    </div>

    <p
      v-if="snapshot?.refusal"
      class="mt-3 rounded-lg p-3 text-sm"
      :style="{ background: 'var(--surface-raised)', color: 'var(--text-muted)' }"
    >
      {{ snapshot.refusal }}
    </p>

    <div
      v-if="failure"
      class="mt-3 rounded-lg p-3 text-sm"
      :style="{ background: 'var(--surface-raised)' }"
    >
      <p :style="{ color: 'var(--color-brand-red)' }">{{ failure }}</p>
      <button
        type="button"
        class="mt-2 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-sunken)]"
        :style="{ color: 'var(--color-brand-cyan)' }"
        @click="retry()"
      >
        Try again
      </button>
    </div>

    <!-- What the last press actually did. The skipped speakers are named
         separately and never folded into the count above them. -->
    <div
      v-if="outcome?.accepted"
      class="mt-3 rounded-lg p-3 text-sm"
      :style="{ background: 'var(--surface-raised)' }"
    >
      <p>
        Re-queued {{ outcome.requeued.length }}
        speaker{{ outcome.requeued.length === 1 ? '' : 's' }}.
      </p>
      <p v-if="outcome.skipped_erased.length > 0" class="mt-1" :style="{ color: 'var(--text-muted)' }">
        {{ outcome.skipped_erased.length }} left alone: their audio has been erased, so their
        existing text is carried into the new protocol unchanged.
      </p>
    </div>

    <template v-if="snapshot && snapshot.speakers.length > 0">
      <div
        v-if="progress !== null"
        class="mt-4 h-1.5 overflow-hidden rounded-full"
        :style="{ background: 'var(--surface-sunken)' }"
        role="progressbar"
        aria-label="Transcription progress"
        :aria-valuenow="Math.round(progress * 100)"
        :aria-valuetext="`${Math.round(progress * 100)}% of speakers finished`"
        aria-valuemin="0"
        aria-valuemax="100"
      >
        <div
          class="h-full transition-all duration-500"
          :style="{ width: `${progress * 100}%`, background: 'var(--color-brand-cyan)' }"
        />
      </div>

      <ul class="mt-3 flex flex-col gap-1.5">
        <li
          v-for="speaker in snapshot.speakers"
          :key="speaker.discord_user_id"
          class="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm"
        >
          <span class="min-w-32 flex-1 truncate">{{ queueSpeakerLabel(speaker) }}</span>
          <span class="text-xs font-medium" :style="{ color: statusColour(speaker.status) }">
            {{ queueStatusWords(speaker.status) }}
          </span>
          <span
            v-if="speaker.attempts > 1"
            class="text-xs tabular-nums"
            :style="{ color: 'var(--text-muted)' }"
          >
            {{ speaker.attempts }} attempts
          </span>
          <!-- The stored error, shortened. Enough to recognise a failure
               without the console becoming a way to read arbitrary text
               out of the job table. -->
          <span
            v-if="speaker.error"
            class="w-full truncate text-xs"
            :style="{ color: 'var(--color-brand-red)' }"
            :title="speaker.error"
          >
            {{ speaker.error }}
          </span>
        </li>
      </ul>

      <!-- In a live region because pressing the button drops focus (the
           button disables itself) and the only thing that changes
           afterwards is this line and the bar above it. Without one, an
           administrator using a screen reader presses "Transcribe again"
           and is told nothing at all. -->
      <p class="mt-3 text-xs" :style="{ color: 'var(--text-muted)' }" role="status" aria-live="polite">
        Session status: {{ snapshot.session_status }}<span v-if="busy"> · watching for changes</span>
      </p>
    </template>
  </section>
</template>
