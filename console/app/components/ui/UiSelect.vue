<script setup lang="ts">
/**
 * A dropdown in the console's own language.
 *
 * There is almost nothing in this file. Every decision it looks like it is
 * making — which row is highlighted, what a key did, whether a value has
 * been chosen — is `~/utils/uiSelect`'s, and this asks it. What is left
 * here is markup, the token bindings, and the ARIA wiring, which is the
 * one part that genuinely needs elements.
 *
 * The wiring follows the "select-only combobox" pattern: a `button` that
 * carries `role="combobox"`, `aria-expanded` and `aria-controls`, a
 * `listbox` of `option`s, and `aria-activedescendant` naming the
 * highlighted row. Focus stays on the button the whole time — moving it
 * into the list would mean the list has to give it back on every path out,
 * including the ones nobody tested.
 */
import { useDismissable } from '~/composables/useDismissable'
import { type UiOption, optionDomId } from '~/utils/uiOption'
import { type SelectEvent, chosenOption, initialSelect, reduceSelect } from '~/utils/uiSelect'

const props = withDefaults(
  defineProps<{
    options: readonly UiOption[]
    modelValue?: string | null
    /** The accessible name of the control. A visible `<label>` elsewhere
     *  should point at `triggerId` instead. */
    label?: string
    /** What the trigger reads when nothing is chosen. */
    placeholder?: string
    disabled?: boolean
    /** Renders the control as refused and sets `aria-invalid`. The
     *  sentence saying why belongs to the caller, beside the control. */
    invalid?: boolean
  }>(),
  { modelValue: null, label: undefined, placeholder: undefined, disabled: false, invalid: false },
)

const emit = defineEmits<{ 'update:modelValue': [string | null] }>()

const base = useId()
const listId = `${base}-list`
const triggerId = `${base}-trigger`

const state = ref(initialSelect(props.modelValue))

const { root, trigger, returnFocus } = useDismissable(
  computed(() => state.value.open),
  () => send({ kind: 'close' }),
)

function send(event: SelectEvent) {
  const outcome = reduceSelect(state.value, props.options, event)
  state.value = outcome.state
  if ('chosen' in outcome) emit('update:modelValue', outcome.chosen ?? null)
  if (outcome.returnFocus) void nextTick(returnFocus)
  return outcome
}

// A value set from outside — a page resetting its own filter, a stored
// guild id arriving after hydration — is taken without opening anything.
watch(
  () => props.modelValue,
  (value) => {
    if (value !== state.value.value) send({ kind: 'sync', value })
  },
)

function onKeydown(event: KeyboardEvent) {
  // Modified keys belong to the browser: Ctrl+F is a find, not a
  // type-ahead for options beginning with "f".
  if (event.ctrlKey || event.metaKey || event.altKey) return
  if (send({ kind: 'key', key: event.key, at: event.timeStamp }).handled) event.preventDefault()
}

const chosen = computed(() => chosenOption(props.options, state.value.value))
const activeId = computed(() =>
  state.value.open ? optionDomId(listId, state.value.active) : undefined,
)

defineExpose({ triggerId })
</script>

<template>
  <div ref="root" class="relative">
    <button
      :id="triggerId"
      ref="trigger"
      type="button"
      role="combobox"
      :aria-controls="listId"
      :aria-expanded="state.open"
      :aria-activedescendant="activeId"
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
        <!-- The subtext, which on most of these lists is a Discord
             snowflake. Rendered as a subordinate line rather than squeezed
             into the label with a dash, because that is what it is. -->
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

    <ul
      v-if="state.open"
      :id="listId"
      role="listbox"
      :aria-label="label"
      class="absolute z-20 mt-1 max-h-64 w-full overflow-y-auto rounded-xl border p-1 shadow-lg"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <!-- A list with nothing in it says so. An empty popup reads as a
           control that failed rather than as a guild with no channels. -->
      <li
        v-if="options.length === 0"
        class="px-3 py-2 text-sm"
        :style="{ color: 'var(--text-muted)' }"
      >
        {{ $t('ui.select.empty') }}
      </li>

      <li
        v-for="(option, index) in options"
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
          <!-- The chosen row is marked with a glyph as well as with a
               background, because a row distinguished only by its colour
               is not distinguished at all for some readers. -->
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
</template>
