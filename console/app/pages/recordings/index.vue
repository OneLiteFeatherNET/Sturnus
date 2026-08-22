<script setup lang="ts">
/**
 * A page of the meetings this person was in, and a player for each.
 *
 * The list is the cheap part -- one call, metadata only. Audio is loaded
 * for the session that is open and no other; a page of ten sessions would
 * otherwise pull hundreds of megabytes for somebody who came to click one
 * protocol link.
 *
 * One session is open at a time. Two multi-track players running at once
 * would be two meetings talking over each other through the same speakers,
 * which is precisely the thing this recording format exists to avoid.
 *
 * **A page, and the page number is in the URL.** This used to be the whole
 * history: somebody who has been in three hundred meetings was served
 * three hundred sessions -- each with every participant, every tag and
 * every track inline -- in one body, on every visit, and the page then
 * rendered an article for each. It is now one window at a time, addressed
 * by `?page=`, so that a list somebody has paged into is a place with an
 * address rather than a state that the back button loses.
 *
 * **The rows stay on screen while the next page loads.** Replacing them
 * with a loading box collapses the document from a couple of thousand
 * pixels to seventy, throws away the scroll position and moves whatever
 * was under the pointer -- for a wait that is usually a few tens of
 * milliseconds. `aria-busy` says what is happening for a reader who
 * cannot see the list dim.
 */
import { offsetForPage, PAGE_SIZE, isPastTheEnd, pageCount, pageFromQuery, pageSummary } from '~/utils/paging'
import type { RecordedSession, SessionsResponse } from '~/utils/recordings'

useHead({ title: 'Recordings' })

const route = useRoute()
const api = useApi()

const page = computed(() => pageFromQuery(route.query.page))

const { data, status, error, refresh } = await useAsyncData(
  'recordings',
  () =>
    api<SessionsResponse>(
      `/sessions?limit=${PAGE_SIZE}&offset=${offsetForPage(page.value, PAGE_SIZE)}`,
    ),
  // One key for the list rather than one per page: the payload from the
  // server render is for whichever page was rendered, and a key carrying
  // the page number would make every other page a cache miss that the
  // hydration payload cannot serve anyway.
  { watch: [page] },
)

const sessions = computed<RecordedSession[]>(() => data.value?.sessions ?? [])
const total = computed(() => data.value?.total ?? 0)
const pages = computed(() => pageCount(total.value, PAGE_SIZE))
const summary = computed(() =>
  pageSummary(total.value, data.value?.offset ?? 0, sessions.value.length),
)
/** A bookmark to a page that has since emptied. Not the same as "no
 *  recordings", and saying so would be telling somebody they were never
 *  recorded because their link went stale. */
const beyondTheEnd = computed(() => isPastTheEnd(total.value, sessions.value.length, page.value))
/** The first load, when there is nothing to keep on screen. Every load
 *  after it is a page change, and those keep the rows. */
const loadingFirst = computed(() => status.value === 'pending' && data.value === undefined)
const refreshing = computed(() => status.value === 'pending')

const openId = ref<string | null>(null)

function toggle(id: string) {
  openId.value = openId.value === id ? null : id
}

// Turning a page ends whatever was playing, because the article holding
// the player is about to be replaced by a different meeting's. Clearing
// it here rather than letting the `v-if` do it silently keeps the state
// and the screen agreeing.
watch(page, () => {
  openId.value = null
})

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
      v-if="loadingFirst"
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

    <!-- A page past the end, which is what a bookmark becomes once enough
         recordings have been erased. Distinguished from "no recordings"
         on purpose: telling somebody they were never recorded because
         their link went stale is a wrong answer that reads as data loss. -->
    <div
      v-else-if="beyondTheEnd"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--border)' }"
    >
      <p class="text-sm font-medium">There is nothing on this page.</p>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        You have {{ total }} recordings, and this page is past the end of them.
      </p>
      <NuxtLink
        to="/recordings"
        class="mt-3 inline-block rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ color: 'var(--color-brand-cyan)' }"
      >
        Back to the first page
      </NuxtLink>
    </div>

    <p
      v-else-if="sessions.length === 0"
      class="rounded-2xl border p-6 text-sm"
      :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
    >
      No recordings yet. A session appears here once Sturnus has been in a voice channel with you.
    </p>

    <template v-else>
      <!-- What is on screen and how much is not, in one sentence. In a
           live region because a page change swaps the rows underneath a
           reader who may not be able to see that anything moved. -->
      <p
        class="text-sm tabular-nums"
        :style="{ color: 'var(--text-muted)' }"
        role="status"
        aria-live="polite"
      >
        {{ summary }}
      </p>

      <!-- A list, and said to be one: ten bare articles give assistive
           technology no count and no way to step between them. -->
      <ul class="flex flex-col gap-4" :aria-busy="refreshing">
        <li v-for="session in sessions" :key="session.id">
          <RecordingSession
            :session="session"
            :open="openId === session.id"
            :time-zone="timeZone"
            @toggle="toggle(session.id)"
          />
        </li>
      </ul>

      <RecordingsPager :page="page" :count="pages" />
    </template>
  </div>
</template>
