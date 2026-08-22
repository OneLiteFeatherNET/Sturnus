<script setup lang="ts">
/**
 * Every meeting this person was in, and a player for each.
 *
 * The list is the cheap part -- one call, metadata only. Audio is loaded
 * for the session that is open and no other; a page of ten sessions would
 * otherwise pull hundreds of megabytes for somebody who came to click one
 * protocol link.
 *
 * One session is open at a time. Two multi-track players running at once
 * would be two meetings talking over each other through the same speakers,
 * which is precisely the thing this recording format exists to avoid.
 */
import type { RecordedSession, SessionsResponse } from '~/utils/recordings'

useHead({ title: 'Recordings' })

const api = useApi()
const { data, status, error, refresh } = await useAsyncData('recordings', () =>
  api<SessionsResponse>('/sessions'),
)

const sessions = computed<RecordedSession[]>(() => data.value?.sessions ?? [])
const openId = ref<string | null>(null)

function toggle(id: string) {
  openId.value = openId.value === id ? null : id
}

/**
 * The zone timestamps are written in.
 *
 * UTC during the server render, the viewer's own zone once there is a
 * browser to ask. Formatting with the resolved zone on both sides would
 * put one time in the HTML and another in the hydrated page -- a mismatch
 * Vue reports in the console and a reader reports as the console being
 * wrong about when their meeting was.
 */
const timeZone = ref('UTC')
onMounted(() => {
  try {
    timeZone.value = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    // Keep UTC. A page that throws while working out a time zone shows
    // nothing at all, which is a poor trade for a nicety.
  }
})
</script>

<template>
  <div class="mx-auto flex max-w-5xl flex-col gap-4">
    <header>
      <h1 class="text-2xl font-semibold">Recordings</h1>
      <!-- Rule six of this page: say whose voices these are. The listener
           is about to hear colleagues, not a file. -->
      <p class="mt-2 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
        These are the meetings you were in. Sturnus records one track per speaker who consented
        before the recording began, and nobody else — anyone in the channel who did not consent is
        listed but has no audio here. This is the material each protocol was transcribed from, so
        when a protocol reads wrong, this is where the answer is.
      </p>
    </header>

    <p
      v-if="status === 'pending'"
      class="rounded-2xl border p-6 text-sm"
      :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
    >
      Loading your sessions…
    </p>

    <div
      v-else-if="error"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--color-brand-red)' }"
    >
      <p class="text-sm font-medium">Your sessions could not be loaded.</p>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        Nothing was lost; the recordings are on the server either way.
      </p>
      <button
        type="button"
        class="mt-3 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ color: 'var(--color-brand-cyan)' }"
        @click="refresh()"
      >
        Try again
      </button>
    </div>

    <p
      v-else-if="sessions.length === 0"
      class="rounded-2xl border p-6 text-sm"
      :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
    >
      No recordings yet. A session appears here once Sturnus has been in a voice channel with you.
    </p>

    <template v-else>
      <RecordingSession
        v-for="session in sessions"
        :key="session.id"
        :session="session"
        :open="openId === session.id"
        :time-zone="timeZone"
        @toggle="toggle(session.id)"
      />
    </template>
  </div>
</template>
