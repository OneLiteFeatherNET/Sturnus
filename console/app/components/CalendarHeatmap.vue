<script setup lang="ts">
/**
 * A year of recording as one square per UTC day.
 *
 * Everything interesting -- the grid, the intensity steps, the sentence a
 * cell says, where an arrow key lands -- lives in `~/utils/heatmap`, where
 * it is tested. This file paints the result and handles focus, and that is
 * all it should ever do.
 *
 * Two decisions are visible here rather than there:
 *
 * - **It is a real `<table>`.** A contribution graph is a table: seven rows
 *   of weekdays, one column per week. Native table semantics announce the
 *   month heading and the weekday row for free, where a grid of `div`s with
 *   hand-written ARIA announces whichever axis its author guessed at.
 * - **One cell is in the tab order, not 365.** A roving `tabindex` with
 *   arrow keys is how every date picker works, and it is the difference
 *   between a graph a keyboard user can read and one they have to press Tab
 *   four hundred times to escape.
 */
import {
  WEEKDAY_LABELS,
  buildYearGrid,
  describeCell,
  monthColumns,
  shiftWithinYear,
  type CalendarDay,
  type HeatmapCell,
} from '~/utils/heatmap'

const props = defineProps<{
  year: number
  days: readonly CalendarDay[]
  selected: string | null
}>()

const emit = defineEmits<{ select: [date: string] }>()

const weeks = computed(() => buildYearGrid(props.year, props.days))
const months = computed(() => monthColumns(weeks.value))

/** Only these three weekdays get a visible label, as a contribution graph
 *  conventionally does -- seven labels crowd a 13-pixel row. The other four
 *  rows still carry their full name for a screen reader. */
const VISIBLE_WEEKDAYS = new Set([0, 2, 4])

const firstDay = computed(() => `${props.year}-01-01`)
const lastDay = computed(() => `${props.year}-12-31`)

/**
 * The one cell in the tab order.
 *
 * It follows the selection when there is one, so tabbing back into the
 * grid after opening a day returns to that day rather than to January.
 */
const roving = ref<string>(props.selected ?? props.days[0]?.date ?? firstDay.value)
watch(
  () => [props.year, props.selected] as const,
  ([, selected]) => {
    roving.value = selected ?? props.days[0]?.date ?? firstDay.value
  },
)

const grid = ref<HTMLElement | null>(null)

function moveTo(date: string) {
  roving.value = date
  // The element only carries the new `tabindex` after the patch, so the
  // focus call has to wait for it -- focusing a `tabindex="-1"` button
  // works, but the browser would then have two candidates for "the cell
  // Tab returns to".
  nextTick(() => {
    grid.value?.querySelector<HTMLElement>(`[data-date="${date}"]`)?.focus()
  })
}

const KEY_STEPS: Record<string, number> = {
  ArrowUp: -1,
  ArrowDown: 1,
  ArrowLeft: -7,
  ArrowRight: 7,
  PageUp: -28,
  PageDown: 28,
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Home') {
    event.preventDefault()
    moveTo(firstDay.value)
    return
  }
  if (event.key === 'End') {
    event.preventDefault()
    moveTo(lastDay.value)
    return
  }

  const step = KEY_STEPS[event.key]
  if (step === undefined) return
  event.preventDefault()
  moveTo(shiftWithinYear(roving.value, step, props.year))
}

/**
 * The tooltip.
 *
 * It follows focus as well as the pointer, so the sentence a mouse user
 * gets by hovering is the same one a keyboard user gets by arrowing onto
 * the cell. It is `aria-hidden` because that sentence is already the
 * button's accessible name -- announcing it twice is worse than not
 * showing it.
 *
 * Positioned from `offsetLeft`/`offsetTop` against the wrapper rather than
 * from a measured cell size: the cell size is a stylesheet's business, and
 * a tooltip that drifts when the padding changes is a bug waiting to be
 * filed.
 */
const anchor = ref<HTMLElement | null>(null)
const tip = ref<{ text: string; left: number; top: number } | null>(null)

function showTip(cell: HeatmapCell, event: Event) {
  const target = event.currentTarget as HTMLElement | null
  if (!target || !anchor.value) return
  const width = anchor.value.offsetWidth
  tip.value = {
    text: describeCell(cell),
    // Clamped so a cell in the first or last week does not push the
    // tooltip out of the scrolling area, where it would be clipped.
    left: Math.min(Math.max(target.offsetLeft + target.offsetWidth / 2, 110), Math.max(width - 110, 110)),
    top: target.offsetTop + target.offsetHeight + 8,
  }
}

function hideTip() {
  tip.value = null
}
</script>

<template>
  <div class="heatmap">
    <div class="overflow-x-auto pb-1">
      <div ref="anchor" class="relative inline-block">
        <table
          ref="grid"
          class="heatmap-table border-separate"
          @keydown="onKeydown"
          @mouseleave="hideTip"
        >
          <caption class="sr-only">
            Recording activity for {{ year }}, one cell per UTC day, laid out as weeks. Use the
            arrow keys to move between days and Enter to open one.
          </caption>
          <thead>
            <tr>
              <th class="w-9" />
              <th
                v-for="(month, index) in months"
                :key="`${month.label}-${index}`"
                :colspan="month.span"
                scope="colgroup"
                class="pb-1 text-left text-[11px] font-medium"
                :style="{ color: 'var(--text-muted)' }"
              >
                {{ month.label }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(weekday, row) in WEEKDAY_LABELS" :key="weekday">
              <th scope="row" class="pr-2 text-right align-middle text-[11px] font-normal">
                <span class="sr-only">{{ weekday }}</span>
                <span
                  v-if="VISIBLE_WEEKDAYS.has(row)"
                  aria-hidden="true"
                  :style="{ color: 'var(--text-muted)' }"
                >{{ weekday.slice(0, 3) }}</span>
              </th>
              <td v-for="(week, column) in weeks" :key="`${row}-${column}`" class="p-0">
                <button
                  v-if="week[row]!.date"
                  type="button"
                  class="heat-cell"
                  :class="{ 'is-selected': week[row]!.date === selected }"
                  :data-date="week[row]!.date"
                  :data-intensity="week[row]!.intensity"
                  :tabindex="week[row]!.date === roving ? 0 : -1"
                  :aria-label="describeCell(week[row]!)"
                  :aria-pressed="week[row]!.date === selected"
                  @click="emit('select', week[row]!.date!)"
                  @focus="showTip(week[row]!, $event)"
                  @blur="hideTip"
                  @mouseenter="showTip(week[row]!, $event)"
                />
                <span v-else class="heat-cell is-blank" aria-hidden="true" />
              </td>
            </tr>
          </tbody>
        </table>

        <div
          v-if="tip"
          aria-hidden="true"
          class="pointer-events-none absolute z-10 max-w-[220px] -translate-x-1/2 rounded-lg border px-2.5 py-1.5 text-xs leading-snug shadow-lg"
          :style="{
            left: `${tip.left}px`,
            top: `${tip.top}px`,
            borderColor: 'var(--border)',
            background: 'var(--surface)',
            color: 'var(--text)',
          }"
        >
          {{ tip.text }}
        </div>
      </div>
    </div>

    <!-- The legend names the ends of the scale in words as well as colour,
         for the same reason every cell does. -->
    <div class="mt-3 flex items-center gap-1.5 text-[11px]" :style="{ color: 'var(--text-muted)' }">
      <span>Less recorded</span>
      <span v-for="step in [0, 1, 2, 3, 4]" :key="step" class="heat-cell" :data-intensity="step" />
      <span>More recorded</span>
    </div>
  </div>
</template>

<style scoped>
/*
  One hue at five lightnesses, mixed against the surface it sits on.

  Not a red-to-green ramp: roughly one man in twelve cannot tell those two
  ends apart, and for him the busiest day and the quietest would be the same
  colour. A single hue varying in lightness survives that, and survives a
  greyscale print, and survives a dim screen in sunlight.

  Mixing against `--surface` rather than hard-coding ten hex values is what
  makes one definition serve both themes: in light the ramp runs from nearly
  white down to full cyan, in dark from nearly black up to it. Both are
  monotonic in lightness, which is the property that makes a ramp readable.
*/
.heatmap {
  --heat-edge: color-mix(in oklab, var(--text) 14%, transparent);
  /* The empty step is mixed with the text colour, not with the page, so it
     is a shade lighter than the card in dark and a shade darker in light.
     A day with no recordings has to be visibly a day -- invisible against
     the background it would read as a hole in the grid. */
  --heat-0: color-mix(in oklab, var(--text) 8%, var(--surface));
  --heat-1: color-mix(in oklab, var(--color-brand-cyan) 26%, var(--surface));
  --heat-2: color-mix(in oklab, var(--color-brand-cyan) 50%, var(--surface));
  --heat-3: color-mix(in oklab, var(--color-brand-cyan) 74%, var(--surface));
  --heat-4: var(--color-brand-cyan);
}

.heatmap-table {
  border-spacing: 3px;
}

.heat-cell {
  display: block;
  width: 13px;
  height: 13px;
  border-radius: 3px;
  box-shadow: inset 0 0 0 1px var(--heat-edge);
  background: var(--heat-0);
}

.heat-cell[data-intensity='1'] {
  background: var(--heat-1);
}
.heat-cell[data-intensity='2'] {
  background: var(--heat-2);
}
.heat-cell[data-intensity='3'] {
  background: var(--heat-3);
}
.heat-cell[data-intensity='4'] {
  background: var(--heat-4);
}

/* A padding square stands for no day at all, so it is nothing to look at
   and nothing to reach. */
.heat-cell.is-blank {
  background: transparent;
  box-shadow: none;
}

button.heat-cell:hover {
  box-shadow: inset 0 0 0 1px var(--text-muted);
}

/* Yellow rather than a deeper cyan: the selection has to be distinguishable
   from "a very busy day", and a ring in the same hue as the scale is not. */
button.heat-cell.is-selected {
  outline: 2px solid var(--color-brand-yellow);
  outline-offset: 1px;
}

button.heat-cell:focus-visible {
  outline: 2px solid var(--text);
  outline-offset: 2px;
}
</style>
