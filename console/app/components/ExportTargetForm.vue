<script setup lang="ts">
/**
 * The form that adds a destination, and the one that changes it.
 *
 * One component for both, because they are the same four fields and a
 * second copy would be a second place for the address rules to drift.
 * What differs is stated as two props and nothing else: `mode` decides the
 * heading and whether the name may be typed, and `taken` decides which
 * names collide.
 *
 * **It shows what this format needs, never the union of every field.**
 * Outline wants a collection and offers the picker Bot Settings already
 * has for `document_target`; the two object-store formats want a key
 * prefix, which has no directory to browse and is therefore typed. Which
 * of the two a format wants is `~/utils/exportTargets`' answer, so adding
 * a format is adding an entry there rather than a branch here.
 *
 * **There is no credential field, and that is the point.** A `PUT` on a
 * destination does not touch its secret — the API gives the credential a
 * route of its own precisely so that renaming a destination cannot clear
 * it — so a password box on this form would either lie about what Save
 * does or wipe a token every time somebody corrected a typo.
 * `TargetDraft` has no field for one, so the mistake is unrepresentable
 * rather than merely avoided. `ExportTargetSecret` is the control that
 * does write it.
 *
 * **A name cannot be changed here, because the API cannot change one.**
 * `update_target` reads the name from the stored row and ignores whatever
 * the body says, so that "publish to Wiki" cannot stop meaning what the
 * person who set it up thought it meant. Rendering an editable box over a
 * value that will be discarded is the interface promising something the
 * API refuses, so on an edit the name is read-only and one sentence says
 * what renaming actually is.
 *
 * Every decision above is `~/utils/exportTargets`'; what is left in this
 * file is markup, the token bindings and which complaint is on screen yet.
 */
import UiSelect from '~/components/ui/UiSelect.vue'
import { singleChoices, type NamedRow } from '~/utils/directory'
import {
  type DraftField,
  type TargetDraft,
  draftProblems,
  formatChoices,
  formatSpec,
  problemFor,
} from '~/utils/exportTargets'
import type { UiOption } from '~/utils/uiOption'

const props = withDefaults(
  defineProps<{
    mode: 'add' | 'edit'
    initial: TargetDraft
    /** Names this guild already uses, this destination's own excluded. */
    taken?: readonly string[]
    /** Outline's collections, for the one format that addresses one. */
    collections?: readonly NamedRow[]
    /** The collections could not be read. Decoration failing, not the
     *  form failing: the field falls back to asking for an id. */
    collectionsFailed?: boolean
    busy?: boolean
  }>(),
  { taken: () => [], collections: () => [], collectionsFailed: false, busy: false },
)

const emit = defineEmits<{ submit: [TargetDraft]; cancel: [] }>()

const { t } = useI18n()
const say = useSay()

const base = useId()
const draft = ref<TargetDraft>({ ...props.initial })

/** Whether Save has been pressed on a draft that was not ready. Before
 *  that, only a field somebody has actually touched complains — a form
 *  that objects to an empty name before anybody has typed in it is a form
 *  that opens shouting. */
const attempted = ref(false)
const dirty = ref<Partial<Record<DraftField, boolean>>>({})

/** The reader stepped around the collection picker to type an id. */
const manual = ref(false)

// A form re-opened on a different destination is a different form. Without
// this, closing one row and opening the next would edit the first one's
// values under the second one's heading.
watch(
  () => props.initial,
  (initial) => {
    draft.value = { ...initial }
    attempted.value = false
    dirty.value = {}
    manual.value = false
  },
)

const spec = computed(() => formatSpec(draft.value.format))
const wantsCollection = computed(() => spec.value?.targetKind === 'collection')

/** Whether there is a list to pick from at all. An empty select would say
 *  "this installation has no collections", which is a claim, and the wrong
 *  one when the request merely failed. */
const pickerAvailable = computed(
  () => wantsCollection.value && !props.collectionsFailed && props.collections.length > 0,
)
const picking = computed(() => pickerAvailable.value && !manual.value)

const formatOptions = computed(() => formatChoices(t, props.initial.format))

/** The collections, plus the stored id when the copy has no row for it.
 *  Dropping it would render as though nothing were configured and rewrite
 *  the destination on the next save — `~/utils/directory`'s rule. */
const collectionOptions = computed<UiOption[]>(() =>
  singleChoices(props.collections, draft.value.target).choices.map((choice) => ({
    value: choice.id,
    label: choice.label,
    detail: choice.resolved ? choice.id : t('admin.destinations.unresolvedCollection'),
  })),
)

const problems = computed(() => draftProblems(draft.value, props.taken))
const ready = computed(() => problems.value.length === 0)

/** The complaint about one field, once it is the reader's to see. */
function complaint(field: DraftField) {
  if (!attempted.value && !dirty.value[field]) return null
  return problemFor(problems.value, field)
}

function touch(field: DraftField) {
  dirty.value = { ...dirty.value, [field]: true }
}

/** Switching format changes what the address field means, so a value
 *  typed for the old one is cleared rather than carried into a field it
 *  cannot be valid in. Except on the format the destination arrived with:
 *  going back to it should give the stored address back. */
function chooseFormat(name: string | null) {
  if (name === null) return
  draft.value = {
    ...draft.value,
    format: name,
    target: name === props.initial.format ? props.initial.target : '',
  }
  touch('format')
  manual.value = false
}

function submit() {
  attempted.value = true
  if (!ready.value || props.busy) return
  emit('submit', { ...draft.value })
}
</script>

<template>
  <form
    class="rounded-xl border p-4"
    :style="{ borderColor: 'var(--border)', background: 'var(--surface-raised)' }"
    @submit.prevent="submit"
  >
    <h3 class="text-sm font-semibold">
      {{ mode === 'add' ? $t('admin.destinations.addHeading') : $t('admin.destinations.editHeading') }}
    </h3>

    <div class="mt-4 flex flex-col gap-4">
      <!-- The name. Typed once, on the way in, and read-only afterwards:
           the API keeps the stored name on a PUT whatever the body says. -->
      <div>
        <label class="block text-sm font-medium" :for="`${base}-name`">
          {{ $t('admin.destinations.nameLabel') }}
        </label>
        <input
          :id="`${base}-name`"
          v-model="draft.name"
          type="text"
          :readonly="mode === 'edit'"
          :aria-invalid="complaint('name') ? 'true' : undefined"
          class="mt-1 w-full rounded-lg border px-3 py-2 text-sm transition-colors read-only:opacity-60"
          :style="{
            borderColor: complaint('name') ? 'var(--danger)' : 'var(--control-border)',
            background: 'var(--surface)',
            color: 'var(--text)',
          }"
          @input="touch('name')"
        >
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ mode === 'edit'
            ? $t('admin.destinations.nameLockedNote')
            : $t('admin.destinations.nameHint') }}
        </p>
        <p v-if="complaint('name')" class="mt-1 text-xs" :style="{ color: 'var(--danger)' }">
          {{ say(complaint('name')) }}
        </p>
      </div>

      <!-- The format. Three rows, and a fourth only when this destination
           already stores something else. -->
      <div>
        <span class="block text-sm font-medium">{{ $t('admin.destinations.formatLabel') }}</span>
        <div class="mt-1">
          <UiSelect
            :options="formatOptions"
            :model-value="draft.format"
            :label="$t('admin.destinations.formatLabel')"
            :invalid="Boolean(complaint('format'))"
            @update:model-value="chooseFormat"
          />
        </div>
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('admin.destinations.formatHint') }}
        </p>
        <!-- Where the absent formats are accounted for. One sentence in
             place of two inert rows, so their absence reads as a fact
             about this deployment rather than as a list that failed to
             render — the same answer `video_consent_offered` gets on the
             consent card. -->
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('admin.destinations.formatsNote') }}
        </p>
        <p v-if="complaint('format')" class="mt-1 text-xs" :style="{ color: 'var(--danger)' }">
          {{ say(complaint('format')) }}
        </p>
      </div>

      <!-- Where it writes. One field, and which field it is belongs to the
           format rather than to this template. -->
      <div>
        <label
          v-if="!picking"
          class="block text-sm font-medium"
          :for="`${base}-target`"
        >
          {{ $t(spec?.targetLabelKey ?? 'admin.destinations.prefixLabel') }}
        </label>
        <span v-else class="block text-sm font-medium">
          {{ $t(spec?.targetLabelKey ?? 'admin.destinations.collectionLabel') }}
        </span>

        <div v-if="picking" class="mt-1">
          <UiSelect
            :options="collectionOptions"
            :model-value="draft.target"
            :label="$t('admin.destinations.collectionLabel')"
            :placeholder="$t('admin.destinations.collectionChoose')"
            :invalid="Boolean(complaint('target'))"
            @update:model-value="(id) => { draft.target = id ?? ''; touch('target') }"
          />
        </div>
        <input
          v-else
          :id="`${base}-target`"
          v-model="draft.target"
          type="text"
          spellcheck="false"
          :aria-invalid="complaint('target') ? 'true' : undefined"
          class="mt-1 w-full rounded-lg border px-3 py-2 font-mono text-sm transition-colors"
          :style="{
            borderColor: complaint('target') ? 'var(--danger)' : 'var(--control-border)',
            background: 'var(--surface)',
            color: 'var(--text)',
          }"
          @input="touch('target')"
        >

        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t(spec?.targetHintKey ?? 'admin.destinations.prefixHint') }}
        </p>
        <!-- Why a field that should have been a picker is a text box. The
             list being unreadable must never stop a destination being
             configured: asking for an id is what this always did. -->
        <p
          v-if="wantsCollection && collectionsFailed"
          class="mt-1 text-xs"
          :style="{ color: 'var(--text-muted)' }"
        >
          {{ $t('admin.destinations.collectionsUnavailable') }}
        </p>
        <button
          v-if="wantsCollection && pickerAvailable"
          type="button"
          class="mt-1 text-xs font-medium transition-colors hover:underline"
          :style="{ color: 'var(--action)' }"
          @click="manual = !manual"
        >
          {{ manual
            ? $t('admin.destinations.collectionBackToPicker')
            : $t('admin.destinations.collectionManual') }}
        </button>
        <p v-if="complaint('target')" class="mt-1 text-xs" :style="{ color: 'var(--danger)' }">
          {{ say(complaint('target')) }}
        </p>
      </div>

      <!-- Whether it publishes at all. A checkbox rather than a second
           button, because it is part of what is being saved. -->
      <div>
        <label class="flex items-center gap-2 text-sm">
          <input v-model="draft.enabled" type="checkbox">
          {{ $t('admin.destinations.enabledLabel') }}
        </label>
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('admin.destinations.enabledHint') }}
        </p>
      </div>
    </div>

    <div class="mt-4 flex flex-wrap items-center gap-2">
      <!-- Disabled while the request runs rather than replaced: a control
           that unmounts itself when pressed drops the keyboard to the top
           of the document. -->
      <button
        type="submit"
        class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-60"
        :style="{ background: 'var(--action)', color: 'var(--action-contrast)' }"
        :disabled="busy"
      >
        {{ busy ? $t('admin.destinations.saving') : $t('admin.destinations.save') }}
      </button>
      <button
        type="button"
        class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
        :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
        :disabled="busy"
        @click="emit('cancel')"
      >
        {{ $t('admin.destinations.cancel') }}
      </button>
    </div>
  </form>
</template>
