<script setup lang="ts">
/**
 * One unfinished session, as the Queue page lists it.
 *
 * Extracted because the page now lists sessions in two places — the ones
 * with a place in the queue, in the order they will run, and the ones with
 * nothing queued at all — and a row rendered twice from two copies of the
 * same markup is a row that stops agreeing with itself. The first thing to
 * diverge would be the state badge, which is the one thing on the row that
 * decides what somebody does next.
 *
 * **The channel is named the way #141 settled.** A channel whose name has
 * gone is an *absence*, not a name: the heading says there is no name, in
 * the muted role and in normal weight, and the id goes underneath as a
 * subordinate line. Eighteen digits set in semibold read as what the
 * meeting is called, and nobody has a meeting called
 * `Channel 1240377558927872021`. The answer comes from `channelNaming`,
 * which is the console's only answer to that question.
 *
 * The long form of the badge stays on the row rather than behind a
 * tooltip, as it always has: which of the four states a row is in decides
 * what to do next, and nobody hovers to find that out.
 *
 * The `lead` slot is where the queued list puts its move handle. It is a
 * slot rather than a prop because the rows that are *not* in the queue
 * have nothing to put there, and a handle rendered disabled would be an
 * offer of a control that cannot exist — the API sends `priority: null`
 * for exactly those rows and says so.
 */
import { QUEUE_TONE_COLOUR, queueChannelNote, queueSessionState, sessionCounts, sessionStartLine, type QueuedSession } from '~/utils/queue'
import { channelNaming, recordingPath } from '~/utils/recordings'

const props = defineProps<{ session: QueuedSession }>()

const say = useSay()

const channel = computed(() => channelNaming(props.session))
const state = computed(() => queueSessionState(props.session))
</script>

<template>
  <article class="p-4">
    <header class="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
      <div class="flex items-start gap-3">
        <slot name="lead" />
        <div class="min-w-0">
          <!-- Normal weight and the muted role when there is no name, so
               the heading reads as the absence it is. -->
          <h3
            class="text-sm"
            :class="channel.named ? 'font-semibold' : 'font-normal italic'"
            :style="{ color: channel.named ? 'var(--text)' : 'var(--text-muted)' }"
          >
            {{ say(channel.heading) }}
          </h3>
          <p
            v-if="channel.id"
            class="truncate text-xs tabular-nums"
            :style="{ color: 'var(--text-muted)' }"
          >
            {{ channel.id }}
          </p>
          <p class="text-xs" :style="{ color: 'var(--text-muted)' }">
            {{ sessionStartLine(session) }}
          </p>
        </div>
      </div>
      <span
        class="shrink-0 self-start rounded-full border px-2.5 py-1 text-xs font-medium"
        :style="{
          borderColor: QUEUE_TONE_COLOUR[state.tone],
          color: QUEUE_TONE_COLOUR[state.tone],
        }"
      >
        {{ state.label }}
      </span>
    </header>

    <p class="text-sm" :style="{ color: 'var(--text-muted)' }">{{ state.detail }}</p>

    <!-- Why the id is standing in for a name, said once on the row that
         needs it. A recordings row shows the absence and stops there;
         somebody reading a backlog is additionally owed the reason. -->
    <p
      v-if="queueChannelNote(session)"
      class="mt-2 text-xs"
      :style="{ color: 'var(--text-muted)' }"
    >
      {{ queueChannelNote(session) }}
    </p>

    <dl class="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs">
      <div v-for="count in sessionCounts(session)" :key="count.key">
        <dt class="inline font-medium">{{ count.label }} ·</dt>
        <dd class="inline tabular-nums" :style="{ color: 'var(--text-muted)' }">
          {{ count.value }}
        </dd>
      </div>
    </dl>

    <div class="mt-3 flex flex-wrap items-center gap-3">
      <!-- The only link on a row, and deliberately the only control that
           is not about order. Whether a re-queue is safe for this session
           is decided on the recording page, once. -->
      <NuxtLink
        :to="recordingPath(session.id)"
        class="rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ borderColor: 'var(--color-brand-cyan)', color: 'var(--color-brand-cyan)' }"
      >
        Open this recording
      </NuxtLink>
      <span class="text-xs" :style="{ color: 'var(--text-muted)' }">
        Session status
        <code class="rounded bg-[var(--surface-raised)] px-1 font-mono">{{
          session.status || 'unknown'
        }}</code>
        — asking for the transcription again is done there, where what the redo would overwrite is
        on the screen next to the button.
      </span>
    </div>
  </article>
</template>
