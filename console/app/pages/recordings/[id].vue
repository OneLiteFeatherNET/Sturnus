<script setup lang="ts">
/**
 * One recording, at its own address.
 *
 * `/recordings/{id}` is canonical: a link in a protocol, a chat message or
 * a bookmark lands on the recording itself rather than on a list somebody
 * then has to search through. That is why this page exists separately
 * from `/recordings`, which stays a list.
 *
 * Everything about the session hangs off it — when it ran, who was in it,
 * what each track measured, the protocol it produced, and the audio. The
 * list view deliberately shows less: a page of ten sessions that loaded
 * this much would pull hundreds of megabytes for somebody scanning for a
 * link.
 *
 * **Two ways to listen, because they answer different questions.** The
 * multi-track transport at the top plays the meeting: every speaker on one
 * clock, which is the only way a conversation makes sense. Each track also
 * gets its own player below, because "what did this one person say" is a
 * question the shared transport cannot answer without muting everybody
 * else first.
 *
 * A 404 from the API is not an error page here. Somebody who was not in a
 * session and somebody following a link to one that never existed get the
 * same answer, and it is deliberately the same — see the audio endpoint's
 * module docstring for why that is a security property and not a
 * politeness.
 */
import {
  audioUrl,
  channelLabel,
  formatSeconds,
  formatTimestamp,
  hasProtocol,
  isInProgress,
  sessionLength,
  trackLabel,
  type RecordedSession,
} from '~/utils/recordings'
import { ApiError } from '~/utils/apiError'

const route = useRoute()
const api = useApi()
const id = computed(() => String(route.params.id ?? ''))

const { data, status, error, refresh } = await useAsyncData(
  () => `recording-${id.value}`,
  () => api<RecordedSession>(`/sessions/${encodeURIComponent(id.value)}`),
  { watch: [id] },
)

const session = computed<RecordedSession | null>(() => data.value ?? null)
const length = computed(() => (session.value ? sessionLength(session.value) : null))

/** A session this viewer may not see, and one that does not exist, are
 *  the same answer. Distinguishing them here would undo the endpoint's
 *  care in not distinguishing them. */
const missing = computed(() => error.value instanceof ApiError && error.value.status === 404)

useHead(() => ({
  title: session.value ? `${channelLabel(session.value)} — Recording` : 'Recording',
}))

const timeZone = ref('UTC')
onMounted(() => {
  try {
    timeZone.value = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    // Keep UTC; see `formatTimestamp`.
  }
})

const base = useRuntimeConfig().public.apiBase

/** Playback position per track, so each spectrogram can show its own
 *  playhead without the tracks having to know about each other. */
const positions = ref<Record<string, number>>({})
const players = new Map<string, HTMLAudioElement>()

/**
 * A ref callback per track, cached so its identity is stable.
 *
 * This used to return a fresh closure on every call, and the template
 * calls it on every render. Vue treats a new ref function as a new
 * binding, so a `timeupdate` — four a second, per playing track — tore
 * down and re-seated *every* track's ref, rebuilt every `<li>`, every
 * four-entry `<dl>` and every `<audio>` vnode on the page. The sibling
 * component (`MultiTrackPlayer`) already cached its binders for exactly
 * this reason and says so; the rule was written down and then not
 * applied one file over.
 */
const binders = new Map<string, (el: unknown) => void>()

function bindPlayer(trackId: string) {
  let existing = binders.get(trackId)
  if (!existing) {
    existing = (el: unknown) => {
      // Duck-typed rather than `instanceof HTMLAudioElement`, so the
      // check holds wherever this runs.
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

const others = computed(() => session.value?.other_participants ?? [])
</script>

<template>
  <div class="mx-auto flex max-w-5xl flex-col gap-4">
    <NuxtLink
      to="/recordings"
      class="text-sm transition-colors hover:underline"
      :style="{ color: 'var(--text-muted)' }"
    >
      ← All recordings
    </NuxtLink>

    <!-- Three blocks, in the three sizes this page is: the metadata card,
         the reader's tags, and the transport. The back link above is real
         and stays put, so the only thing that moves when the recording
         lands is the recording. -->
    <div
      v-if="status === 'pending' && !error"
      aria-busy="true"
      class="flex flex-col gap-4"
    >
      <p class="sr-only">{{ $t('recordings.loadingOne') }}</p>
      <div
        class="h-56 animate-pulse rounded-2xl border motion-reduce:animate-none"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      />
      <div
        class="h-24 animate-pulse rounded-2xl border motion-reduce:animate-none"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      />
      <div
        class="h-72 animate-pulse rounded-2xl border motion-reduce:animate-none"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      />
    </div>

    <div
      v-else-if="missing"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--border)' }"
    >
      <p class="text-sm font-medium">There is no recording here.</p>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        Either it never existed, or it belongs to a session you were not in. Sturnus does not say
        which — the existence of a meeting you were not part of is not something a link should be
        able to confirm.
      </p>
    </div>

    <div
      v-else-if="error"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--danger)' }"
    >
      <p class="text-sm font-medium">This recording could not be loaded.</p>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        Nothing was lost; the recording is on the server either way.
      </p>
      <!-- Disabled rather than replaced while the retry runs: the button
           that started it is the one that would vanish, and a control
           that unmounts itself when pressed drops the keyboard to the top
           of the document. -->
      <button
        type="button"
        class="mt-3 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-60"
        :style="{ color: 'var(--action)' }"
        :disabled="status === 'pending'"
        @click="refresh()"
      >
        {{ status === 'pending' ? 'Trying again…' : 'Try again' }}
      </button>
    </div>

    <template v-else-if="session">
      <!-- Metadata: everything about this session that is not audio. -->
      <header
        class="rounded-2xl border p-5"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <h1 class="flex flex-wrap items-center gap-2 text-2xl font-semibold">
          <span>{{ channelLabel(session) }}</span>
          <span
            v-if="isInProgress(session)"
            class="rounded-full px-2 py-0.5 text-xs font-medium"
            :style="{ background: 'var(--positive)', color: 'var(--positive-contrast)' }"
          >
            Recording now
          </span>
        </h1>

        <dl class="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4">
          <div>
            <dt :style="{ color: 'var(--text-muted)' }">Started</dt>
            <dd class="mt-0.5 tabular-nums">{{ formatTimestamp(session.started_at, timeZone) }}</dd>
          </div>
          <div>
            <dt :style="{ color: 'var(--text-muted)' }">Ended</dt>
            <dd class="mt-0.5 tabular-nums">
              {{ session.ended_at ? formatTimestamp(session.ended_at, timeZone) : '—' }}
            </dd>
          </div>
          <div>
            <dt :style="{ color: 'var(--text-muted)' }">Length</dt>
            <dd class="mt-0.5 tabular-nums">
              {{ length !== null ? formatSeconds(length) : 'unknown' }}
            </dd>
          </div>
          <div>
            <dt :style="{ color: 'var(--text-muted)' }">Recorded speakers</dt>
            <dd class="mt-0.5 tabular-nums">{{ session.tracks.length }}</dd>
          </div>
        </dl>

        <div class="mt-4 flex flex-wrap items-center gap-3">
          <a
            v-if="hasProtocol(session)"
            :href="session.document_url ?? ''"
            target="_blank"
            rel="noreferrer"
            class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
            :style="{ color: 'var(--action)' }"
          >
            Open the protocol ↗
          </a>
          <span v-else class="text-sm" :style="{ color: 'var(--text-muted)' }">
            No protocol was written for this session.
          </span>
        </div>

        <!-- Everybody who was in the channel but has no track: they did
             not consent before it began. Named rather than omitted, because
             "who else was there" is a fact about the meeting and their
             absence from the audio is the point. -->
        <p v-if="others.length > 0" class="mt-4 text-sm" :style="{ color: 'var(--text-muted)' }">
          Also in the channel, without a recording:
          {{ others.map((person) => person.display_name).join(', ') }}
        </p>
      </header>

      <!-- The reader's own labels on this recording. Placed above the
           audio because it is the half of the page somebody returns to a
           recording to *write*, and below the metadata because it is not
           what the recording is. -->
      <RecordingTags :session-id="session.id" :tags="session.tags" />

      <!-- The meeting, on one clock. -->
      <section
        class="rounded-2xl border p-5"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <h2 class="text-base font-semibold">The whole meeting</h2>
        <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
          Every speaker on one transport, which is the only way a conversation makes sense. Solo
          and mute turn tracks down rather than stopping them, so unmuting lands you where
          everybody else already is.
        </p>
        <MultiTrackPlayer :session="session" />
      </section>

      <!-- Second wave: only an administrator of this guild sees anything
           here, and for everybody else the component renders nothing at
           all rather than a disabled control that confirms it exists. -->
      <RequeuePanel :session-id="session.id" />

      <!-- One speaker at a time. -->
      <section
        class="rounded-2xl border p-5"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <h2 class="text-base font-semibold">Each track on its own</h2>
        <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
          For when the question is what one person said rather than what was discussed. Each
          player is independent of the transport above and of every other track here.
        </p>

        <p
          v-if="session.tracks.length === 0"
          class="mt-4 rounded-lg border border-dashed p-4 text-sm"
          :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
        >
          Nobody in this session had consented before it began, so there is no audio.
        </p>

        <ul v-else class="mt-4 flex flex-col gap-4">
          <li
            v-for="track in session.tracks"
            :key="track.discord_user_id"
            class="rounded-xl p-4"
            :style="{ background: 'var(--surface-raised)' }"
          >
            <!-- The four measurements are not repeated here. The
                 transport directly above already shows them per speaker,
                 and rendering them twice was around a hundred redundant
                 nodes on an eight-speaker page and every speaker's
                 numbers read out twice in a row by a screen reader. -->
            <h3 class="text-sm font-medium">{{ trackLabel(track) }}</h3>

            <!-- Named, because eight identical "audio" controls in a row
                 tell a screen-reader user nothing about which speaker
                 they are on. `preload="none"`: nothing is fetched until
                 somebody presses play on this particular speaker. -->
            <audio
              :ref="bindPlayer(track.discord_user_id)"
              :src="audioUrl(base, session.id, track.discord_user_id)"
              class="mt-3 w-full"
              controls
              preload="none"
              :aria-label="`${trackLabel(track)} on their own`"
              @timeupdate="onTime(track.discord_user_id, $event)"
            />

            <TrackSpectrogram
              :session-id="session.id"
              :discord-user-id="track.discord_user_id"
              :position="positions[track.discord_user_id] ?? null"
              @seek="(seconds) => seek(track.discord_user_id, seconds)"
            />
          </li>
        </ul>
      </section>
    </template>
  </div>
</template>
