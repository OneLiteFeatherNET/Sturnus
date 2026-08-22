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
 * Every figure on it is formatted by `~/utils/format`, where the decisions
 * -- an unmeasured total is an em dash and never a zero, a skipped track
 * is always confessed -- have tests and no framework around them.
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

useHead({ title: 'Dashboard' })

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
      throw createError({
        statusCode: failureStatus(cause) ?? undefined,
        statusMessage: 'The dashboard could not be loaded.',
        message: describeFailure(cause),
      })
    }
  },
  { lazy: true },
)

const loading = computed(() => status.value === 'pending' || status.value === 'idle')
const figures = computed(() => (data.value ? summaryFigures(data.value) : []))
const highlights = computed(() => (data.value ? sessionHighlights(data.value) : []))
const nothingRecorded = computed(() => Boolean(data.value && hasNothingRecorded(data.value)))

// What to say about a failure, and what to offer in response, are both
// decisions and both live in `~/utils/format`. The message rendered here is
// the sanitised one thrown above; `describeFailure` stands in only if
// something else ever throws here without one.
const failure = computed(() =>
  error.value ? error.value.message || describeFailure(error.value) : null,
)
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
    <h1 class="mb-1 text-2xl font-semibold">Welcome</h1>
    <p class="mb-8 text-sm" :style="{ color: 'var(--text-muted)' }">
      Everything Sturnus has recorded of you, across every meeting.
      <span v-if="session" class="whitespace-nowrap">Discord {{ session.discord_user_id }}.</span>
    </p>

    <!-- Loading. A skeleton of the shape that is coming, not a spinner:
         the layout does not jump when the figures land. -->
    <div v-if="loading" aria-busy="true">
      <p class="sr-only">Loading your figures.</p>
      <div
        class="mb-4 h-36 animate-pulse rounded-2xl border"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      />
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <div
          v-for="n in 4"
          :key="n"
          class="h-28 animate-pulse rounded-2xl border"
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
      <h2 class="mb-2 text-base font-medium">Your figures could not be loaded.</h2>
      <p class="mb-4 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ failure }} Nothing is lost -- this page only reads, and the recordings behind it are
        unaffected.
      </p>
      <NuxtLink
        v-if="failedSession"
        to="/sign-in"
        class="inline-block rounded-lg border px-4 py-2 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ borderColor: 'var(--border)' }"
      >
        Sign in again
      </NuxtLink>
      <button
        v-else
        type="button"
        class="rounded-lg border px-4 py-2 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ borderColor: 'var(--border)' }"
        @click="refresh()"
      >
        Try again
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
      <h2 class="mb-2 text-lg font-medium">Sturnus has not recorded you yet.</h2>
      <p class="mx-auto max-w-md text-sm" :style="{ color: 'var(--text-muted)' }">
        Join a voice channel Sturnus is recording. Once the meeting ends it transcribes what was
        said, writes the protocol into Outline, and this page fills in: how long you spoke, who
        you spoke with, and which meetings produced a protocol you were part of.
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
            {{ lead.label }}
          </h2>
          <p class="mt-2 text-4xl font-semibold tracking-tight tabular-nums sm:text-5xl">
            {{ lead.value }}
          </p>
          <p v-if="lead.note" class="mt-3 text-sm" :style="{ color: 'var(--text-muted)' }">
            {{ lead.note }}
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
            {{ figure.label }}
          </dt>
          <dd class="mt-2 text-2xl font-semibold tracking-tight tabular-nums sm:text-3xl">
            {{ figure.value }}
          </dd>
          <p v-if="figure.note" class="mt-2 text-xs" :style="{ color: 'var(--text-muted)' }">
            {{ figure.note }}
          </p>
        </div>
      </dl>

      <section v-if="highlights.length" class="mt-8">
        <h2 class="mb-3 text-sm font-medium" :style="{ color: 'var(--text-muted)' }">
          Sessions worth remembering
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
              {{ highlight.label }}
            </p>
            <p class="mt-2 font-medium break-words">{{ highlight.session.channel }}</p>
            <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
              {{ highlight.session.when }}
            </p>
            <p class="mt-1 text-sm tabular-nums" :style="{ color: 'var(--text-muted)' }">
              {{ highlight.session.duration }}
            </p>
          </li>
        </ul>
      </section>
    </template>
  </div>
</template>
