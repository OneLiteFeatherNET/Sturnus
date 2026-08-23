<script setup lang="ts">
/**
 * A year of meetings at a glance, and one day of them in detail.
 *
 * The two halves answer different questions and are fetched separately for
 * that reason: the year endpoint returns one row per day that had
 * recordings, which is cheap enough to render on the server, while a day's
 * sessions are only worth fetching once somebody asks for a day.
 *
 * ## UTC, and what it costs
 *
 * Both endpoints group by **UTC** day, and this page keeps that grouping
 * rather than pretending otherwise. `~/utils/heatmap` records why the year
 * grid cannot be re-bucketed locally (it returns aggregates, and aggregates
 * do not decompose), and `~/utils/timeline` records why the day axis spans
 * the UTC day even though its labels are local. The visible cost: a meeting
 * at 00:30 in Berlin sits in the previous day's cell, and the day panel's
 * axis starts at 02:00 rather than midnight. Both are stated on screen
 * instead of being quietly wrong.
 */
import type { CalendarDay } from '~/utils/heatmap'
import type { DaySession } from '~/utils/timeline'

const { t } = useI18n()
const say = useSay()

useHead({ title: () => t('nav.calendar') })

interface CalendarYearResponse {
  year: number
  days: CalendarDay[]
}

interface CalendarDayResponse {
  date: string
  sessions: DaySession[]
}

const api = useApi()
const route = useRoute()
const router = useRouter()

/**
 * "This year" decided once, on the server, and carried to the browser.
 *
 * `useState` rather than a plain `new Date()` in setup: a render that
 * straddles New Year, or a browser whose clock disagrees with the cluster's,
 * would otherwise pick one year on the server and another on hydration --
 * and the page would silently refetch a different year than it painted.
 */
const thisYear = useState('calendar-this-year', () => new Date().getUTCFullYear())

/** Sturnus has no recordings before it existed, and none from the future. */
const EARLIEST_YEAR = 2024

function parseYear(raw: unknown): number | null {
  const value = Number(Array.isArray(raw) ? raw[0] : raw)
  if (!Number.isInteger(value)) return null
  if (value < EARLIEST_YEAR || value > thisYear.value) return null
  return value
}

// The year lives in the URL so a link to a year is a link to a year. It is
// read once at setup -- same value on the server and in the browser -- and
// written back on every change.
const year = ref(parseYear(route.query.year) ?? thisYear.value)

const selected = ref<string | null>(null)

const {
  data: yearData,
  status: yearStatus,
  error: yearError,
  refresh: refreshYear,
} = await useAsyncData(
  'calendar-year',
  () => api<CalendarYearResponse>(`/calendar?year=${year.value}`),
  { watch: [year], default: () => null },
)

const {
  data: dayData,
  status: dayStatus,
  error: dayError,
} = await useAsyncData(
  'calendar-day',
  () =>
    selected.value
      ? api<CalendarDayResponse>(`/calendar/${selected.value}`)
      : Promise.resolve(null),
  // Never on the server: nothing is selected during the first render, and
  // the timeline it feeds is labelled in the browser's timezone.
  { immediate: false, watch: [selected], default: () => null },
)

watch(year, (value) => {
  // A day of one year has no meaning in another, and leaving the panel open
  // would show 21 August 2025 under a 2026 heading.
  selected.value = null
  router.replace({ query: { ...route.query, year: String(value) } })
})

const days = computed<CalendarDay[]>(() => yearData.value?.days ?? [])
const yearIsEmpty = computed(() => yearStatus.value === 'success' && days.value.length === 0)

const recordedDays = computed(() => days.value.length)
const totalSessions = computed(() => days.value.reduce((sum, day) => sum + day.sessions, 0))

/**
 * How much the year holds, in one sentence.
 *
 * Two counts in one sentence, and each of them pluralises on its own -- a
 * message can only choose its form from one number, so the two noun phrases
 * are messages of their own and the sentence has a hole for each. That is
 * not the concatenation this convention forbids: the sentence is still one
 * whole string in the locale file, and German is free to put the days
 * before the sessions if it wants to.
 */
const yearSummary = computed(() => ({
  key: 'calendar.yearSummary',
  params: {
    sessions: { key: 'calendar.sessionCount', params: { count: totalSessions.value } },
    days: { key: 'calendar.dayCount', params: { count: recordedDays.value } },
  },
}))

function step(delta: number) {
  const next = year.value + delta
  if (next < EARLIEST_YEAR || next > thisYear.value) return
  year.value = next
}

async function select(date: string) {
  selected.value = date
  // Move focus to the panel that just appeared. Without this the day opens
  // somewhere below a keyboard user's cursor and nothing announces it.
  await nextTick()
  document.getElementById('day-heading')?.focus()
}
</script>

<template>
  <div>
    <h1 class="mb-1 text-2xl font-semibold">{{ $t('nav.calendar') }}</h1>
    <p class="mb-6 text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ $t('calendar.intro') }}
    </p>

    <div class="mb-4 flex flex-wrap items-center gap-3">
      <div class="flex items-center gap-1">
        <button
          type="button"
          class="rounded-lg border px-2.5 py-1.5 text-sm transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-40"
          :style="{ borderColor: 'var(--border)' }"
          :disabled="year <= EARLIEST_YEAR"
          :aria-label="$t('calendar.showYear', { year: String(year - 1) })"
          @click="step(-1)"
        >
          &lsaquo;
        </button>
        <span class="min-w-16 text-center text-lg font-semibold tabular-nums">{{ year }}</span>
        <button
          type="button"
          class="rounded-lg border px-2.5 py-1.5 text-sm transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-40"
          :style="{ borderColor: 'var(--border)' }"
          :disabled="year >= thisYear"
          :aria-label="$t('calendar.showYear', { year: String(year + 1) })"
          @click="step(1)"
        >
          &rsaquo;
        </button>
      </div>
      <p
        v-if="yearStatus === 'success' && recordedDays > 0"
        class="text-sm"
        :style="{ color: 'var(--text-muted)' }"
      >
        {{ say(yearSummary) }}
      </p>
    </div>

    <div
      class="rounded-xl border p-5"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <!-- The heatmap's own box, reserved. Seven rows of 13 px cells at
           3 px spacing plus the month row above them is 137 px, and the
           scroll container adds 4 below; then the legend. Stepping from
           2025 to 2024 used to collapse this panel to one line of text and
           push the day below it up the screen, which is a particularly bad
           reflow to have on a control somebody presses repeatedly. -->
      <div v-if="yearStatus === 'pending'" aria-busy="true">
        <p class="sr-only">{{ $t('calendar.loadingYear', { year }) }}</p>
        <div
          class="h-[141px] animate-pulse rounded-lg motion-reduce:animate-none"
          :style="{ background: 'var(--surface-sunken)' }"
        />
        <div
          class="mt-3 h-4 w-52 max-w-full animate-pulse rounded motion-reduce:animate-none"
          :style="{ background: 'var(--surface-sunken)' }"
        />
      </div>

      <div v-else-if="yearError">
        <p class="text-sm font-medium">
          {{ $t('calendar.yearFailedHeading', { year: String(year) }) }}
        </p>
        <p class="mt-1 mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
          {{ $t('calendar.yearFailedDetail') }}
        </p>
        <button
          type="button"
          class="rounded-lg border px-3 py-1.5 text-sm transition-colors hover:bg-[var(--surface-raised)]"
          :style="{ borderColor: 'var(--border)' }"
          @click="refreshYear()"
        >
          {{ $t('error.retry') }}
        </button>
      </div>

      <!-- An empty year says so, and says what would fill it. A grid of 365
           blank squares with no caption reads as a broken page. -->
      <div v-else-if="yearIsEmpty">
        <p class="text-sm font-medium">
          {{ $t('calendar.emptyYearHeading', { year: String(year) }) }}
        </p>
        <!-- The two endings are a value inside one sentence rather than
             two fragments after it. Which of them applies is this page's
             decision; where in the sentence it lands is the language's. -->
        <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
          {{
            say({
              key: 'calendar.emptyYearDetail',
              params: {
                then: {
                  key:
                    year > EARLIEST_YEAR
                      ? 'calendar.emptyYearOtherYears'
                      : 'calendar.emptyYearWait',
                },
              },
            })
          }}
        </p>
      </div>

      <CalendarHeatmap
        v-else
        :year="year"
        :days="days"
        :selected="selected"
        @select="select"
      />
    </div>

    <div
      v-if="selected"
      class="mt-6 rounded-xl border p-5"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <!-- The day panel in the shape the timeline will fill: its heading,
           its two sentences about sessions and about the timezone, and one
           lane of bars. A day with several overlapping sessions is taller
           than this, and no skeleton can know that before the answer
           arrives -- one lane is the floor rather than a guess. -->
      <div v-if="dayStatus === 'pending'" aria-busy="true">
        <p class="sr-only">{{ $t('calendar.loadingDay', { date: selected }) }}</p>
        <div
          class="h-7 w-56 max-w-full animate-pulse rounded motion-reduce:animate-none"
          :style="{ background: 'var(--surface-sunken)' }"
        />
        <div
          class="mt-1 h-5 w-72 max-w-full animate-pulse rounded motion-reduce:animate-none"
          :style="{ background: 'var(--surface-sunken)' }"
        />
        <div
          class="mt-0.5 h-8 animate-pulse rounded motion-reduce:animate-none"
          :style="{ background: 'var(--surface-sunken)' }"
        />
        <!-- The hour marks, which are four small numbers on a transparent
             row. Reserved rather than drawn: a grey bar here would promise
             something the timeline never puts in this space. -->
        <div class="mt-5 h-4" />
        <div
          class="h-[42px] animate-pulse rounded-lg border motion-reduce:animate-none"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface-sunken)' }"
        />
      </div>
      <p v-else-if="dayError" class="text-sm">
        {{ $t('calendar.dayFailed', { date: selected }) }}
      </p>
      <CalendarDayTimeline
        v-else-if="dayData"
        :date="dayData.date"
        :sessions="dayData.sessions"
      />
    </div>
  </div>
</template>
