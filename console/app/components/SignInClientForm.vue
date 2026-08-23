<script setup lang="ts">
/**
 * The form that registers a guild's own sign-in client, and the one that
 * changes it.
 *
 * One component for both, because they are the same five fields and a
 * second copy would be a second place for the slug and URL rules to drift.
 * What differs is one prop: `mode` decides the heading and the wording of
 * the note under the sign-in name, and nothing else — the API's `PUT` is a
 * whole replacement rather than a patch, so registering and re-registering
 * really are the same request with the same body.
 *
 * **There is no credential field, and that is the point.** A `PUT` on the
 * registration does not touch the stored secret; the API gave the
 * credential two routes of its own precisely so that changing a base URL
 * cannot clear it. A password box here would either lie about what Save
 * does or wipe a working client secret every time somebody corrected a
 * typo in a client id. `ClientDraft` has no field for one, so the mistake
 * is unrepresentable rather than merely avoided — the same construction
 * `ExportTargetForm` uses, and the stakes here are higher: this credential
 * decides who gets a session at all.
 *
 * **There is no availability check either, and no button that could
 * become one.** Whether a sign-in name is free is the fact §2.2 exists to
 * keep undiscoverable — a console that answered it would be an oracle for
 * which organisations use this service, reachable by anybody who
 * administers any guild anywhere. So the name is checked for *shape* while
 * it is typed, and for availability by the API alone, whose one 409 does
 * not say whether the name is held or reserved.
 *
 * **There is no provider dropdown.** `outline` is the only value
 * `_registration` accepts, and a dropdown with one row is a control that
 * asks a question with one answer. The field is carried in the draft
 * regardless, so that a registration this console does not understand is
 * not silently rewritten to Outline by somebody saving a change of client
 * id.
 *
 * Every decision above is `~/utils/oauthClient`'s; what is left in this
 * file is markup, the token bindings and which complaint is on screen yet.
 */
import {
  type ClientDraft,
  type ClientDraftField,
  PROVIDER_OUTLINE,
  clientDraftProblems,
  clientProblemFor,
} from '~/utils/oauthClient'

const props = withDefaults(
  defineProps<{
    mode: 'register' | 'change'
    initial: ClientDraft
    busy?: boolean
  }>(),
  { busy: false },
)

const emit = defineEmits<{ submit: [ClientDraft]; cancel: [] }>()

const say = useSay()

const base = useId()
const draft = ref<ClientDraft>({ ...props.initial })

/** Whether Save has been pressed on a draft that was not ready. Before
 *  that, only a field somebody has actually touched complains — a form that
 *  objects to an empty sign-in name before anybody has typed in it is a
 *  form that opens shouting. */
const attempted = ref(false)
const dirty = ref<Partial<Record<ClientDraftField, boolean>>>({})

// A form re-opened on a different guild's registration is a different form.
// Without this, switching servers with the panel open would edit the first
// guild's values under the second guild's heading.
watch(
  () => props.initial,
  (initial) => {
    draft.value = { ...initial }
    attempted.value = false
    dirty.value = {}
  },
)

const problems = computed(() => clientDraftProblems(draft.value))
const ready = computed(() => problems.value.length === 0)

/** The complaint about one field, once it is the reader's to see. */
function complaint(field: ClientDraftField) {
  if (!attempted.value && !dirty.value[field]) return null
  return clientProblemFor(problems.value, field)
}

function touch(field: ClientDraftField) {
  dirty.value = { ...dirty.value, [field]: true }
}

/** Whether the guild comes back to this deployment's own callback.
 *
 *  Bound through a function rather than straight into `redirectMode`, so
 *  that turning it off also clears what was typed — the box is gone from
 *  the screen and `clientDraftBody` is sending `null`, and a value still
 *  held in between is an interface disagreeing with its own request. */
const ownRedirect = computed(() => draft.value.redirectMode === 'custom')

function setOwnRedirect(own: boolean) {
  draft.value = {
    ...draft.value,
    redirectMode: own ? 'custom' : 'default',
    redirectUri: own ? draft.value.redirectUri : '',
  }
  touch('redirectUri')
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
      {{ mode === 'register'
        ? $t('admin.signInLink.registerHeading')
        : $t('admin.signInLink.changeHeading') }}
    </h3>

    <div class="mt-4 flex flex-col gap-4">
      <!-- The sign-in name. A public path segment, so its shape is fixed
           and it is checked as it is typed — never for availability. -->
      <div>
        <label class="block text-sm font-medium" :for="`${base}-slug`">
          {{ $t('admin.signInLink.slugLabel') }}
        </label>
        <input
          :id="`${base}-slug`"
          v-model="draft.slug"
          type="text"
          spellcheck="false"
          autocapitalize="none"
          autocomplete="off"
          :maxlength="32"
          :aria-invalid="complaint('slug') ? 'true' : undefined"
          :aria-describedby="`${base}-slug-hint`"
          class="mt-1 w-full rounded-lg border px-3 py-2 font-mono text-sm transition-colors"
          :style="{
            borderColor: complaint('slug') ? 'var(--danger)' : 'var(--control-border)',
            background: 'var(--surface)',
            color: 'var(--text)',
          }"
          @input="touch('slug')"
        >
        <p :id="`${base}-slug-hint`" class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('admin.signInLink.slugHint') }}
        </p>
        <!-- Said where a name is typed rather than discovered after a
             refusal: the name is in a link people follow, and changing it
             later is changing a link somebody has already handed out. -->
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ mode === 'register'
            ? $t('admin.signInLink.slugPermanenceNew')
            : $t('admin.signInLink.slugPermanenceChange') }}
        </p>
        <p v-if="complaint('slug')" class="mt-1 text-xs" :style="{ color: 'var(--danger)' }">
          {{ say(complaint('slug')) }}
        </p>
      </div>

      <!-- Where the guild's people authorise. -->
      <div>
        <label class="block text-sm font-medium" :for="`${base}-base-url`">
          {{ $t('admin.signInLink.baseUrlLabel') }}
        </label>
        <input
          :id="`${base}-base-url`"
          v-model="draft.baseUrl"
          type="url"
          inputmode="url"
          spellcheck="false"
          autocomplete="off"
          :aria-invalid="complaint('baseUrl') ? 'true' : undefined"
          :aria-describedby="`${base}-base-url-hint`"
          class="mt-1 w-full rounded-lg border px-3 py-2 font-mono text-sm transition-colors"
          :style="{
            borderColor: complaint('baseUrl') ? 'var(--danger)' : 'var(--control-border)',
            background: 'var(--surface)',
            color: 'var(--text)',
          }"
          @input="touch('baseUrl')"
        >
        <p
          :id="`${base}-base-url-hint`"
          class="mt-1 text-xs"
          :style="{ color: 'var(--text-muted)' }"
        >
          {{ $t('admin.signInLink.baseUrlHint') }}
        </p>
        <p v-if="complaint('baseUrl')" class="mt-1 text-xs" :style="{ color: 'var(--danger)' }">
          {{ say(complaint('baseUrl')) }}
        </p>
      </div>

      <!-- Which application in that Outline. Half of a credential pair, and
           the half that is not a secret — so it is an ordinary text field
           and is never written to a log. -->
      <div>
        <label class="block text-sm font-medium" :for="`${base}-client-id`">
          {{ $t('admin.signInLink.clientIdLabel') }}
        </label>
        <input
          :id="`${base}-client-id`"
          v-model="draft.clientId"
          type="text"
          spellcheck="false"
          autocomplete="off"
          :aria-invalid="complaint('clientId') ? 'true' : undefined"
          :aria-describedby="`${base}-client-id-hint`"
          class="mt-1 w-full rounded-lg border px-3 py-2 font-mono text-sm transition-colors"
          :style="{
            borderColor: complaint('clientId') ? 'var(--danger)' : 'var(--control-border)',
            background: 'var(--surface)',
            color: 'var(--text)',
          }"
          @input="touch('clientId')"
        >
        <p
          :id="`${base}-client-id-hint`"
          class="mt-1 text-xs"
          :style="{ color: 'var(--text-muted)' }"
        >
          {{ $t('admin.signInLink.clientIdHint') }}
        </p>
        <p v-if="complaint('clientId')" class="mt-1 text-xs" :style="{ color: 'var(--danger)' }">
          {{ say(complaint('clientId')) }}
        </p>
      </div>

      <!-- Where the browser comes back to. A checkbox and a box that only
           exists when it is ticked, because "" and null are the same value
           in a text field and different values in this API. -->
      <div>
        <label class="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            :checked="ownRedirect"
            @change="setOwnRedirect(($event.target as HTMLInputElement).checked)"
          >
          {{ $t('admin.signInLink.redirectOwnLabel') }}
        </label>
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ ownRedirect
            ? $t('admin.signInLink.redirectOwnHint')
            : $t('admin.signInLink.redirectDefaultHint') }}
        </p>

        <div v-if="ownRedirect" class="mt-2">
          <label class="block text-sm font-medium" :for="`${base}-redirect`">
            {{ $t('admin.signInLink.redirectLabel') }}
          </label>
          <input
            :id="`${base}-redirect`"
            v-model="draft.redirectUri"
            type="url"
            inputmode="url"
            spellcheck="false"
            autocomplete="off"
            :aria-invalid="complaint('redirectUri') ? 'true' : undefined"
            class="mt-1 w-full rounded-lg border px-3 py-2 font-mono text-sm transition-colors"
            :style="{
              borderColor: complaint('redirectUri') ? 'var(--danger)' : 'var(--control-border)',
              background: 'var(--surface)',
              color: 'var(--text)',
            }"
            @input="touch('redirectUri')"
          >
          <p v-if="complaint('redirectUri')" class="mt-1 text-xs" :style="{ color: 'var(--danger)' }">
            {{ say(complaint('redirectUri')) }}
          </p>
        </div>
      </div>

      <!-- The provider. Stated rather than chosen: one legal value is not a
           question. It travels in the draft so that a save cannot rewrite
           a registration this console has never heard of. -->
      <div>
        <span class="block text-sm font-medium">{{ $t('admin.signInLink.providerLabel') }}</span>
        <p class="mt-1 font-mono text-sm" :style="{ color: 'var(--text)' }">{{ draft.provider }}</p>
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('admin.signInLink.providerHint') }}
        </p>
        <p
          v-if="draft.provider !== PROVIDER_OUTLINE"
          class="mt-1 text-xs"
          :style="{ color: 'var(--danger)' }"
        >
          {{ say(clientProblemFor(problems, 'provider')) }}
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
        {{ busy ? $t('admin.signInLink.saving') : $t('admin.signInLink.save') }}
      </button>
      <button
        type="button"
        class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
        :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
        :disabled="busy"
        @click="emit('cancel')"
      >
        {{ $t('admin.signInLink.cancel') }}
      </button>
    </div>
  </form>
</template>
