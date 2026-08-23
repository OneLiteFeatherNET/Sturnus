<script setup lang="ts">
/**
 * One field holding tags and words at the same time.
 *
 * The recordings page has a tag list and a search box side by side, and
 * everybody types the whole thing into the search box. This is the one
 * field, and the thing that makes it work is that the line between a chip
 * and free text is in the **value** and not only on the screen: what comes
 * out is `{ chips, text }`, so a caller sends the chips as a filter and the
 * text as a query without parsing anything back out.
 *
 * All of that is `~/utils/uiChipInput`. What this file adds is the one
 * fact that rules need and cannot get without a DOM: **where the caret
 * is**. Backspace takes a chip only when the field is empty *and* the
 * caret is at its front — without the second half, editing the middle of a
 * word deletes the tag behind you and you do not see it go.
 */
import { useDismissable } from '~/composables/useDismissable'
import {
  type ChipValue,
  EMPTY_CHIPS,
  addChips,
  describeChips,
  reduceChipKey,
  removeChip,
  setText,
  suggestionsFor,
} from '~/utils/uiChipInput'

const props = withDefaults(
  defineProps<{
    modelValue?: ChipValue
    /** Tags worth offering. An empty list simply means no suggestions. */
    suggestions?: readonly string[]
    label?: string
    placeholder?: string
    disabled?: boolean
    invalid?: boolean
  }>(),
  {
    modelValue: () => EMPTY_CHIPS,
    suggestions: () => [],
    label: undefined,
    placeholder: undefined,
    disabled: false,
    invalid: false,
  },
)

const emit = defineEmits<{ 'update:modelValue': [ChipValue] }>()

const say = useSay()

const base = useId()
const fieldId = `${base}-field`
const listId = `${base}-suggestions`

const field = ref<HTMLInputElement | null>(null)
const focused = ref(false)

const offered = computed(() => suggestionsFor(props.suggestions, props.modelValue))
const showing = computed(() => focused.value && offered.value.length > 0)

// Only `root` is wanted here: there is nothing to return focus *to*, since
// the field never lost it — the suggestions are opened by typing in it.
const { root } = useDismissable(showing, () => {
  focused.value = false
})

function put(value: ChipValue) {
  if (value !== props.modelValue) emit('update:modelValue', value)
}

function onKeydown(event: KeyboardEvent) {
  const element = event.target as HTMLInputElement
  const caretAtStart = element.selectionStart === 0 && element.selectionEnd === 0
  const outcome = reduceChipKey(props.modelValue, event.key, caretAtStart)
  if (!outcome.handled) return
  event.preventDefault()
  put(outcome.value)
}

function accept(tag: string) {
  put(addChips(props.modelValue, tag))
  // Focus stays in the field: choosing a suggestion is one step of typing,
  // not the end of it.
  void nextTick(() => field.value?.focus())
}

const note = computed(() => describeChips(props.modelValue))
</script>

<template>
  <div ref="root" class="relative">
    <!-- The box is a label rather than a div with a click handler, so
         pressing anywhere in the empty space to its right puts the caret
         in the field — which is what everybody expects of a field that
         has chips in front of it. -->
    <label
      :for="fieldId"
      class="flex flex-wrap items-center gap-1.5 rounded-lg border px-2 py-1.5"
      :style="{
        borderColor: invalid ? 'var(--danger)' : 'var(--control-border)',
        background: 'var(--surface-raised)',
      }"
    >
      <span
        v-for="chip in modelValue.chips"
        :key="chip"
        class="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs"
        :style="{ background: 'var(--surface-sunken)', color: 'var(--text)' }"
      >
        {{ chip }}
        <button
          type="button"
          class="rounded-full px-1 transition-colors"
          :style="{ color: 'var(--text-muted)' }"
          :aria-label="$t('ui.chipInput.remove', { chip })"
          :disabled="disabled"
          @click="put(removeChip(modelValue, chip))"
        >
          ×
        </button>
      </span>

      <input
        :id="fieldId"
        ref="field"
        type="text"
        autocomplete="off"
        role="combobox"
        aria-autocomplete="list"
        :aria-controls="listId"
        :aria-expanded="showing"
        :aria-label="label"
        :aria-describedby="`${base}-note`"
        :aria-invalid="invalid ? 'true' : undefined"
        :placeholder="placeholder ?? $t('ui.chipInput.placeholder')"
        :value="modelValue.text"
        :disabled="disabled"
        class="min-w-32 flex-1 bg-transparent px-1 py-0.5 text-sm outline-none"
        :style="{ color: 'var(--text)' }"
        @input="put(setText(modelValue, ($event.target as HTMLInputElement).value))"
        @keydown="onKeydown"
        @focus="focused = true"
      >
    </label>

    <!-- The distinction this control is built on is invisible to a reader
         who is not looking at it: a row of chips and a caret are, to a
         screen reader, one field with some words in it. -->
    <p :id="`${base}-note`" role="status" aria-live="polite" class="sr-only">{{ say(note) }}</p>

    <ul
      v-if="showing"
      :id="listId"
      role="listbox"
      :aria-label="$t('ui.chipInput.suggestions')"
      class="absolute z-20 mt-1 w-full rounded-xl border p-1 shadow-lg"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <li v-for="tag in offered" :key="tag" role="option" :aria-selected="false">
        <button
          type="button"
          class="w-full rounded-lg px-3 py-1.5 text-left text-sm transition-colors hover:bg-[var(--surface-raised)]"
          :style="{ color: 'var(--text)' }"
          @click="accept(tag)"
        >
          {{ tag }}
        </button>
      </li>
    </ul>
  </div>
</template>
