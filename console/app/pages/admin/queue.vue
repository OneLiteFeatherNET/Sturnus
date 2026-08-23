<script setup lang="ts">
/**
 * Where one guild's transcription work stands, and which of it has stopped
 * moving.
 *
 * Everything that is a *decision* -- which row comes first, what a state
 * reads like as a sentence, whether a row needs a person or merely needs
 * time, which caveat travels with which figure, whether an empty page is
 * good news -- lives in `~/utils/queue` and is tested there. What is left
 * in this file is layout, request plumbing and the polling loop. The guild
 * picker is not reimplemented either: it is the same functions the Bot
 * Settings and User Settings pages use, down to the remembered choice, so
 * switching servers on one admin page carries to the others.
 *
 * Three things this page refuses to do, all three on purpose:
 *
 * - **It never presents a derived figure as a fact.** Three of the numbers
 *   here are inferred from something the API process cannot see -- the
 *   worker's real lease, a job's enqueue time that does not exist in the
 *   schema, the size of a list that was cut -- and each one is shown with
 *   the sentence that says so. An administrator who restarts a healthy
 *   worker because a console called it dead has been failed by the
 *   console.
 * - **It offers no re-queue control.** Whether a redo is safe for a given
 *   session is decided once, on the recording page, by `RequeuePanel` and
 *   the endpoint behind it. A second control here would be a second
 *   definition of when a redo is safe, and the two would drift. So every
 *   row links to `/recordings/{id}` and stops there.
 * - **It stops watching when the work does.** Every re-read is a database
 *   read across a whole guild, and a page left open overnight must not
 *   keep making them. Watching is driven by `isQueueMoving`, which reads
 *   `pending` and `running` and deliberately ignores `dead`: a dead job
 *   never changes on its own, so waiting for it would be a page waiting
 *   for ever on news that cannot arrive.
 *
 * **The timer moved to the server.** This page used to ask the API every
 * five seconds and be told "nothing changed" almost every time. It now
 * holds one `EventSource` open and is told when something does. The
 * polling loop is still here and still tested, because it is the fallback:
 * an event stream is the one response an intermediary can break silently,
 * and an administrator behind a proxy that buffers or drops one must still
 * see their queue move. Which of the two is in use is a sentence on the
 * page rather than something to discover in developer tools — "live" and
 * "checking every few seconds" look identical whenever the figures happen
 * not to be changing, which is exactly when somebody is deciding whether
 * to believe them.
 */
import {
  CLEAR_QUEUE_HEADING,
  CLEAR_QUEUE_NOTE,
  LIFECYCLE_SCOPE_NOTE,
  type GuildQueue,
  attentionItems,
  describeQueueError,
  isQueueClear,
  isQueueMoving,
  lifecycleFigures,
  orderQueueSessions,
  parseGuildQueue,
  queueChannelLabel,
  queueChannelNote,
  queuePath,
  queueSessionState,
  queueStreamPath,
  sessionCounts,
  sessionStartLine,
  sessionsSummaryLine,
  startQueuePolling,
  truncationNotice,
} from '~/utils/queue'
import {
  describeQueueMode,
  openQueueStream,
  type QueueStreamHandle,
  type QueueStreamMode,
} from '~/utils/queueStream'
import { recordingPath } from '~/utils/recordings'
import {
  chooseGuild,
  guildLabel,
  parseGuilds,
  readSelectedGuild,
  writeSelectedGuild,
} from '~/utils/settings'

useHead({ title: 'Queue' })

const api = useApi()

const { data: guildData, error: guildError } = await useAsyncData('queue-guilds', async () =>
  parseGuilds(await api('/guilds')),
)

const guilds = computed(() => guildData.value ?? [])

// Server-side there is no browser and therefore no remembered choice, so
// the first render picks the first guild. The remembered one is applied
// after hydration -- the same trade the sidebar and the other two admin
// pages make: a correct first paint for everybody, and one repaint for the
// person who has two guilds and last worked on the second.
const selected = ref<string | null>(chooseGuild(guilds.value, null))

onMounted(() => {
  selected.value = chooseGuild(guilds.value, readSelectedGuild(window.localStorage))
})

function selectGuild(guildId: string) {
  selected.value = guildId
  if (import.meta.client) writeSelectedGuild(window.localStorage, guildId)
}

// The guild the queue belongs to travels *with* the queue rather than in a
// ref of its own. A ref set inside the fetcher would be null after
// hydration -- the server ran the fetch, the client did not -- and the
// figures would vanish on every first paint.
const {
  data: queueData,
  error: queueError,
  status: queueStatus,
  refresh,
} = await useAsyncData(
  'queue-overview',
  async () => {
    const guildId = selected.value
    if (!guildId) return { guildId: null as string | null, queue: null as GuildQueue | null }
    return { guildId, queue: parseGuildQueue(await api(queuePath(guildId))) }
  },
  { watch: [selected] },
)

/** Nothing is shown while the answer on hand belongs to another guild.
 *  Reading one server's backlog under another server's heading is the
 *  exact mistake the switcher exists to prevent, and figures that linger
 *  for a few hundred milliseconds are long enough to be acted on. */
const queue = computed(() =>
  queueData.value && queueData.value.guildId === selected.value ? queueData.value.queue : null,
)

const currentGuild = computed(
  () => guilds.value.find((guild) => guild.id === selected.value) ?? null,
)

const sessions = computed(() => orderQueueSessions(queue.value?.sessions ?? []))

/**
 * The reader's clock, and `null` until there is one.
 *
 * The oldest-pending figure is worth far more as "3 h 20 min ago" than as
 * a bare instant, and an age computed during the server render would
 * differ from the one computed a second later in the browser -- a
 * hydration mismatch on the one paragraph of this page nobody should have
 * reason to distrust. So the first render, on both sides, shows the
 * instant alone, and the age is appended once the page has mounted.
 */
const now = ref<number | null>(null)

const figures = computed(() => (queue.value ? lifecycleFigures(queue.value) : []))
const attention = computed(() => (queue.value ? attentionItems(queue.value, now.value) : []))
const truncation = computed(() => (queue.value ? truncationNotice(queue.value) : null))
const clear = computed(() => Boolean(queue.value && isQueueClear(queue.value)))
const moving = computed(() => Boolean(queue.value && isQueueMoving(queue.value)))

/** How often the queue is re-read **when the live feed is unavailable**.
 *  Five seconds rather than the three `RequeuePanel` uses: that panel is
 *  watched by somebody who has just pressed a button and is waiting for
 *  it, this page is a guild-wide read that somebody leaves open. */
const POLL_MS = 5000

const runtime = useRuntimeConfig()

/** The open feed, or `null` when nothing is being watched. Both this and
 *  the loop below live in `~/utils/queueStream` and `~/utils/queue`, where
 *  they can be driven by a fake source and fake timers; what is left here
 *  is when to start one and when to let it go. */
let stream: QueueStreamHandle | null = null
/** Which guild `stream` is watching, so a re-render does not tear down and
 *  reopen a perfectly good connection every time an event lands on it. */
let watched: string | null = null
/** The fallback loop, or `null` when nothing is scheduled. */
let poll: ReturnType<typeof startQueuePolling> | null = null

/** How the page is keeping itself current, in the reader's words. */
const streamMode = ref<QueueStreamMode>('stopped')

function stopPolling() {
  poll?.stop()
  poll = null
}

function stopWatching() {
  stream?.stop()
  stream = null
  watched = null
  stopPolling()
}

/** The fallback, and it is not optional. An event stream is the one
 *  response an intermediary can break without breaking anything else, and
 *  an administrator behind such a proxy must still see their queue move. */
function fallBackToPolling() {
  stopPolling()
  poll = startQueuePolling({
    // Re-asked each round rather than captured now, because whether there
    // is anything left to watch is a fact about the data that just came
    // back.
    shouldContinue: () => moving.value,
    run: reload,
    delayMs: POLL_MS,
  })
}

function startWatching(guildId: string) {
  stopWatching()
  watched = guildId
  streamMode.value = 'connecting'
  stream = openQueueStream({
    url: `${runtime.public.apiBase}${queueStreamPath(guildId)}`,
    onSnapshot: (payload) => {
      // Guarded against the guild having been switched while an event was
      // in flight. Reading one server's backlog under another server's
      // heading is the exact mistake the switcher exists to prevent.
      if (selected.value !== guildId) return
      queueData.value = { guildId, queue: parseGuildQueue(payload) }
      // The clock moves with the data rather than on a ticker of its own:
      // an age that crept forward beside a figure that had not been
      // re-read would be an age of the wrong thing.
      now.value = Date.now()
    },
    onMode: (mode) => {
      streamMode.value = mode
      if (mode === 'polling') fallBackToPolling()
    },
  })
}

/** Watches while there is work in flight, and stops when there is not. */
function syncWatcher() {
  // Never during a server render: a connection opened there would keep the
  // render alive and would read for a reader who already has their HTML.
  if (!import.meta.client) return
  const guildId = selected.value
  if (!guildId || !moving.value) {
    stopWatching()
    streamMode.value = 'stopped'
    return
  }
  // Already watching the right guild -- including a feed that has since
  // fallen back to polling, which must not be restarted on every event it
  // delivers.
  if (stream && watched === guildId) return
  startWatching(guildId)
}

/** Whether a re-read is in flight. `useAsyncData`'s own status settles
 *  back to `success` while a background refresh runs, which is right for
 *  the page -- the figures on screen stay readable rather than being
 *  replaced by a spinner every five seconds -- and leaves the button with
 *  nothing to report. This ref is that report, and nothing else. */
const refreshing = ref(false)

async function reload() {
  refreshing.value = true
  try {
    await refresh()
  } finally {
    refreshing.value = false
  }
  // The clock is re-read with the data rather than on a ticker of its own:
  // an age that crept forward beside a figure that had not been refetched
  // would be an age of the wrong thing.
  now.value = Date.now()
}

async function refreshNow() {
  await reload()
  syncWatcher()
}

/** The one sentence that says how current these figures are.
 *
 *  Said plainly rather than as a spinner. "Live", "checking every few
 *  seconds" and "not watching at all" look identical whenever the figures
 *  happen not to be changing, and two of those three mean the page is
 *  behind. */
const watchLine = computed(() =>
  moving.value
    ? describeQueueMode(streamMode.value)
    : 'Nothing is queued or running, so this page has stopped reading this server’s queue. Refresh it to check again.',
)

onMounted(() => {
  // The clock arrives here and nowhere earlier. Until it does, the
  // oldest-pending paragraph shows its instant without an age, which is
  // what the server rendered too -- so the two agree and Vue has nothing
  // to report.
  now.value = Date.now()
  syncWatcher()
})

// A guild switched, or the work stopped, changes what there is to watch.
// Both are watched rather than either alone: switching between two busy
// servers never changes `moving`, and a server going quiet never changes
// `selected`.
watch([selected, moving], () => syncWatcher())

onBeforeUnmount(stopWatching)

/** Three tones, three colours. Rendering "a speaker failed for good" and
 *  "a worker has it in hand" in the same grey would hide the one
 *  distinction this page exists to draw. */
const TONE_COLOUR: Record<string, string> = {
  clear: 'var(--color-brand-green)',
  watch: 'var(--color-brand-cyan)',
  alarm: 'var(--color-brand-red)',
}
</script>

<template>
  <div class="max-w-3xl">
    <h1 class="mb-1 text-2xl font-semibold">Queue</h1>
    <p class="mb-6 text-sm" :style="{ color: 'var(--text-muted)' }">
      Where one server's transcription work stands: what is queued, what a worker has in hand, and
      what has stopped moving and will not start again on its own. Nothing here can be re-queued —
      that decision belongs to a single recording, and every row links to it.
    </p>

    <p
      v-if="guildError"
      class="rounded-xl border p-4 text-sm"
      :style="{ borderColor: 'var(--color-brand-red)', background: 'var(--surface)' }"
    >
      {{ describeQueueError(guildError) }}
    </p>

    <!-- Somebody who administers nothing gets the reason and the way in,
         not an empty page. -->
    <section
      v-else-if="guilds.length === 0"
      class="rounded-xl border p-6"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <h2 class="mb-2 text-base font-semibold">There is nothing here for you yet</h2>
      <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
        This section reports the transcription queue of one Discord server, and it is open to the
        administrators of a server where Sturnus is running. You administer none of them right now.
      </p>
      <p class="text-sm" :style="{ color: 'var(--text-muted)' }">
        Administrators are the members holding the Discord role that server names in its
        <code class="rounded bg-[var(--surface-raised)] px-1 font-mono">admin_role_id</code>
        setting. Somebody who already has it can grant you that role — Sturnus mirrors the
        membership from Discord, so the change reaches this console on its own.
      </p>
    </section>

    <template v-else>
      <section
        class="mb-6 rounded-xl border p-4"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <!-- With more than one guild the switcher is the only thing
             standing between an administrator and reading the wrong
             server's backlog, so the current one is named here and its id
             repeated underneath. -->
        <label
          v-if="guilds.length > 1"
          class="mb-2 block text-xs font-medium uppercase tracking-wide"
          :style="{ color: 'var(--text-muted)' }"
          for="guild-switcher"
        >
          Which server
        </label>
        <select
          v-if="guilds.length > 1"
          id="guild-switcher"
          class="w-full rounded-lg border px-3 py-2 text-sm"
          :style="{
            borderColor: 'var(--border)',
            background: 'var(--surface-raised)',
            color: 'var(--text)',
          }"
          :value="selected"
          @change="selectGuild(($event.target as HTMLSelectElement).value)"
        >
          <option v-for="guild in guilds" :key="guild.id" :value="guild.id">
            {{ guildLabel(guild) }}
          </option>
        </select>
        <p v-else class="text-sm">
          <span :style="{ color: 'var(--text-muted)' }">Showing</span>
          <span class="ml-1 font-medium">{{ currentGuild ? guildLabel(currentGuild) : '—' }}</span>
        </p>
        <p v-if="currentGuild" class="mt-2 text-xs" :style="{ color: 'var(--text-muted)' }">
          Guild ID
          <code class="rounded bg-[var(--surface-raised)] px-1 font-mono">{{ currentGuild.id }}</code>
        </p>
      </section>

      <p
        v-if="queueError"
        class="mb-6 rounded-xl border p-4 text-sm"
        :style="{ borderColor: 'var(--color-brand-red)', background: 'var(--surface)' }"
      >
        {{ describeQueueError(queueError) }}
      </p>

      <template v-else>
        <p v-if="queueStatus === 'pending'" class="text-sm" :style="{ color: 'var(--text-muted)' }">
          Reading this server's queue…
        </p>

        <template v-else-if="queue">
          <div class="mb-6 flex flex-wrap items-center justify-between gap-3">
            <!-- Said plainly rather than as a spinner, and it names which
                 of the two ways of watching is in use: "live" and
                 "checking every few seconds" look identical when the
                 figures happen not to be changing, and one of them is
                 several seconds behind. In a live region because it
                 changes on its own, without anything on the page having
                 been pressed. -->
            <p
              class="text-xs"
              :style="{ color: 'var(--text-muted)' }"
              role="status"
              aria-live="polite"
            >
              {{ watchLine }}
            </p>
            <button
              type="button"
              class="shrink-0 rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-40"
              :style="{ borderColor: 'var(--border)' }"
              :disabled="refreshing"
              @click="refreshNow()"
            >
              {{ refreshing ? 'Reading…' : 'Refresh' }}
            </button>
          </div>

          <!-- The four lifecycle counts, in the order a job moves through
               them, and what they are counting. Reading them as a sum of
               the sessions below is the mistake the note under them
               exists to prevent. -->
          <section
            class="mb-6 rounded-xl border p-4"
            :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
          >
            <h2 class="mb-3 text-sm font-semibold">Jobs in this server</h2>
            <dl class="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <div v-for="figure in figures" :key="figure.key">
                <dt class="text-xs font-medium uppercase tracking-wide" :style="{ color: 'var(--text-muted)' }">
                  {{ figure.label }}
                </dt>
                <dd
                  class="text-2xl font-semibold tabular-nums"
                  :style="{ color: TONE_COLOUR[figure.tone] }"
                >
                  {{ figure.value }}
                </dd>
                <dd class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
                  {{ figure.note }}
                </dd>
              </div>
            </dl>
            <p class="mt-4 text-xs" :style="{ color: 'var(--text-muted)' }">
              {{ LIFECYCLE_SCOPE_NOTE }}
            </p>
          </section>

          <!-- Kept visually apart from the four counts above, because they
               are a different kind of number: those describe the pipeline,
               these describe something a person has to do. Each carries
               the caveat that makes it honest, in the panel rather than in
               a footnote — a footnote is read once, by the person who was
               already careful. -->
          <section
            class="mb-8 rounded-xl border p-4"
            :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
          >
            <h2 class="mb-1 text-sm font-semibold">What needs somebody</h2>
            <p class="mb-4 text-xs" :style="{ color: 'var(--text-muted)' }">
              Three figures that no amount of waiting changes. All three are worked out from
              something the API process cannot see directly, so each one says what it was measured
              against.
            </p>
            <div class="flex flex-col gap-4">
              <div
                v-for="item in attention"
                :key="item.key"
                class="rounded-lg border p-3"
                :style="{
                  borderColor: TONE_COLOUR[item.tone],
                  background: 'var(--surface-raised)',
                }"
              >
                <div class="flex flex-wrap items-baseline justify-between gap-2">
                  <span class="text-sm font-medium">{{ item.label }}</span>
                  <span
                    class="text-lg font-semibold tabular-nums"
                    :style="{ color: TONE_COLOUR[item.tone] }"
                  >
                    {{ item.value }}
                  </span>
                </div>
                <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
                  {{ item.detail }}
                </p>
              </div>
            </div>
          </section>

          <!-- A guild with nothing outstanding is the state everybody
               wants and the state that looks most like a broken page, so
               it says which of the two it is. -->
          <section
            v-if="clear"
            class="rounded-xl border p-6"
            :style="{ borderColor: 'var(--color-brand-green)', background: 'var(--surface)' }"
          >
            <h2 class="mb-2 text-base font-semibold">{{ CLEAR_QUEUE_HEADING }}</h2>
            <p class="text-sm" :style="{ color: 'var(--text-muted)' }">{{ CLEAR_QUEUE_NOTE }}</p>
          </section>

          <template v-else>
            <h2 class="mb-1 text-sm font-semibold">Unfinished sessions</h2>
            <p class="mb-2 text-sm" :style="{ color: 'var(--text-muted)' }">
              {{ sessionsSummaryLine(sessions) }}
            </p>
            <!-- Otherwise a page showing twenty sessions reads as "there
                 are twenty", which is the one question a backlog page is
                 opened with. -->
            <p
              v-if="truncation"
              class="mb-4 rounded-lg border p-3 text-xs"
              :style="{ borderColor: 'var(--color-brand-yellow)', background: 'var(--surface)' }"
            >
              {{ truncation }}
            </p>

            <!-- A guild whose figures are not clear but whose list is
                 empty is a real state: the counts above are guild-wide,
                 and a closed session missing its protocol is counted there
                 without necessarily fitting in a cut list. -->
            <p
              v-if="sessions.length === 0"
              class="rounded-xl border p-4 text-sm"
              :style="{
                borderColor: 'var(--border)',
                background: 'var(--surface)',
                color: 'var(--text-muted)',
              }"
            >
              No individual session is listed as unfinished. The figures above are guild-wide, so
              read them rather than this list to see whether anything is outstanding.
            </p>

            <div v-else class="flex flex-col gap-4">
              <article
                v-for="item in sessions"
                :key="item.id"
                class="rounded-xl border p-4"
                :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
              >
                <header
                  class="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between"
                >
                  <div>
                    <h3 class="text-sm font-semibold">{{ queueChannelLabel(item) }}</h3>
                    <p class="text-xs" :style="{ color: 'var(--text-muted)' }">
                      {{ sessionStartLine(item) }}
                    </p>
                  </div>
                  <span
                    class="shrink-0 self-start rounded-full border px-2.5 py-1 text-xs font-medium"
                    :style="{
                      borderColor: TONE_COLOUR[queueSessionState(item).tone],
                      color: TONE_COLOUR[queueSessionState(item).tone],
                    }"
                  >
                    {{ queueSessionState(item).label }}
                  </span>
                </header>

                <!-- The badge's long form is on the row rather than in a
                     tooltip: which of the four states a row is in decides
                     what to do next, and nobody hovers to find that out. -->
                <p class="text-sm" :style="{ color: 'var(--text-muted)' }">
                  {{ queueSessionState(item).detail }}
                </p>

                <p
                  v-if="queueChannelNote(item)"
                  class="mt-2 text-xs"
                  :style="{ color: 'var(--text-muted)' }"
                >
                  {{ queueChannelNote(item) }}
                </p>

                <dl class="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs">
                  <div v-for="count in sessionCounts(item)" :key="count.key">
                    <dt class="inline font-medium">{{ count.label }} ·</dt>
                    <dd class="inline tabular-nums" :style="{ color: 'var(--text-muted)' }">
                      {{ count.value }}
                    </dd>
                  </div>
                </dl>

                <div class="mt-3 flex flex-wrap items-center gap-3">
                  <!-- The only control on a row, and deliberately the only
                       one. Whether a re-queue is safe for this session is
                       decided on the recording page, once. -->
                  <NuxtLink
                    :to="recordingPath(item.id)"
                    class="rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
                    :style="{ borderColor: 'var(--color-brand-cyan)', color: 'var(--color-brand-cyan)' }"
                  >
                    Open this recording
                  </NuxtLink>
                  <span class="text-xs" :style="{ color: 'var(--text-muted)' }">
                    Session status
                    <code class="rounded bg-[var(--surface-raised)] px-1 font-mono">{{
                      item.status || 'unknown'
                    }}</code>
                    — asking for the transcription again is done there, where what the redo would
                    overwrite is on the screen next to the button.
                  </span>
                </div>
              </article>
            </div>
          </template>
        </template>
      </template>
    </template>
  </div>
</template>
