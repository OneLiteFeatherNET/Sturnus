<script setup lang="ts">
/**
 * A date picker that emits an instant, not a date.
 *
 * The console has no date picker. `/settings` fakes one with a text input,
 * `/recordings` has two bare `date` fields, and `/admin/consents` has a
 * `datetime-local` — and only the last of those can express the thing the
 * API actually wants, which is an ISO-8601 instant carrying an explicit
 * offset. `~/utils/effectiveInstant` worked that out, including the part
 * everybody gets wrong: the offset attached is the offset **of the chosen
 * moment**, so a January instant chosen in July carries January's. This
 * control calls that module and adds nothing to it.
 *
 * **Two ways in, on purpose.** The typed field is a native
 * `datetime-local`, because the browser's own date entry is better than
 * anything written here — it knows the reader's date order, it steps
 * fields with the arrow keys, and it is the control assistive technology
 * already understands. What the browser does *not* do is show a month at
 * a glance in this console's palette, so the calendar beside it is ours.
 * The fake to avoid was never the native input; it was the *text* input
 * pretending to be one.
 *
 * **Two things it can be choosing, and the difference is the API's.** A
 * withdrawal is a moment and needs its offset. A recordings filter is a
 * pair of *inclusive calendar days* — `sturnus.console.filters` says so
 * and parses them with `date.fromisoformat` — and a day has no offset to
 * carry. `granularity="day"` is that: a native `date` field, a value that
 * is the day itself, and no note underneath, because the note exists to
 * show an offset and there is none. It also removes a hydration hazard
 * the instant shape has on a *linkable* filter: the offset is the
 * browser's, a server has a different one, and the two renders would
 * disagree on the very links this page exists to make shareable.
 *
 * Everything the calendar decides — the six-week grid, what an arrow key
 * does to a date, where a month boundary falls — and everything that
 * differs between the two shapes is `~/utils/uiDatePicker`, tested
 * without a document.
 */
import { useDismissable } from '~/composables/useDismissable'
import { WEEKDAY_INSTANTS } from '~/utils/heatmap'
import {
  type Day,
  type Granularity,
  clampDay,
  monthGrid,
  monthInstant,
  monthOf,
  moveDay,
  shapeOf,
  shiftMonth,
} from '~/utils/uiDatePicker'

const props = withDefaults(
  defineProps<{
    /** An ISO-8601 instant with an offset, or a `YYYY-MM-DD` day when
     *  `granularity` says so, or `null`. */
    modelValue?: string | null
    /** Whether this control is choosing a moment or a day. A filter over
     *  inclusive calendar days is not a moment, and saying it is puts an
     *  offset on screen that no request carries. */
    granularity?: Granularity
    label?: string
    /** Bounds, as UTC calendar days. */
    min?: Day | null
    max?: Day | null
    disabled?: boolean
    invalid?: boolean
  }>(),
  {
    modelValue: null,
    granularity: 'instant',
    label: undefined,
    min: null,
    max: null,
    disabled: false,
    invalid: false,
  },
)

const emit = defineEmits<{ 'update:modelValue': [string | null] }>()

const say = useSay()

const base = useId()
const fieldId = `${base}-field`
const gridId = `${base}-grid`

/** What differs between choosing a moment and choosing a day, in one
 *  object. Everything below asks it rather than testing the prop. */
const shape = computed(() => shapeOf(props.granularity))

/** The field value the reader is editing. The model value is derived from
 *  it; the reverse would mean re-deriving a wall clock on every keystroke
 *  and fighting the caret. */
const local = ref(shape.value.toField(props.modelValue))
const open = ref(false)

/** Which day the calendar's keyboard cursor is on. Not the chosen day: a
 *  reader walks the grid before committing, exactly as they walk a
 *  listbox before pressing Enter. */
const today = new Date()
const todayDay = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`
const cursor = ref<Day>(shape.value.dayOf(local.value) ?? todayDay)
const month = ref(monthOf(cursor.value))

const { root, trigger, returnFocus } = useDismissable(open, () => {
  open.value = false
})

watch(
  () => props.modelValue,
  (value) => {
    const next = shape.value.toField(value)
    if (next !== local.value) local.value = next
  },
)

/** One place emits, and it emits a whole value or nothing. A half-typed
 *  one is `null` rather than a naive string: the API answers 400 to those,
 *  and a control that sends one has turned a typo into a server error. */
function publish() {
  emit('update:modelValue', shape.value.toModel(local.value))
}

function onTyped(value: string) {
  local.value = value
  const day = shape.value.dayOf(value)
  if (day) {
    cursor.value = day
    month.value = monthOf(day)
  }
  publish()
}

function choose(day: Day) {
  local.value = shape.value.onDay(local.value, day)
  cursor.value = day
  publish()
  open.value = false
  void nextTick(returnFocus)
}

function openCalendar() {
  open.value = !open.value
  if (!open.value) return
  cursor.value = clampDay(shape.value.dayOf(local.value) ?? todayDay, props.min, props.max)
  month.value = monthOf(cursor.value)
  void nextTick(focusCursor)
}

function focusCursor() {
  root.value?.querySelector<HTMLElement>('[data-cursor="true"]')?.focus()
}

function page(delta: number) {
  month.value = shiftMonth(month.value, delta)
}

function onGridKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    open.value = false
    void nextTick(returnFocus)
    return
  }
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    choose(cursor.value)
    return
  }
  const moved = moveDay(cursor.value, event.key)
  // `null` for every key the grid has no opinion about — Tab above all,
  // because a grid that swallows Tab is a keyboard trap.
  if (moved === null) return
  event.preventDefault()
  cursor.value = clampDay(moved, props.min, props.max)
  month.value = monthOf(cursor.value)
  void nextTick(focusCursor)
}

const chosenDay = computed(() => shape.value.dayOf(local.value))
const weeks = computed(() =>
  monthGrid(month.value, {
    chosen: chosenDay.value,
    today: todayDay,
    min: props.min,
    max: props.max,
  }),
)
const note = computed(() => shape.value.note(local.value))
</script>

<template>
  <div ref="root" class="relative">
    <div class="flex items-center gap-2">
      <input
        :id="fieldId"
        :type="shape.inputType"
        :value="local"
        :aria-label="label"
        :aria-describedby="note ? `${base}-note` : undefined"
        :aria-invalid="invalid ? 'true' : undefined"
        :disabled="disabled"
        class="min-w-0 flex-1 rounded-lg border px-3 py-2 text-sm"
        :style="{
          borderColor: invalid ? 'var(--danger)' : 'var(--control-border)',
          background: 'var(--surface-raised)',
          color: 'var(--text)',
        }"
        @input="onTyped(($event.target as HTMLInputElement).value)"
      >
      <button
        ref="trigger"
        type="button"
        :aria-expanded="open"
        :aria-controls="gridId"
        :aria-label="$t('ui.datePicker.openCalendar')"
        :disabled="disabled"
        class="shrink-0 rounded-lg border px-3 py-2 text-sm transition-colors disabled:opacity-40"
        :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
        @click="openCalendar"
      >
        <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
          <path
            d="M7 2v2H5.5A2.5 2.5 0 0 0 3 6.5v13A2.5 2.5 0 0 0 5.5 22h13a2.5 2.5 0 0 0 2.5-2.5v-13A2.5 2.5 0 0 0 18.5 4H17V2h-2v2H9V2H7Zm12 8v9.5a.5.5 0 0 1-.5.5h-13a.5.5 0 0 1-.5-.5V10h14Z"
          />
        </svg>
      </button>
    </div>

    <!-- The instant itself, quoted. This control's entire reason to exist
         is the offset, so the offset is on screen rather than implied.
         A day has no offset and therefore no note: repeating the field
         back under the field is chrome, and a filter bar carrying two of
         them is a filter bar nobody reads the rest of. -->
    <p v-if="note" :id="`${base}-note`" class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
      {{ say(note) }}
    </p>

    <div
      v-if="open"
      class="absolute z-20 mt-1 rounded-xl border p-3 shadow-lg"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <div class="mb-2 flex items-center justify-between gap-2">
        <button
          type="button"
          class="rounded-lg border px-2 py-1 text-sm transition-colors"
          :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
          :aria-label="$t('ui.datePicker.previousMonth')"
          @click="page(-1)"
        >
          ‹
        </button>
        <!-- `Intl`'s month name, not a table of English words. -->
        <span aria-live="polite" class="text-sm font-semibold">
          {{ $d(monthInstant(month), 'monthYear') }}
        </span>
        <button
          type="button"
          class="rounded-lg border px-2 py-1 text-sm transition-colors"
          :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
          :aria-label="$t('ui.datePicker.nextMonth')"
          @click="page(1)"
        >
          ›
        </button>
      </div>

      <table :id="gridId" role="grid" :aria-label="label ?? $t('ui.datePicker.openCalendar')">
        <thead>
          <tr>
            <!-- Weekday names from an instant apiece, so German reads
                 "Mo" and English "Mon" — the same trick `heatmap.ts`
                 uses, and the same Monday-first week. -->
            <th
              v-for="(at, index) in WEEKDAY_INSTANTS"
              :key="index"
              scope="col"
              class="px-1 pb-1 text-xs font-medium"
              :style="{ color: 'var(--text-muted)' }"
            >
              {{ $d(at, 'weekdayShort') }}
            </th>
          </tr>
        </thead>
        <tbody @keydown="onGridKeydown">
          <tr v-for="(week, index) in weeks" :key="index">
            <td v-for="cell in week" :key="cell.day" role="gridcell" class="p-0.5">
              <button
                type="button"
                :data-cursor="cell.day === cursor ? 'true' : undefined"
                :tabindex="cell.day === cursor ? 0 : -1"
                :aria-selected="cell.chosen"
                :aria-current="cell.today ? 'date' : undefined"
                :disabled="cell.disabled"
                class="h-8 w-8 rounded-lg text-sm transition-colors disabled:opacity-30"
                :style="{
                  background: cell.chosen ? 'var(--action)' : 'transparent',
                  color: cell.chosen
                    ? 'var(--action-contrast)'
                    : cell.inMonth
                      ? 'var(--text)'
                      : 'var(--text-muted)',
                  borderWidth: cell.today ? '1px' : '0',
                  borderColor: 'var(--control-border)',
                }"
                @click="choose(cell.day)"
              >
                {{ cell.dayOfMonth }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
