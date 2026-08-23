<script setup lang="ts">
/**
 * What this meeting is called, and what was written about it.
 *
 * **Everybody who was in it sees this.** That is the difference between
 * this and the tag editor below it, and it is the reason the two are not
 * one component with a longer form: a tag is a private remark about a
 * conversation other people were also in, and a title is the meeting's
 * name. Anybody who was in the room may correct it, there is no history,
 * and the form says so — somebody about to empty a field is about to empty
 * it for four colleagues, and finding that out afterwards is not a
 * reasonable way to learn it.
 *
 * **One button for two fields, because the endpoint replaces.** `PUT` on
 * `/sessions/{id}/name` stores the pair it is given, and a member left out
 * of the body is a member cleared. An interface with a Save beside the
 * title would therefore be an interface that deletes the description
 * whenever somebody fixes a typo in the name — silently, and with no way
 * back. So there is one control, it submits the whole draft, and
 * `~/utils/sessionNaming` is arranged so that no other body can be built.
 *
 * The shape of the interaction is `RecordingTags`', deliberately: a
 * disabled-not-removed button, a live region that is in the DOM before it
 * has anything to say, and the server's answer replacing the local text.
 * What is not borrowed is the optimism. A tag appears before its round
 * trip because the alternative is a chip that lags a keystroke; a title is
 * already on screen in the box somebody typed it into, so there is nothing
 * to show early and an optimistic heading would only be a heading that
 * flickers back on a refusal.
 */
import { ApiError } from '~/utils/apiError'
import type { Message } from '~/utils/message'
import {
  NAME_MAX_DESCRIPTION_CHARS,
  NAME_MAX_TITLE_CHARS,
  nameBodyFrom,
  nameDraftFrom,
  nameIsDirty,
  nameRefusal,
  nameWriteFailed,
  sessionNamePath,
  type SessionName,
} from '~/utils/sessionNaming'

const props = defineProps<{
  sessionId: string
  /** The pair the session endpoint returned. */
  name: SessionName
}>()

const emit = defineEmits<{ saved: [SessionName] }>()

const api = useApi()
const say = useSay()

/** What is stored, as far as this component knows. Replaced by whatever a
 *  write answers with, never by what was typed: normalisation may have
 *  changed the text, and a form showing its own input back would keep
 *  displaying a title the database does not have. */
const stored = ref<SessionName>({ title: props.name.title, description: props.name.description })
const draft = ref(nameDraftFrom(props.name))

// The session can be refetched under this component -- a navigation to
// another recording reuses it -- and without this the boxes would hold the
// previous meeting's name. Guarded on the draft being clean, because
// overwriting half-typed prose with a payload that arrived for unrelated
// reasons is the one thing worse than showing a stale title.
watch(
  () => props.name,
  (fresh) => {
    if (nameIsDirty(stored.value, draft.value)) return
    stored.value = { title: fresh.title, description: fresh.description }
    draft.value = nameDraftFrom(fresh)
  },
)

const saving = ref(false)
/** What the live region says next. A key and its values, decided in
 *  `~/utils/sessionNaming` and worded in the locale files. */
const message = ref<Message | null>(null)

const dirty = computed(() => nameIsDirty(stored.value, draft.value))

// Generated rather than fixed: two of these on one page would share an id,
// and a `<label for>` pointing at two elements points at whichever the
// browser finds first.
const titleId = useId()
const descriptionId = useId()
const noteId = `${titleId}-note`

async function save() {
  if (saving.value) return
  const refusal = nameRefusal(draft.value)
  if (refusal !== null) {
    message.value = refusal
    return
  }
  saving.value = true
  message.value = null
  try {
    // The whole draft, both members. See the module: there is no shape
    // here that can carry one of them.
    const answer = await api<SessionName>(sessionNamePath(props.sessionId), {
      method: 'PUT',
      body: nameBodyFrom(draft.value),
    })
    stored.value = { title: answer.title, description: answer.description }
    draft.value = nameDraftFrom(answer)
    message.value = { key: 'recordings.nameSaved' }
    // So the heading above the tabs stops disagreeing with the box that
    // was just saved. The page owns what it renders; this says what the
    // server stored and lets it decide.
    emit('saved', { title: answer.title, description: answer.description })
  } catch (cause) {
    message.value = nameWriteFailed(cause instanceof ApiError ? cause.status : 0)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section
    class="rounded-2xl border p-5"
    :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
  >
    <h2 class="text-base font-semibold">{{ $t('recordings.nameHeading') }}</h2>
    <p class="mt-1 max-w-2xl text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ $t('recordings.nameNote') }}
    </p>

    <form class="mt-4 flex flex-col gap-4" @submit.prevent="save()">
      <div>
        <label class="text-sm font-medium" :for="titleId">
          {{ $t('recordings.nameTitleLabel') }}
        </label>
        <input
          :id="titleId"
          v-model="draft.title"
          type="text"
          :maxlength="NAME_MAX_TITLE_CHARS"
          :disabled="saving"
          :aria-describedby="noteId"
          :placeholder="$t('recordings.nameTitlePlaceholder')"
          class="mt-1 w-full rounded-lg border px-3 py-1.5 text-sm"
          :style="{
            borderColor: 'var(--control-border)',
            background: 'var(--surface-raised)',
            color: 'var(--text)',
          }"
        >
      </div>

      <div>
        <label class="text-sm font-medium" :for="descriptionId">
          {{ $t('recordings.nameDescriptionLabel') }}
        </label>
        <!-- A textarea rather than a second input: a description keeps its
             own line breaks, which is what makes an agenda an agenda. The
             API keeps them too and collapses only the title. -->
        <textarea
          :id="descriptionId"
          v-model="draft.description"
          rows="6"
          :maxlength="NAME_MAX_DESCRIPTION_CHARS"
          :disabled="saving"
          :aria-describedby="noteId"
          :placeholder="$t('recordings.nameDescriptionPlaceholder')"
          class="mt-1 w-full rounded-lg border px-3 py-2 text-sm"
          :style="{
            borderColor: 'var(--control-border)',
            background: 'var(--surface-raised)',
            color: 'var(--text)',
          }"
        />
      </div>

      <!-- Said before the button rather than after the mistake. Both
           halves are surprises: the write replaces, and the result is not
           private. -->
      <p :id="noteId" class="text-xs" :style="{ color: 'var(--text-muted)' }">
        {{ $t('recordings.nameReplaceNote') }}
      </p>

      <div class="flex flex-wrap items-center gap-3">
        <!-- Disabled while it works rather than replaced: a control that
             unmounts itself when pressed drops the keyboard to the top of
             the document. Disabled while nothing has changed, too, so that
             an available Save means there is something to save. -->
        <button
          type="submit"
          class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-60"
          :style="{ color: 'var(--action)' }"
          :disabled="saving || !dirty"
        >
          {{ saving ? $t('recordings.nameSaving') : $t('recordings.nameSave') }}
        </button>
        <span v-if="dirty && !saving" class="text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('recordings.nameUnsaved') }}
        </span>
      </div>
    </form>

    <!-- Always in the DOM, so that a screen reader is watching it before it
         has anything to say. A live region added at the moment of the
         announcement announces nothing. -->
    <p
      class="mt-2 min-h-5 text-xs"
      :style="{ color: 'var(--text-muted)' }"
      role="status"
      aria-live="polite"
    >
      {{ message ? say(message) : '' }}
    </p>
  </section>
</template>
