<script setup lang="ts" generic="Row extends UiRow">
/**
 * A list whose rows open, with checkboxes for the actions that apply to
 * more than one of them.
 *
 * The first half is what every list in this console has hand-rolled. The
 * second half is the part with teeth, and all of its arithmetic is in
 * `~/utils/uiDisclosureList`: which rows are ticked, whether the header
 * box is empty, half or full, and — the sentence this exists for — how
 * much of the selection is not on the page the reader is looking at.
 *
 * Two things here genuinely need elements and are therefore here rather
 * than in the module:
 *
 * - **`indeterminate` is a property, not an attribute.** It cannot be
 *   bound in a template at all, so the half-ticked state has to be
 *   written onto the element. A header box that renders empty when two of
 *   twenty rows are ticked turns "clear these two" into "tick the other
 *   eighteen".
 * - **A row's summary and its revealed actions are the caller's markup**,
 *   so they arrive as slots. This component owns the disclosure, not what
 *   is disclosed.
 *
 * It is generic in its row type, and that is not decoration. A caller has
 * more to say about a row than an id — a consent, a person, a job — and a
 * list that narrowed every row to `UiRow` on the way into the slot would
 * force the page to look the row back up by index in a second array. That
 * lookup is exactly how one name ends up beside somebody else's record the
 * first time the two arrays stop being the same length.
 */
import {
  type UiRow,
  bulkStatement,
  headerState,
  isExpanded,
  selectableIds,
  selectionSummary,
  toggleAllOnPage,
  toggleExpanded,
  toggleSelected,
} from '~/utils/uiDisclosureList'

const props = withDefaults(
  defineProps<{
    rows: readonly Row[]
    /** The ids ticked, across every page. Absent means no checkboxes. */
    selected?: readonly string[] | null
    /** The word for the bulk action, which goes into the sentence saying
     *  what it would apply to. */
    bulkAction?: string
    label?: string
  }>(),
  { selected: null, bulkAction: undefined, label: undefined },
)

const emit = defineEmits<{
  'update:selected': [readonly string[]]
  'bulk': [readonly string[]]
}>()

const say = useSay()

const base = useId()
const open = ref<readonly string[]>([])
const header = ref<HTMLInputElement | null>(null)

const pageIds = computed(() => selectableIds(props.rows))
const ticked = computed(() => props.selected ?? [])
const choosing = computed(() => props.selected !== null)
const state = computed(() => headerState(ticked.value, pageIds.value))

// The half-ticked state is a DOM property with no attribute behind it, so
// it is written rather than bound — and written on every change, because
// the browser clears it the moment the box is clicked.
watch(
  [state, header],
  () => {
    if (header.value) header.value.indeterminate = state.value === 'some'
  },
  { immediate: true },
)

const summary = computed(() => selectionSummary(ticked.value, pageIds.value))
const scope = computed(() =>
  props.bulkAction ? bulkStatement(props.bulkAction, ticked.value) : null,
)
</script>

<template>
  <div>
    <!-- The bar exists only when there is something to say. "0 selected"
         beside a button nobody can press is a control that reads as
         broken. -->
    <div
      v-if="choosing"
      class="mb-2 flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface-raised)' }"
    >
      <label class="flex items-center gap-2 text-sm">
        <input
          :id="`${base}-all`"
          ref="header"
          type="checkbox"
          :checked="state === 'all'"
          :disabled="pageIds.length === 0"
          @change="emit('update:selected', toggleAllOnPage(ticked, pageIds))"
        >
        {{ $t('ui.list.selectPage') }}
      </label>

      <span v-if="summary" class="text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ say(summary) }}
      </span>

      <template v-if="scope">
        <!-- The action's own word is in the sentence rather than implied
             by whichever button is nearest, so a second action appearing
             beside the first cannot make it wrong. -->
        <span class="text-sm" :style="{ color: 'var(--text-muted)' }">{{ say(scope) }}</span>
        <button
          type="button"
          class="ml-auto rounded-lg border px-3 py-1.5 text-sm transition-colors"
          :style="{ borderColor: 'var(--danger)', color: 'var(--danger)' }"
          @click="emit('bulk', ticked)"
        >
          {{ bulkAction }}
        </button>
      </template>
    </div>

    <ul :aria-label="label" class="flex flex-col gap-1">
      <li
        v-for="(row, index) in rows"
        :key="row.id"
        class="rounded-xl border"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <div class="flex items-center gap-2 px-3 py-2">
          <input
            v-if="choosing"
            type="checkbox"
            :checked="ticked.includes(row.id)"
            :disabled="row.selectable === false"
            :aria-label="$t('ui.list.selectRow', { row: String(index + 1) })"
            @change="emit('update:selected', toggleSelected(ticked, row.id))"
          >

          <button
            :id="`${base}-summary-${index}`"
            type="button"
            class="flex min-w-0 flex-1 items-center justify-between gap-2 text-left text-sm transition-colors"
            :style="{ color: 'var(--text)' }"
            :aria-expanded="isExpanded(open, row.id)"
            :aria-controls="`${base}-actions-${index}`"
            @click="open = toggleExpanded(open, row.id)"
          >
            <span class="min-w-0"><slot name="row" :row="row" :index="index" /></span>
            <span class="shrink-0 text-xs" :style="{ color: 'var(--text-muted)' }">
              {{ isExpanded(open, row.id) ? $t('ui.list.collapse') : $t('ui.list.expand') }}
            </span>
          </button>
        </div>

        <div
          v-if="isExpanded(open, row.id)"
          :id="`${base}-actions-${index}`"
          class="border-t px-3 py-2"
          :style="{ borderColor: 'var(--border)' }"
        >
          <slot name="actions" :row="row" :index="index" />
        </div>
      </li>

      <li
        v-if="rows.length === 0"
        class="rounded-xl border px-3 py-4 text-sm"
        :style="{
          borderColor: 'var(--border)',
          background: 'var(--surface)',
          color: 'var(--text-muted)',
        }"
      >
        {{ $t('ui.list.empty') }}
      </li>
    </ul>
  </div>
</template>
