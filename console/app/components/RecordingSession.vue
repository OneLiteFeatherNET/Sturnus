<script setup lang="ts">
/**
 * One meeting: when it was, who was in it, what was measured, and where
 * its protocol lives.
 *
 * The player is mounted only while the session is open. That is not a
 * visual nicety -- it is the difference between a page that costs a few
 * kilobytes of JSON and one that pulls every track of every session on
 * first paint.
 */
import {
  channelLabel,
  formatSeconds,
  formatTimestamp,
  hasProtocol,
  isInProgress,
  recordingPath,
  sessionLength,
  trackLabel,
  type RecordedSession,
} from '~/utils/recordings'

const props = defineProps<{
  session: RecordedSession
  open: boolean
  timeZone: string
}>()

defineEmits<{ toggle: [] }>()

const say = useSay()

const length = computed(() => sessionLength(props.session))
const panelId = computed(() => `session-${props.session.id}`)
const speakers = computed(() => props.session.tracks.map(trackLabel))
// Read here rather than asserted in the template: a template expression is
// compiled as JavaScript, and a `!` that survives type-checking would not
// survive the build.
const protocolUrl = computed(() => props.session.document_url ?? '')
const others = computed(() =>
  props.session.other_participants.map((person) => person.display_name).join(', '),
)
</script>

<template>
  <article
    class="rounded-2xl border"
    :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
  >
    <div class="flex flex-wrap items-start gap-4 p-4">
      <div class="min-w-0 flex-1 sm:min-w-56">
        <h2 class="flex flex-wrap items-center gap-2 text-base font-semibold">
          <span>{{ say(channelLabel(session)) }}</span>
          <span
            v-if="isInProgress(session)"
            class="rounded-full px-2 py-0.5 text-xs font-medium"
            :style="{ background: 'var(--positive)', color: 'var(--positive-contrast)' }"
          >
            {{ $t('recordings.recordingNow') }}
          </span>
        </h2>
        <p class="mt-1 text-sm tabular-nums" :style="{ color: 'var(--text-muted)' }">
          {{ formatTimestamp(session.started_at, timeZone) }}
          <span v-if="length !== null"> · {{ formatSeconds(length) }}</span>
          <span v-else> · {{ $t('common.durationUnknown') }}</span>
        </p>
      </div>

      <!-- The protocol, or the fact that there is none. A session without
           one is hidden nowhere: "no protocol" is usually the answer
           somebody came looking for, and a row that quietly omitted the
           link would send them to look for a bug instead. -->
      <!-- Wraps rather than refusing to shrink. Three controls with an
           intrinsic width of about 230 px, marked `shrink-0`, were what a
           375 px screen could not fit. -->
      <div class="flex flex-wrap items-center gap-2">
        <a
          v-if="hasProtocol(session)"
          :href="protocolUrl"
          target="_blank"
          rel="noreferrer"
          class="flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
          :style="{ color: 'var(--action)' }"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path
              d="M14 3h7v7h-2V6.4l-8.3 8.3-1.4-1.4L17.6 5H14V3ZM5 5h5v2H6v11h11v-4h2v6H5V5Z"
            />
          </svg>
          {{ $t('recordings.protocolLabel') }}
        </a>
        <span
          v-else
          class="rounded-lg px-3 py-1.5 text-sm"
          :style="{ color: 'var(--text-muted)' }"
        >
          {{ $t('recordings.noProtocol') }}
        </span>

        <button
          type="button"
          class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
          :style="{ color: 'var(--text-muted)' }"
          :aria-expanded="open"
          :aria-controls="panelId"
          @click="$emit('toggle')"
        >
          {{ open ? $t('recordings.close') : $t('recordings.listen') }}
        </button>

        <!-- The canonical address of this recording. The row above plays
             it in place, which is what somebody scanning a list wants;
             this is the link they send to a colleague, and the page that
             has the spectrograms, the per-track players and everything
             that is too heavy to put in a list ten of these long. -->
        <NuxtLink
          :to="recordingPath(session.id)"
          class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
          :style="{ color: 'var(--action)' }"
        >
          {{ $t('recordings.open') }}
        </NuxtLink>
      </div>
    </div>

    <div class="border-t px-4 py-3 text-sm" :style="{ borderColor: 'var(--border)' }">
      <p v-if="speakers.length > 0">
        <span :style="{ color: 'var(--text-muted)' }">{{ $t('recordings.recordedLabel') }}</span>
        {{ speakers.join(', ') }}
      </p>
      <p v-else :style="{ color: 'var(--text-muted)' }">
        {{ $t('recordings.nobodyConsented') }}
      </p>
      <p v-if="session.other_participants.length > 0" class="mt-1">
        <span :style="{ color: 'var(--text-muted)' }">{{ $t('recordings.alsoInChannel') }}</span>
        {{ others }}
      </p>
      <!-- The reader's own labels, read-only here. A list is for finding
           a meeting, not for editing one; the editor is on the recording's
           own page, where there is room to say who can see them. -->
      <ul v-if="session.tags.length > 0" class="mt-2 flex flex-wrap gap-1.5">
        <li
          v-for="tag in session.tags"
          :key="tag"
          class="rounded-full px-2 py-0.5 text-xs"
          :style="{ background: 'var(--surface-raised)', color: 'var(--text-muted)' }"
        >
          {{ tag }}
        </li>
      </ul>
    </div>

    <div :id="panelId" class="px-4 pb-4">
      <MultiTrackPlayer v-if="open && session.tracks.length > 0" :session="session" />
      <p
        v-else-if="open"
        class="mt-4 rounded-xl border p-4 text-sm"
        :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
      >
        {{ $t('recordings.nothingToPlay') }}
      </p>
    </div>
  </article>
</template>
