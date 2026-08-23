<script setup lang="ts">
/**
 * A destination's credential: written, never read, and honest about both.
 *
 * The API has one route that writes a credential and none that returns
 * one. `ExportTarget` carries `has_secret` and nothing else about it, and
 * `ExportTargetStore.secret_for` — the one method that can produce the
 * value — is deliberately absent from the port the console's handlers can
 * reach. So this control cannot show a credential, and the interface must
 * not imply that it could.
 *
 * **Three properties, and each of them is a failure somewhere else that
 * this file exists to not repeat.**
 *
 * - **No reveal, and no mask either.** A masked value is a value: it says
 *   how long the token is and what its first characters are, and it
 *   promises a "show" button that this API cannot serve. What is on screen
 *   is one sentence — a credential is stored, or none is — and a sentence
 *   saying that there is no way to read it back.
 * - **The input does not exist until somebody asks for it.** An empty
 *   password box rendered beside a configured credential is an invitation
 *   to save the form and silently clear it, which is the exact failure the
 *   API split this onto its own route to prevent. Here the box appears
 *   only after a deliberate press, and closing it discards what was typed.
 *   The destination form beside this one has no credential field at all.
 * - **Clearing is its own act, and it is confirmed.** `{"secret": null}`
 *   is a different request from `{"secret": "…"}`, and it is irreversible
 *   in the strongest sense available: nothing anywhere can read back what
 *   was there to put it back. Two presses, and the second one says what it
 *   costs.
 *
 * The states and the words are `~/utils/exportTargets.secretState`; this
 * file is the markup, the focus and which of the two panels is open.
 */
import { type ExportTarget, canSubmitSecret, secretState } from '~/utils/exportTargets'

const props = withDefaults(
  defineProps<{ target: ExportTarget; busy?: boolean }>(),
  { busy: false },
)

const emit = defineEmits<{ store: [string]; clear: [] }>()

const base = useId()
const state = computed(() => secretState(props.target))

/** Which panel is open. Never both: "type a new credential" and "throw the
 *  old one away" are opposite intentions, and a reader who has one of them
 *  in front of them should not be one mis-click from the other. */
const open = ref<'none' | 'typing' | 'clearing'>('none')
const typed = ref('')
const field = ref<HTMLInputElement | null>(null)

/** What was typed never outlives the panel. There is nothing to come back
 *  to: the value cannot be read back from anywhere, so a half-typed
 *  credential left in a hidden input is only a credential sitting in the
 *  page for longer than anybody meant it to. */
function close() {
  open.value = 'none'
  typed.value = ''
}

// A destination that arrives with a credential newly stored is a different
// destination as far as this control is concerned, so nothing typed
// survives the answer coming back.
watch(() => props.target.id, close)
watch(() => props.target.hasSecret, close)

function startTyping() {
  open.value = 'typing'
  typed.value = ''
  void nextTick(() => field.value?.focus())
}

function store() {
  if (!canSubmitSecret(typed.value) || props.busy) return
  emit('store', typed.value)
}
</script>

<template>
  <section
    class="rounded-lg border p-3"
    :style="{ borderColor: 'var(--border)' }"
  >
    <h4 class="text-sm font-semibold">{{ $t('admin.destinations.secretHeading') }}</h4>

    <!-- What is known about the credential, which is whether there is one.
         Never how long it is, never its first characters, never a row of
         dots standing in for it: a mask is a value, and a value would have
         to have come from somewhere. -->
    <p class="mt-1 text-sm" :style="{ color: 'var(--text)' }">{{ $t(state.statusKey) }}</p>
    <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
      {{ $t('admin.destinations.secretNeverShown') }}
    </p>
    <!-- Said here rather than left to be discovered: none of the three
         formats this deployment publishes reads a stored credential today,
         and a control that let somebody believe it did would be a control
         that quietly does nothing. -->
    <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
      {{ $t('admin.destinations.secretUnused') }}
    </p>

    <div v-if="open === 'none'" class="mt-3 flex flex-wrap items-center gap-2">
      <button
        type="button"
        class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
        :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
        :disabled="busy"
        @click="startTyping"
      >
        {{ $t(state.actionKey) }}
      </button>
      <button
        v-if="state.canClear"
        type="button"
        class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
        :style="{ borderColor: 'var(--danger)', color: 'var(--danger)' }"
        :disabled="busy"
        @click="open = 'clearing'"
      >
        {{ $t('admin.destinations.secretClear') }}
      </button>
    </div>

    <!-- The box, and it exists only because somebody pressed for it. -->
    <form v-else-if="open === 'typing'" class="mt-3" @submit.prevent="store">
      <label class="block text-sm font-medium" :for="`${base}-secret`">
        {{ $t('admin.destinations.secretInputLabel') }}
      </label>
      <input
        :id="`${base}-secret`"
        ref="field"
        v-model="typed"
        type="password"
        autocomplete="new-password"
        spellcheck="false"
        class="mt-1 w-full rounded-lg border px-3 py-2 font-mono text-sm transition-colors"
        :style="{
          borderColor: 'var(--control-border)',
          background: 'var(--surface)',
          color: 'var(--text)',
        }"
      >
      <div class="mt-2 flex flex-wrap items-center gap-2">
        <!-- Refused while empty, because an empty box must not read as a
             way to clear one. The API answers 400 to it as well. -->
        <button
          type="submit"
          class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-60"
          :style="{ background: 'var(--action)', color: 'var(--action-contrast)' }"
          :disabled="busy || !canSubmitSecret(typed)"
        >
          {{ busy ? $t('admin.destinations.secretSaving') : $t('admin.destinations.secretSave') }}
        </button>
        <button
          type="button"
          class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
          :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
          :disabled="busy"
          @click="close"
        >
          {{ $t('admin.destinations.cancel') }}
        </button>
      </div>
    </form>

    <!-- Clearing, confirmed, and told what it costs. -->
    <div
      v-else
      class="mt-3 rounded-lg border p-3"
      :style="{ borderColor: 'var(--danger)' }"
    >
      <p class="text-sm font-semibold">
        {{ $t('admin.destinations.secretClearConfirmHeading') }}
      </p>
      <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
        {{ $t('admin.destinations.secretClearConfirmBody') }}
      </p>
      <div class="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-60"
          :style="{ background: 'var(--danger)', color: 'var(--danger-contrast)' }"
          :disabled="busy"
          @click="emit('clear')"
        >
          {{ $t('admin.destinations.secretClearConfirm') }}
        </button>
        <button
          type="button"
          class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
          :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
          :disabled="busy"
          @click="close"
        >
          {{ $t('admin.destinations.cancel') }}
        </button>
      </div>
    </div>
  </section>
</template>
