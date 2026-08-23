<script setup lang="ts">
/**
 * One meeting, as one line of a list somebody is scanning a hundred of.
 *
 * This was a card that expanded in place and mounted a multi-track player,
 * an editor's worth of metadata and three buttons. It is a row, and the
 * cut is the point of it.
 *
 * **What a row has to do is get you to the right meeting.** So it carries
 * the four things that tell two meetings apart — *when*, *where*, *who*
 * and *how long* — plus the two things somebody filters by and would
 * otherwise have to take on trust: whether a protocol exists, and the
 * reader's own labels. Nothing else. Everything a person wants once they
 * have found the meeting is one click away on its own page, which is
 * where it can be given room.
 *
 * **The whole row is the link.** Not a row with an "Open" button in it:
 * a link the width of the list is a hit target nobody misses, it is one
 * stop for the keyboard instead of three, and it means the row can say
 * what it is by behaving like a link — the heading takes the action
 * colour, underlines under the pointer, and a chevron sits at the end
 * where a list of links puts one. The old card looked like a container
 * and behaved like one; the accordion was how you found out it was not.
 *
 * That is also why the protocol is a **word here and a link there**. A
 * link inside a link is not something a browser can express, and the row
 * has one job. "Protocol" / "No protocol" answers the question the filter
 * above asks, in words rather than in a colour, so it is announced and
 * not merely seen.
 *
 * Every decision below is `~/utils/recordings`: what an unnamed channel
 * reads as, how many speakers are named before the row starts counting
 * them, and the day and the clock being two different things.
 */
import {
  channelNaming,
  formatSeconds,
  hasProtocol,
  isInProgress,
  recordingPath,
  sessionLength,
  speakerSummary,
  stampParts,
  type RecordedSession,
} from '~/utils/recordings'

const props = defineProps<{
  session: RecordedSession
  timeZone: string
}>()

const say = useSay()

const channel = computed(() => channelNaming(props.session))
const stamp = computed(() => stampParts(props.session.started_at, props.timeZone))
const length = computed(() => sessionLength(props.session))
const speakers = computed(() => speakerSummary(props.session))
</script>

<template>
  <NuxtLink
    :to="recordingPath(session.id)"
    class="group flex items-start gap-3 px-3 py-3 transition-colors hover:bg-[var(--surface-raised)] focus-visible:bg-[var(--surface-raised)] focus-visible:outline-2 focus-visible:-outline-offset-2 sm:px-4"
    :style="{ outlineColor: 'var(--action)' }"
  >
    <!-- The left edge of the list, and the only column that lines up. A
         hundred rows are scanned down it, so the day is set alone and the
         clock recedes under it: `2026-08-21 14:30` as one string makes the
         eye read eleven characters to find the two it wants. -->
    <time
      :datetime="session.started_at"
      class="w-[5.25rem] shrink-0 text-xs tabular-nums sm:w-24 sm:text-sm"
    >
      <span class="block">{{ stamp.day }}</span>
      <span v-if="stamp.time" class="block" :style="{ color: 'var(--text-muted)' }">
        {{ stamp.time }}
      </span>
    </time>

    <div class="min-w-0 flex-1">
      <h2 class="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm font-semibold sm:text-base">
        <!-- A channel with a name is a link's worth of heading. One
             without is an absence, set in the muted role and in normal
             weight, because eighteen digits promoted to a heading is
             exactly what makes a list unreadable. -->
        <span
          class="min-w-0 truncate group-hover:underline"
          :class="channel.named ? '' : 'font-normal italic'"
          :style="{ color: channel.named ? 'var(--action)' : 'var(--text-muted)' }"
        >{{ say(channel.heading) }}</span>
        <span
          v-if="isInProgress(session)"
          class="shrink-0 rounded-full px-2 py-0.5 text-xs font-medium"
          :style="{ background: 'var(--positive)', color: 'var(--positive-contrast)' }"
        >
          {{ $t('recordings.recordingNow') }}
        </span>
      </h2>

      <!-- The id, kept and demoted. It is the only handle anybody
           debugging a channel that has left the guild actually has, and
           it is not a name, so it does not sit where a name sits. -->
      <p
        v-if="channel.id"
        class="truncate text-xs tabular-nums"
        :style="{ color: 'var(--text-muted)' }"
      >
        {{ channel.id }}
      </p>

      <!-- Who, how long, and whether the document was ever written — one
           wrapping line, so that at 360 px it becomes three and at any
           width above that it stays one. Each part carries a label only a
           screen reader hears: the row is a single link, and its name is
           the only description of it a reader who cannot see the columns
           is given. -->
      <p class="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs sm:text-sm">
        <span class="min-w-0" :style="{ color: 'var(--text-muted)' }">
          <span class="sr-only">{{ $t('recordings.recordedLabel') }} </span>
          {{ say(speakers) }}
        </span>
        <!-- No screen-reader label on this one: `common.durationUnknown`
             already carries the word "length", and prefixing both branches
             would read it out twice on the branch that needs it least. -->
        <span class="tabular-nums" :style="{ color: 'var(--text-muted)' }">
          {{ length === null ? $t('common.durationUnknown') : formatSeconds(length) }}
        </span>
        <span :style="{ color: hasProtocol(session) ? 'var(--text)' : 'var(--text-muted)' }">
          {{ hasProtocol(session) ? $t('recordings.hasProtocol') : $t('recordings.noProtocol') }}
        </span>
      </p>

      <!-- The reader's own labels, read-only. They are here because they
           are what the field above filters by, and a filter whose effect
           is invisible in the results is one nobody can check. -->
      <ul v-if="session.tags.length > 0" class="mt-1 flex flex-wrap gap-1">
        <li class="sr-only">{{ $t('recordings.tagsHeading') }}</li>
        <li
          v-for="tag in session.tags"
          :key="tag"
          class="rounded-full px-2 py-0.5 text-xs"
          :style="{ background: 'var(--surface-sunken)', color: 'var(--text-muted)' }"
        >
          {{ tag }}
        </li>
      </ul>
    </div>

    <!-- Where a list of links puts one. Decorative: the row already says
         in words that it opens the recording. -->
    <svg
      aria-hidden="true"
      class="mt-0.5 shrink-0"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="currentColor"
      :style="{ color: 'var(--text-muted)' }"
    >
      <path d="m9.4 18.4-1.4-1.4 5-5-5-5 1.4-1.4 6.4 6.4-6.4 6.4Z" />
    </svg>
  </NuxtLink>
</template>
