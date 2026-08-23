<script setup lang="ts">
/**
 * The dropdown for a guild with two hundred channels in it.
 *
 * Structurally `UiSelect` with a text field at the top of the popup, and
 * the two files are deliberately not folded into one with a `filterable`
 * flag. They differ in what focus does, in what Escape means, and in
 * which keys belong to the control rather than to a caret — three
 * branches through one template, each of which would be reachable only
 * half the time. Two hundred lines of markup that can be read straight
 * through is cheaper than one file with a mode.
 *
 * What *is* shared is shared: the option arithmetic (`~/utils/uiOption`),
 * the dismissal and focus return (`~/composables/useDismissable`), and
 * every rule about what the filter does (`~/utils/uiCombobox`).
 *
 * Focus lives on the field while the popup is open, which is what makes
 * `aria-autocomplete="list"` truthful, and goes back to the trigger when
 * the popup closes.
 */
import { useDismissable } from '~/composables/useDismissable'
import {
  type ComboboxEvent,
  filterOptions,
  filterSummary,
  initialCombobox,
  reduceCombobox,
} from '~/utils/uiCombobox'
import { type UiOption, optionDomId } from '~/utils/uiOption'
import { chosenOption } from '~/utils/uiSelect'

const props = withDefaults(
  defineProps<{
    options: readonly UiOption[]
    modelValue?: string | null
    label?: string
    placeholder?: string
    /** What the filter field reads when it is empty. */
    filterPlaceholder?: string
    disabled?: boolean
    invalid?: boolean
  }>(),
  {
    modelValue: null,
    label: undefined,
    placeholder: undefined,
    filterPlaceholder: undefined,
    disabled: false,
    invalid: false,
  },
)

const emit = defineEmits<{ 'update:modelValue': [string | null] }>()

const say = useSay()

const base = useId()
const listId = `${base}-list`
const triggerId = `${base}-trigger`
const fieldId = `${base}-filter`

const state = ref(initialCombobox(props.modelValue))
const field = ref<HTMLInputElement | null>(null)

const { root, trigger, returnFocus } = useDismissable(
  computed(() => state.value.open),
  () => send({ kind: 'close' }),
)

function send(event: ComboboxEvent) {
  const wasOpen = state.value.open
  const outcome = reduceCombobox(state.value, props.options, event)
  state.value = outcome.state
  if ('chosen' in outcome) emit('update:modelValue', outcome.chosen ?? null)
  if (outcome.returnFocus) void nextTick(returnFocus)
  // Focus follows the popup: a filter field somebody has to click before
  // they can type in it is a filter field that may as well not be there.
  else if (!wasOpen && outcome.state.open) void nextTick(() => field.value?.focus())
  return outcome
}

watch(
  () => props.modelValue,
  (value) => {
    if (value !== state.value.value) send({ kind: 'sync', value })
  },
)

function onKeydown(event: KeyboardEvent) {
  if (event.ctrlKey || event.metaKey || event.altKey) return
  if (send({ kind: 'key', key: event.key }).handled) event.preventDefault()
}

const visible = computed(() => filterOptions(props.options, state.value.query))
const chosen = computed(() => chosenOption(props.options, state.value.value))
const activeId = computed(() =>
  state.value.open ? optionDomId(listId, state.value.active) : undefined,
)
const summary = computed(() => filterSummary(visible.value.length, state.value.query))
</script>

<template>
  <div ref="root" class="relative">
    <button
      :id="triggerId"
      ref="trigger"
      type="button"
      aria-haspopup="listbox"
      :aria-controls="listId"
      :aria-expanded="state.open"
      :aria-label="label"
      :aria-invalid="invalid ? 'true' : undefined"
      :disabled="disabled"
      class="flex w-full items-center justify-between gap-2 rounded-lg border px-3 py-2 text-left text-sm transition-colors disabled:opacity-40"
      :style="{
        borderColor: invalid ? 'var(--danger)' : 'var(--control-border)',
        background: 'var(--surface-raised)',
        color: 'var(--text)',
      }"
      @click="send({ kind: 'toggle' })"
      @keydown="onKeydown"
    >
      <span class="min-w-0 flex-1">
        <span class="block truncate">
          {{ chosen ? chosen.label : (placeholder ?? $t('ui.select.placeholder')) }}
        </span>
        <span
          v-if="chosen?.detail"
          class="block truncate text-xs"
          :style="{ color: 'var(--text-muted)' }"
        >{{ chosen.detail }}</span>
      </span>
      <svg
        aria-hidden="true"
        class="shrink-0"
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="currentColor"
      >
        <path d="M12 15.5 5.5 9l1.4-1.4 5.1 5.1 5.1-5.1L18.5 9 12 15.5Z" />
      </svg>
    </button>

    <div
      v-if="state.open"
      class="absolute z-20 mt-1 w-full rounded-xl border p-1 shadow-lg"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <input
        :id="fieldId"
        ref="field"
        type="text"
        role="combobox"
        autocomplete="off"
        aria-autocomplete="list"
        :aria-controls="listId"
        :aria-expanded="true"
        :aria-activedescendant="activeId"
        :aria-label="label ?? $t('ui.combobox.filter')"
        :placeholder="filterPlaceholder ?? $t('ui.combobox.filter')"
        :value="state.query"
        class="mb-1 w-full rounded-lg border px-3 py-2 text-sm"
        :style="{
          borderColor: 'var(--control-border)',
          background: 'var(--surface-raised)',
          color: 'var(--text)',
        }"
        @input="send({ kind: 'query', query: ($event.target as HTMLInputElement).value })"
        @keydown="onKeydown"
      >

      <!-- What the filter did, for a reader who cannot see the list get
           shorter. A list shrinking from two hundred rows to two is the
           most useful thing this control does and the only part of it a
           sighted reader gets for nothing. -->
      <p role="status" aria-live="polite" class="sr-only">{{ say(summary) }}</p>

      <ul
        :id="listId"
        role="listbox"
        :aria-label="label"
        class="max-h-64 overflow-y-auto"
      >
        <li
          v-if="visible.length === 0"
          class="px-3 py-2 text-sm"
          :style="{ color: 'var(--text-muted)' }"
        >
          {{ say(summary) }}
        </li>

        <li
          v-for="(option, index) in visible"
          :id="optionDomId(listId, index)"
          :key="option.value"
          role="option"
          :aria-selected="option.value === state.value"
          :aria-disabled="option.disabled ? 'true' : undefined"
          class="cursor-pointer rounded-lg px-3 py-2 text-sm"
          :class="option.disabled ? 'cursor-not-allowed opacity-50' : ''"
          :style="{
            background: index === state.active ? 'var(--surface-sunken)' : 'transparent',
            color: 'var(--text)',
          }"
          @click="send({ kind: 'choose', index })"
          @mousemove="send({ kind: 'point', index })"
        >
          <span class="flex items-center justify-between gap-2">
            <span class="min-w-0">
              <span class="block truncate">{{ option.label }}</span>
              <span
                v-if="option.detail"
                class="block truncate text-xs"
                :style="{ color: 'var(--text-muted)' }"
              >{{ option.detail }}</span>
            </span>
            <span
              v-if="option.value === state.value"
              aria-hidden="true"
              class="shrink-0"
              :style="{ color: 'var(--action)' }"
            >✓</span>
          </span>
        </li>
      </ul>
    </div>
  </div>
</template>
