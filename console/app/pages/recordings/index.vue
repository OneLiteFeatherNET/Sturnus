<script setup lang="ts">
/**
 * A page of the meetings this person was in, as a list you can scan.
 *
 * **Nothing plays here.** A row used to expand in place and mount a
 * multi-track player, which made every row a container for a player, a
 * metadata block and three buttons — and made the list a worse list in
 * order to be a mediocre player. Audio, spectrograms, per-track
 * transports and the protocol link all live on the recording's own page,
 * where there is room for them. What is left here is the one job a list
 * has: telling a hundred meetings apart well enough to open the right
 * one. `RecordingRow` argues the cut.
 *
 * **Rows, not cards.** The owner asked for leaner cards; the leanest card
 * is a row. A card is a container, and a container is the right shape for
 * something with parts — this has none any more. One bordered surface
 * with hairline dividers puts a hundred meetings under each other with
 * their dates in a column, which is how a list is read.
 *
 * The list is the cheap part -- one call, metadata only.
 *
 * **A page, and the page number is in the URL.** This used to be the whole
 * history: somebody who has been in three hundred meetings was served
 * three hundred sessions -- each with every participant, every tag and
 * every track inline -- in one body, on every visit, and the page then
 * rendered an article for each. It is now one window at a time, addressed
 * by `?page=`, so that a list somebody has paged into is a place with an
 * address rather than a state that the back button loses.
 *
 * **The filter is in the URL too, and it resets the page.** A filtered
 * list is a place: it can be bookmarked, opened in a second tab, and sent
 * to a colleague as "the retro meetings from August" rather than as
 * instructions. Changing the filter drops `?page=`, because page four of
 * the old list names nothing in the new one -- and landing on an empty
 * page after a search reads as "nothing matched" when it means "you are
 * still on page four".
 *
 * **The rows stay on screen while the next page loads.** Replacing them
 * with a loading box collapses the document from a couple of thousand
 * pixels to seventy, throws away the scroll position and moves whatever
 * was under the pointer -- for a wait that is usually a few tens of
 * milliseconds. `aria-busy` says what is happening for a reader who
 * cannot see the list dim.
 */
import {
  isPastTheEnd,
  offsetForPage,
  PAGE_SIZE,
  pageCount,
  pageFromQuery,
  pageSummary,
} from '~/utils/paging'
import {
  filteredSessionsPath,
  filtersFromQuery,
  filtersToRouteQuery,
  hasActiveFilters,
  type RecordingFilters,
} from '~/utils/recordingFilters'
import type { RecordedSession, SessionsResponse } from '~/utils/recordings'

const { t } = useI18n()
const say = useSay()

useHead({ title: () => t('nav.recordings') })

const route = useRoute()
const api = useApi()

const router = useRouter()

const page = computed(() => pageFromQuery(route.query.page))
const filters = computed(() => filtersFromQuery(route.query))
const filtered = computed(() => hasActiveFilters(filters.value))

const { data, status, error, refresh } = await useAsyncData(
  'recordings',
  () =>
    api<SessionsResponse>(
      filteredSessionsPath(filters.value, PAGE_SIZE, offsetForPage(page.value, PAGE_SIZE)),
    ),
  // One key for the list rather than one per page or per filter: the
  // payload from the server render is for whichever list was rendered,
  // and a key carrying the query would make every other combination a
  // cache miss that the hydration payload cannot serve anyway.
  { watch: [page, filters] },
)

/** Applying a filter is a navigation, so that the address bar and the
 *  list always describe the same thing. `page` is dropped rather than
 *  kept: page four of the old list names nothing in the new one, and
 *  landing on an empty page after a search reads as "nothing matched"
 *  when it means "you are still on page four". */
function apply(next: RecordingFilters) {
  router.push({ path: route.path, query: filtersToRouteQuery(next) })
}

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
      <h1 class="text-2xl font-semibold">{{ $t('nav.recordings') }}</h1>
      <!-- Rule six of this page: say whose voices these are. The listener
           is about to hear colleagues, not a file. -->
      <p class="mt-2 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('recordings.intro') }}
      </p>
    </header>

    <RecordingsFilterBar :filters="filters" :total="total" @apply="apply" />

    <!-- The failure is checked before the loading state, so that pressing
         "Try again" keeps the card — and the button — on screen while the
         retry runs. Checked the other way round, the control that started
         the retry unmounts itself and the keyboard lands on the body. -->
    <div
      v-if="error"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--danger)' }"
    >
      <p class="text-sm font-medium">{{ $t('recordings.failedHeading') }}</p>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('recordings.failedDetail') }}
      </p>
      <button
        type="button"
        class="mt-3 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-60"
        :style="{ color: 'var(--action)' }"
        :disabled="refreshing"
        @click="refresh()"
      >
        {{ refreshing ? $t('recordings.retrying') : $t('error.retry') }}
      </button>
    </div>

    <!-- The first load, and the only one that has nothing to keep on
         screen. Blocks the size of the rows that are coming rather than a
         sentence in a box: the sentence is seventy pixels tall and the
         list is a thousand, so the page grew by an order of magnitude the
         moment it arrived and took whatever was under the pointer with it.

         The sentence survives as the `sr-only` one, because it is what a
         screen reader is told alongside `aria-busy` -- and because "your
         sessions are loading" is more use than a shape nobody can see.

         Six rows rather than the twenty a full page holds. A list
         skeleton cannot reserve a height it does not know, and twenty
         rows of grey for somebody who has been in three meetings is a
         worse lie than six. Six now rather than the four this held when
         a row was a card: a row is about half the height, so six of them
         reserve roughly what four cards did. -->
    <div v-else-if="loadingFirst" aria-busy="true" class="flex flex-col gap-4">
      <p class="sr-only">{{ $t('recordings.loadingList') }}</p>
      <div
        class="h-5 w-64 max-w-full animate-pulse rounded motion-reduce:animate-none"
        :style="{ background: 'var(--surface)' }"
      />
      <ul
        class="overflow-hidden rounded-2xl border"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <li
          v-for="n in 6"
          :key="n"
          class="h-[76px] animate-pulse border-t first:border-t-0 motion-reduce:animate-none"
          :style="{ borderColor: 'var(--border)' }"
        />
      </ul>
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
      <p class="text-sm font-medium">{{ $t('recordings.pastEndHeading') }}</p>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ say({ key: 'recordings.pastEndDetail', params: { count: total } }) }}
      </p>
      <!-- Keeps the filter and drops only the page: somebody who paged
           past the end of a search wants the first page of that search,
           not the first page of everything. -->
      <NuxtLink
        :to="{ path: '/recordings', query: filtersToRouteQuery(filters) }"
        class="mt-3 inline-block rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ color: 'var(--action)' }"
      >
        {{ $t('recordings.backToFirstPage') }}
      </NuxtLink>
    </div>

    <!-- Two different sentences, because they mean different things to
         somebody who was recorded yesterday. A filtered list that says
         "no recordings yet" tells them their meetings are gone. -->
    <div
      v-else-if="sessions.length === 0 && filtered"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--border)' }"
    >
      <p class="text-sm font-medium">{{ $t('recordings.noMatchHeading') }}</p>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('recordings.noMatchDetail') }}
      </p>
      <NuxtLink
        to="/recordings"
        class="mt-3 inline-block rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ color: 'var(--action)' }"
      >
        {{ $t('recordings.showAll') }}
      </NuxtLink>
    </div>

    <p
      v-else-if="sessions.length === 0"
      class="rounded-2xl border p-6 text-sm"
      :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
    >
      {{ $t('recordings.emptyList') }}
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
        {{ say(summary) }}
      </p>

      <!-- A list, and said to be one: twenty bare articles give assistive
           technology no count and no way to step between them.

           One surface with hairline rules rather than twenty cards with
           gaps between them. Twenty rounded borders and nineteen gaps is
           twenty separate objects to look at; a ruled list is one object
           with twenty lines in it, which is what a reader comparing dates
           down a column is actually doing. -->
      <ul
        class="overflow-hidden rounded-2xl border"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        :aria-busy="refreshing"
      >
        <li
          v-for="session in sessions"
          :key="session.id"
          class="border-t first:border-t-0"
          :style="{ borderColor: 'var(--border)' }"
        >
          <RecordingRow :session="session" :time-zone="timeZone" />
        </li>
      </ul>

      <RecordingsPager :page="page" :count="pages" />
    </template>
  </div>
</template>
