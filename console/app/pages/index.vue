<script setup lang="ts">
/**
 * What Sturnus holds about the person looking at it.
 *
 * The page renders four states and never blends them. A dashboard that
 * showed zeros while it was still fetching would be telling somebody
 * something false about their own participation, and a dashboard that
 * showed a grid of zeros to somebody who has never been in a recorded
 * meeting would look broken rather than empty.
 *
 * Every figure on it is decided by `~/utils/format`, where the decisions
 * -- an unmeasured total is an absence and never a zero, a skipped track
 * is always confessed -- have tests and no framework around them. What
 * comes back from there is a key and its numbers; `say` turns one into a
 * sentence, and it is the only thing on this page that knows a language.
 */
import {
  describeFailure,
  failureStatus,
  hasNothingRecorded,
  isSessionFailure,
  sessionHighlights,
  summaryFigures,
  type DashboardSummary,
} from '~/utils/format'

const { t } = useI18n()
const say = useSay()

useHead({ title: () => t('nav.dashboard') })

const session = useSession()
const api = useApi()

// `lazy` so a client-side navigation back to this page renders the loading
// state instead of holding the previous page on screen with nothing to
// explain the wait. The first load still resolves during the server render,
// so somebody arriving at the console sees figures rather than a skeleton
// that flashes and vanishes.
const { data, status, error, refresh } = useAsyncData<DashboardSummary>(
  'dashboard',
  async () => {
    try {
      return await api<DashboardSummary>('/dashboard')
    }
    catch (cause) {
      // The hostname leak this used to guard against is now closed one
      // layer down: `useApi` throws only an `ApiError`, which carries a
      // status and a relative path and nothing else. This catch survives
      // for a different job -- turning that into a Nuxt error with a
      // status code, so a failed dashboard renders as the failure it is
      // rather than as an empty page.
      //
      // The status is all it carries. It used to carry an English sentence
      // as well, which the template then rendered: an error object is not
      // a place a translation can live, and the status is what
      // `describeFailure` reads anyway -- from this error just as readily
      // as from the one it wraps.
      throw createError({ statusCode: failureStatus(cause) ?? undefined })
    }
  },
  { lazy: true },
)

const loading = computed(() => status.value === 'pending' || status.value === 'idle')
const figures = computed(() => (data.value ? summaryFigures(data.value) : []))
const highlights = computed(() => (data.value ? sessionHighlights(data.value) : []))
const nothingRecorded = computed(() => Boolean(data.value && hasNothingRecorded(data.value)))

// What to say about a failure, and what to offer in response, are both
// decisions and both live in `~/utils/format`. The error that reaches here
// is the one thrown above, which carries the status and nothing else --
// which is all `describeFailure` ever read.
const failure = computed(() => (error.value ? describeFailure(error.value) : null))
// A refused session is not worth a retry button: pressing it goes round a
// loop that cannot succeed until somebody signs in again.
const failedSession = computed(() => isSessionFailure(error.value))

// The lead figure is the question the page exists to answer, so it gets the
// hero card and the rest share the grid.
const lead = computed(() => figures.value[0] ?? null)
const rest = computed(() => figures.value.slice(1))
</script>

<template>
  <div class="mx-auto max-w-5xl">
    <h1 class="mb-1 text-2xl font-semibold">{{ $t('dashboard.welcome') }}</h1>
    <p class="mb-8 text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ $t('dashboard.intro') }}
      <span v-if="session" class="whitespace-nowrap">
        {{ $t('dashboard.discordAccount', { id: session.discord_user_id }) }}
      </span>
    </p>

    <!-- Loading. A skeleton of the shape that is coming, not a spinner:
         the layout does not jump when the figures land. -->
    <div v-if="loading" aria-busy="true">
      <p class="sr-only">{{ $t('dashboard.loadingFigures') }}</p>
      <div
        class="mb-4 h-36 animate-pulse rounded-2xl border motion-reduce:animate-none"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      />
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <div
          v-for="n in 4"
          :key="n"
          class="h-28 animate-pulse rounded-2xl border motion-reduce:animate-none"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        />
      </div>
    </div>

    <!-- Failure. Named as a failure and offered a retry, because the one
         thing worse than an error is an empty page that looks deliberate. -->
    <div
      v-else-if="error"
      role="alert"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--danger)', background: 'var(--surface)' }"
    >
      <h2 class="mb-2 text-base font-medium">{{ $t('dashboard.failedHeading') }}</h2>
      <p class="mb-4 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ say({ key: 'dashboard.failedDetail', params: { reason: failure ?? '' } }) }}
      </p>
      <NuxtLink
        v-if="failedSession"
        to="/sign-in"
        class="inline-block rounded-lg border px-4 py-2 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ borderColor: 'var(--border)' }"
      >
        {{ $t('dashboard.signInAgain') }}
      </NuxtLink>
      <button
        v-else
        type="button"
        class="rounded-lg border px-4 py-2 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ borderColor: 'var(--border)' }"
        @click="refresh()"
      >
        {{ $t('error.retry') }}
      </button>
    </div>

    <!-- Nothing recorded yet. What would fill this page, and how to do it. -->
    <div
      v-else-if="nothingRecorded"
      class="rounded-2xl border p-8 text-center"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <div class="mb-4 flex justify-center">
        <SturnusMark :size="48" />
      </div>
      <h2 class="mb-2 text-lg font-medium">{{ $t('dashboard.emptyHeading') }}</h2>
      <p class="mx-auto max-w-md text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('dashboard.emptyNote') }}
      </p>
    </div>

    <template v-else-if="data">
      <!-- The lead figure. Larger than the rest because it is the one
           somebody came to read. -->
      <section
        v-if="lead"
        class="mb-4 overflow-hidden rounded-2xl border"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <div
          class="h-1"
          :style="{
            background:
              'linear-gradient(90deg, var(--color-brand-blue), var(--color-brand-magenta), var(--color-brand-cyan))',
          }"
        />
        <div class="p-6">
          <h2 class="text-sm font-medium" :style="{ color: 'var(--text-muted)' }">
            {{ $t(lead.labelKey) }}
          </h2>
          <p class="mt-2 text-4xl font-semibold tracking-tight tabular-nums sm:text-5xl">
            {{ say(lead.value) }}
          </p>
          <p v-if="lead.note" class="mt-3 text-sm" :style="{ color: 'var(--text-muted)' }">
            {{ say(lead.note) }}
          </p>
        </div>
      </section>

      <dl class="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <div
          v-for="figure in rest"
          :key="figure.key"
          class="rounded-2xl border p-5"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        >
          <dt class="text-sm font-medium" :style="{ color: 'var(--text-muted)' }">
            {{ $t(figure.labelKey) }}
          </dt>
          <dd class="mt-2 text-2xl font-semibold tracking-tight tabular-nums sm:text-3xl">
            {{ say(figure.value) }}
          </dd>
          <p v-if="figure.note" class="mt-2 text-xs" :style="{ color: 'var(--text-muted)' }">
            {{ say(figure.note) }}
          </p>
        </div>
      </dl>

      <section v-if="highlights.length" class="mt-8">
        <h2 class="mb-3 text-sm font-medium" :style="{ color: 'var(--text-muted)' }">
          {{ $t('dashboard.highlightsHeading') }}
        </h2>
        <ul class="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <li
            v-for="highlight in highlights"
            :key="highlight.key"
            class="rounded-2xl border p-5"
            :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
          >
            <p
              class="text-xs font-medium tracking-wide uppercase"
              :style="{ color: 'var(--action)' }"
            >
              {{ $t(highlight.labelKey) }}
            </p>
            <p class="mt-2 font-medium break-words">{{ say(highlight.session.channel) }}</p>
            <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
              {{ say(highlight.session.when) }}
            </p>
            <p class="mt-1 text-sm tabular-nums" :style="{ color: 'var(--text-muted)' }">
              {{ say(highlight.session.duration) }}
            </p>
          </li>
        </ul>
      </section>
    </template>

    <!-- The thing a reader came to do, on the page they land on: their
         consent, and — for an administrator — the settings their server
         changes often. Below the figures rather than above them, so a band
         whose height nobody can predict never moves what is already on
         screen; for somebody who is neither it renders nothing at all.
         Outside the state chain above on purpose: whether Sturnus has
         recorded anybody, and whether their figures loaded, says nothing
         about whether they have a consent to withdraw. -->
    <QuickSettingsBand />
  </div>
</template>
