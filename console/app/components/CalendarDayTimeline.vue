<script setup lang="ts">
/**
 * One day's recordings, drawn against the clock.
 *
 * The positions come from `~/utils/timeline`, where they are tested; this
 * file turns fractions into percentages and instants into local labels.
 *
 * **The axis spans a UTC day and is labelled in the viewer's zone.** That
 * combination is deliberate and the panel says so out loud, because the
 * consequence is visible: outside UTC the axis does not begin at the
 * viewer's midnight. `~/utils/timeline` records why the obvious
 * alternative -- a 00:00-to-24:00 local axis -- is worse rather than
 * merely different.
 *
 * The local formatting is also why this component never renders on the
 * server: it appears only after somebody clicks a day, by which time there
 * is a browser with a real timezone. Rendering it during SSR would print
 * the *server's* zone and then rewrite every label on hydration.
 */
import { formatDuration } from '~/utils/duration'
import { formatIsoDate, weekdayOf } from '~/utils/heatmap'
import { axisTicks, layOutDay, summarise, type DaySession, type TimelineBar } from '~/utils/timeline'

const props = defineProps<{
  date: string
  sessions: readonly DaySession[]
}>()

const bars = computed(() => layOutDay(props.date, props.sessions))
const ticks = computed(() => axisTicks(props.date, 3))
const summary = computed(() => summarise(props.sessions))
const laneCount = computed(() => Math.max(1, ...bars.value.map((bar) => bar.lane + 1)))

const LANE_HEIGHT = 34

/** The viewer's zone, named, so the axis is not silently in "some" zone. */
const zone = computed(() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'your local time'
  } catch {
    return 'your local time'
  }
})

function clock(at: Date): string {
  return at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function endOf(bar: TimelineBar): Date | null {
  if (bar.durationSeconds === null) return null
  return new Date(bar.startedAt.getTime() + bar.durationSeconds * 1000)
}

/** The words on the bar: when it started, and where. */
function label(bar: TimelineBar): string {
  return `${clock(bar.startedAt)} · ${bar.channel}`
}

/** The whole sentence, for the tooltip and for a screen reader. */
function describe(bar: TimelineBar): string {
  const ended = endOf(bar)
  const span = ended ? `${clock(bar.startedAt)} to ${clock(ended)}` : `from ${clock(bar.startedAt)}`
  return `${bar.channel}, ${span} (${zone.value}), ${formatDuration(bar.durationSeconds)}.`
}

/**
 * Where the words go.
 *
 * A bar narrower than a tenth of the day has no room for them, and a bar
 * near the right edge would push a trailing label off the panel -- so the
 * label moves inside, after, or before, in that order of preference.
 */
function placement(bar: TimelineBar): 'inside' | 'after' | 'before' {
  if (bar.extent >= 0.1) return 'inside'
  return bar.offset > 0.7 ? 'before' : 'after'
}

function percent(fraction: number): string {
  return `${(fraction * 100).toFixed(4)}%`
}
</script>

<template>
  <section aria-labelledby="day-heading">
    <h2 id="day-heading" tabindex="-1" class="text-lg font-semibold">
      {{ weekdayOf(date) }}, {{ formatIsoDate(date) }}
    </h2>
    <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
      <template v-if="summary.sessions === 0">No recordings on this day.</template>
      <template v-else>
        {{ summary.sessions }} session{{ summary.sessions === 1 ? '' : 's' }},
        {{ formatDuration(summary.totalDurationSeconds) }} in total<template
          v-if="summary.unknownDurations > 0"
        >
          (plus {{ summary.unknownDurations }} whose length was never recorded)</template
        >.
      </template>
    </p>
    <p class="mt-0.5 text-xs" :style="{ color: 'var(--text-muted)' }">
      The API groups days in UTC; the clock below is {{ zone }}, so this day runs from
      {{ clock(ticks[0]!.at) }} to {{ clock(ticks[ticks.length - 1]!.at) }} where you are.
    </p>

    <div v-if="summary.sessions > 0" class="mt-5">
      <!-- The hour marks. Both ends are labelled; the first is pulled left
           and the last right so neither hangs off the panel. -->
      <div
        aria-hidden="true"
        class="relative h-4 text-[11px]"
        :style="{ color: 'var(--text-muted)' }"
      >
        <span
          v-for="(tick, index) in ticks"
          :key="tick.at.toISOString()"
          class="absolute top-0 whitespace-nowrap"
          :style="{
            left: percent(tick.offset),
            transform:
              index === 0
                ? 'none'
                : index === ticks.length - 1
                  ? 'translateX(-100%)'
                  : 'translateX(-50%)',
          }"
        >{{ clock(tick.at) }}</span>
      </div>

      <div
        class="relative rounded-lg border"
        :style="{
          height: `${laneCount * LANE_HEIGHT + 8}px`,
          borderColor: 'var(--border)',
          background: 'var(--surface-sunken)',
        }"
      >
        <span
          v-for="tick in ticks"
          :key="`line-${tick.at.toISOString()}`"
          aria-hidden="true"
          class="absolute top-0 bottom-0 w-px"
          :style="{ left: percent(tick.offset), background: 'var(--border)' }"
        />

        <ol class="absolute inset-x-0 top-1">
          <li
            v-for="bar in bars"
            :key="bar.id"
            class="absolute"
            :style="{ left: percent(bar.offset), width: percent(bar.extent), top: `${bar.lane * LANE_HEIGHT}px` }"
          >
            <div
              class="timeline-bar"
              :class="{ 'is-unknown': bar.durationSeconds === null }"
              tabindex="0"
              :title="describe(bar)"
            >
              <span
                v-if="placement(bar) === 'inside'"
                aria-hidden="true"
                class="truncate px-2 text-xs font-medium"
              >
                {{ label(bar) }}
              </span>
              <span class="sr-only">{{ describe(bar) }}</span>
            </div>
            <span
              v-if="placement(bar) !== 'inside'"
              aria-hidden="true"
              class="absolute top-0 leading-7 whitespace-nowrap text-xs"
              :class="placement(bar) === 'after' ? 'left-full ml-1.5' : 'right-full mr-1.5'"
              :style="{ color: 'var(--text-muted)' }"
            >{{ label(bar) }}</span>
          </li>
        </ol>
      </div>
    </div>
  </section>
</template>

<style scoped>
.timeline-bar {
  display: flex;
  align-items: center;
  height: 28px;
  overflow: hidden;
  border-radius: 6px;
  background: color-mix(in oklab, var(--color-brand-cyan) 55%, var(--surface));
  color: var(--text);
  box-shadow: inset 0 0 0 1px color-mix(in oklab, var(--color-brand-cyan) 80%, var(--surface));
}

.timeline-bar:focus-visible {
  outline: 2px solid var(--text);
  outline-offset: 2px;
}

/* A session whose length was never recorded is drawn at the minimum width,
   which would otherwise be indistinguishable from a two-minute meeting.
   The hatching says "this is a mark, not a measurement" without relying on
   a second colour to carry it. */
.timeline-bar.is-unknown {
  background: repeating-linear-gradient(
    45deg,
    color-mix(in oklab, var(--color-brand-magenta) 45%, var(--surface)) 0 4px,
    transparent 4px 8px
  );
  box-shadow: inset 0 0 0 1px color-mix(in oklab, var(--color-brand-magenta) 70%, var(--surface));
}
</style>
