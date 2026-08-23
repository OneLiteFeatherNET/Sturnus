<script setup lang="ts">
/**
 * A guild's client secret: written, never read, and honest about both.
 *
 * `PUT /api/guilds/{id}/oauth-client/secret` is the only route that writes
 * this value and `DELETE` on the same path is the only one that forgets it.
 * Nothing anywhere returns it: it is wrapped by the master key and bound to
 * the row it sits in — to the guild *and* to the purpose — so a wrapped
 * secret moved into another guild's row, or into that guild's export-target
 * row, fails to authenticate rather than decrypting into somebody else's
 * credential. The console could not show this value if it wanted to, and
 * the interface must not imply that it could.
 *
 * **Three properties, and each of them is a failure somewhere else that
 * this file exists to not repeat.** They are the three `ExportTargetSecret`
 * established for an export credential, restated here rather than shared,
 * because the two controls answer to different routes with different bodies
 * — this one clears with a `DELETE` where that one clears with
 * `{"secret": null}` — and a component parameterised over both would be a
 * component whose reader has to check which API it is looking at.
 *
 * - **No reveal, and no mask either.** A masked value is a value: it says
 *   how long the credential is and what its first characters are, and it
 *   promises a "show" button this API cannot serve. What is on screen is
 *   one sentence — a secret is stored, or none is — and a sentence saying
 *   there is no way to read it back.
 * - **The input does not exist until somebody asks for it.** An empty
 *   password box rendered beside a stored credential is an invitation to
 *   save the form and silently clear it, which is the exact failure the API
 *   split this onto its own route to prevent. Here the box appears only
 *   after a deliberate press, and closing it discards what was typed. The
 *   registration form beside this one has no credential field at all.
 * - **Clearing is its own act, and it is confirmed.** It is irreversible in
 *   the strongest sense available — nothing anywhere can read back what was
 *   there to put it back — and it has a consequence outside this page: the
 *   guild's sign-in link stops working immediately and starts answering
 *   exactly as an unknown one. Two presses, and the second one says what it
 *   costs.
 *
 * **Nothing here logs, echoes or round-trips the value.** It is bound to
 * one `ref`, emitted once, and dropped; there is no watcher on it, no
 * `console` call anywhere in this component, and the `ref` is emptied the
 * moment the panel closes or the answer comes back.
 *
 * The states and the words are `clientSecretState` in
 * `~/utils/oauthClient`; this file is the markup, the focus and which of
 * the two panels is open.
 */
import {
  type GuildOAuthClient,
  MAX_SECRET_LENGTH,
  canSubmitClientSecret,
  clientSecretState,
} from '~/utils/oauthClient'

const props = withDefaults(
  defineProps<{ client: GuildOAuthClient; busy?: boolean }>(),
  { busy: false },
)

const emit = defineEmits<{ store: [string]; clear: [] }>()

const base = useId()
const state = computed(() => clientSecretState(props.client))

/** Which panel is open. Never both: "type a new credential" and "throw the
 *  old one away" are opposite intentions, and a reader who has one of them
 *  in front of them should not be one mis-click from the other. */
const open = ref<'none' | 'typing' | 'clearing'>('none')
const typed = ref('')
const field = ref<HTMLInputElement | null>(null)

/** What was typed never outlives the panel. There is nothing to come back
 *  to — the value cannot be read back from anywhere — so a half-typed
 *  credential left in a hidden input is only a credential sitting in the
 *  page for longer than anybody meant it to. */
function close() {
  open.value = 'none'
  typed.value = ''
}

// A registration that arrives with a secret newly stored, or a different
// guild's registration entirely, is a different thing as far as this
// control is concerned. Nothing typed survives either.
watch(() => props.client.guildId, close)
watch(() => props.client.hasSecret, close)

function startTyping() {
  open.value = 'typing'
  typed.value = ''
  void nextTick(() => field.value?.focus())
}

function store() {
  if (!canSubmitClientSecret(typed.value) || props.busy) return
  emit('store', typed.value)
}
</script>

<template>
  <section
    class="rounded-lg border p-3"
    :style="{ borderColor: 'var(--border)' }"
  >
    <h3 class="text-sm font-semibold">{{ $t('admin.signInLink.secretHeading') }}</h3>

    <!-- What is known about the credential, which is whether there is one.
         Never how long it is, never its first characters, never a row of
         dots standing in for it: a mask is a value, and a value would have
         had to come from somewhere. -->
    <p class="mt-1 text-sm" :style="{ color: 'var(--text)' }">{{ $t(state.statusKey) }}</p>
    <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
      {{ $t('admin.signInLink.secretNeverShown') }}
    </p>
    <!-- Said here rather than left to be discovered after a master-key
         rotation: the wrapped secret names the key that wrapped it, and a
         rotation that left the old key behind makes this guild's link stop
         working silently from the outside. Re-typing the secret re-wraps
         it, and this is where somebody would be standing when they needed
         to know that. -->
    <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
      {{ $t('admin.signInLink.secretRotationNote') }}
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
        {{ $t('admin.signInLink.secretClear') }}
      </button>
    </div>

    <!-- The box, and it exists only because somebody pressed for it. -->
    <form v-else-if="open === 'typing'" class="mt-3" @submit.prevent="store">
      <label class="block text-sm font-medium" :for="`${base}-secret`">
        {{ $t('admin.signInLink.secretInputLabel') }}
      </label>
      <input
        :id="`${base}-secret`"
        ref="field"
        v-model="typed"
        type="password"
        autocomplete="new-password"
        spellcheck="false"
        :maxlength="MAX_SECRET_LENGTH"
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
          :disabled="busy || !canSubmitClientSecret(typed)"
        >
          {{ busy ? $t('admin.signInLink.secretSaving') : $t('admin.signInLink.secretSave') }}
        </button>
        <button
          type="button"
          class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
          :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
          :disabled="busy"
          @click="close"
        >
          {{ $t('admin.signInLink.cancel') }}
        </button>
      </div>
    </form>

    <!-- Clearing, confirmed, and told what it costs — including the one
         consequence that is not on this page: the link stops working the
         moment this is pressed. -->
    <div
      v-else
      class="mt-3 rounded-lg border p-3"
      :style="{ borderColor: 'var(--danger)' }"
    >
      <p class="text-sm font-semibold">
        {{ $t('admin.signInLink.secretClearConfirmHeading') }}
      </p>
      <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
        {{ $t('admin.signInLink.secretClearConfirmBody') }}
      </p>
      <div class="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-60"
          :style="{ background: 'var(--danger)', color: 'var(--danger-contrast)' }"
          :disabled="busy"
          @click="emit('clear')"
        >
          {{ $t('admin.signInLink.secretClearConfirm') }}
        </button>
        <button
          type="button"
          class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
          :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
          :disabled="busy"
          @click="close"
        >
          {{ $t('admin.signInLink.cancel') }}
        </button>
      </div>
    </div>
  </section>
</template>
