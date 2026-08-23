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
import { durationMessage } from '~/utils/duration'
import { dayInstant } from '~/utils/heatmap'
import { axisTicks, layOutDay, summarise, type DaySession, type TimelineBar } from '~/utils/timeline'

const props = defineProps<{
  date: string
  sessions: readonly DaySession[]
}>()

const { d } = useI18n()
const say = useSay()

const bars = computed(() => layOutDay(props.date, props.sessions))
const ticks = computed(() => axisTicks(props.date, 3))
const summary = computed(() => summarise(props.sessions))
const laneCount = computed(() => Math.max(1, ...bars.value.map((bar) => bar.lane + 1)))

const LANE_HEIGHT = 34

/** The viewer's zone, named, so the axis is not silently in "some" zone. An
 *  IANA name is not a word in any language; the fallback is. */
const zone = computed(() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || say({ key: 'calendar.zoneUnknown' })
  } catch {
    return say({ key: 'calendar.zoneUnknown' })
  }
})

/** A time of day in the viewer's zone. The `clock` format is the one shape
 *  in `i18n.config.ts` with no zone pinned to it, which is what this panel
 *  wants and says out loud. */
function clock(at: Date): string {
  return d(at, 'clock')
}

function endOf(bar: TimelineBar): Date | null {
  if (bar.durationSeconds === null) return null
  return new Date(bar.startedAt.getTime() + bar.durationSeconds * 1000)
}

/** The words on the bar: when it started, and where. A time, a dot and a
 *  channel name -- no sentence, and so nothing for a language to reorder. */
function label(bar: TimelineBar): string {
  return `${clock(bar.startedAt)} · ${say(bar.channel)}`
}

/** The whole sentence, for the tooltip and for a screen reader. */
function describe(bar: TimelineBar): string {
  const ended = endOf(bar)
  return say({
    key: 'calendar.barDescribed',
    params: {
      channel: bar.channel,
      span: ended
        ? { key: 'calendar.barSpan', params: { from: clock(bar.startedAt), to: clock(ended) } }
        : { key: 'calendar.barFrom', params: { from: clock(bar.startedAt) } },
      zone: zone.value,
      duration: durationMessage(bar.durationSeconds),
    },
  })
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

/**
 * The paint on one bar.
 *
 * One hue at two strengths -- a fill mixed against the surface and a
 * one-pixel inset edge of the same hue, harder. `color-mix` rather than a
 * pair of hexes is what makes the same definition serve both themes: in
 * light the bar is a pale cyan on white, in dark a deep one on the sunken
 * panel, and in both the edge is the same colour a shade further from the
 * background.
 *
 * **A session whose length was never recorded is hatched, not tinted.**
 * It is drawn at the minimum width, which is otherwise indistinguishable
 * from a two-minute meeting -- and the difference between "two minutes"
 * and "nobody knows" is the whole question somebody opened this panel to
 * ask. The stripes say "this is a mark, not a measurement" without asking
 * anybody to tell two colours apart to read it.
 *
 * This is an inline binding rather than a stylesheet because the values
 * are per-bar and the console keeps its CSS in one file -- see
 * `test/stylesheets.spec.ts`.
 */
function paint(bar: TimelineBar): Record<string, string> {
  if (bar.durationSeconds === null) {
    return {
      background:
        'repeating-linear-gradient(45deg,'
        + ' color-mix(in oklab, var(--color-brand-magenta) 45%, var(--surface)) 0 4px,'
        + ' transparent 4px 8px)',
      boxShadow:
        'inset 0 0 0 1px color-mix(in oklab, var(--color-brand-magenta) 70%, var(--surface))',
      color: 'var(--text)',
    }
  }
  return {
    background: 'color-mix(in oklab, var(--color-brand-cyan) 55%, var(--surface))',
    boxShadow: 'inset 0 0 0 1px color-mix(in oklab, var(--color-brand-cyan) 80%, var(--surface))',
    color: 'var(--text)',
  }
}
</script>

<template>
  <section aria-labelledby="day-heading">
    <!-- One `fullDate`, not a weekday glued to a date: whether a comma goes
         after the weekday is a thing the two languages disagree on, and
         `Intl` already knows the answer for both. -->
    <h2 id="day-heading" tabindex="-1" class="text-lg font-semibold">
      {{ $d(dayInstant(date), 'fullDate') }}
    </h2>
    <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
      <template v-if="summary.sessions === 0">{{ $t('calendar.dayNoRecordings') }}</template>
      <!-- Two sentences rather than one with a parenthesis inside it: the
           counts pluralise separately, and a message chooses its form from
           one number. -->
      <template v-else>
        {{
          say({
            key: 'calendar.daySummary',
            params: {
              count: summary.sessions,
              duration: durationMessage(summary.totalDurationSeconds),
            },
          })
        }}
        <template v-if="summary.unknownDurations > 0">
          {{
            say({
              key: 'calendar.dayUnknownLengths',
              params: { count: summary.unknownDurations },
            })
          }}
        </template>
      </template>
    </p>
    <p class="mt-0.5 text-xs" :style="{ color: 'var(--text-muted)' }">
      {{
        $t('calendar.zoneNote', {
          zone,
          start: clock(ticks[0]!.at),
          end: clock(ticks[ticks.length - 1]!.at),
        })
      }}
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
              class="flex h-7 items-center overflow-hidden rounded-md focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:[outline-color:var(--text)]"
              :style="paint(bar)"
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
