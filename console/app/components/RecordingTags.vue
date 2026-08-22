<script setup lang="ts">
/**
 * The labels this person has put on this recording.
 *
 * **Only they can see them.** Everybody else in the meeting has their own
 * set, and neither sees the other's — a tag is a remark about a
 * conversation other people were also in, and a shared list would publish
 * those remarks to everyone who was there. The page says so out loud,
 * because a person deciding what to write needs to know who will read it,
 * and "private unless stated otherwise" is not an assumption anybody
 * should have to make about a system that records them.
 *
 * The component holds no decisions. What a tag is, when two are one, and
 * what the limits are all live in `~/utils/tagging` where they are tested
 * without rendering anything — this file is the shape on the screen and
 * the order the writes happen in.
 *
 * **Writes are optimistic and roll back.** A chip appears the moment it is
 * typed, because the alternative is a control that does nothing for a
 * round trip; if the write fails the set returns to exactly what it was
 * and says so. What comes back from the server replaces the local set
 * either way, since normalisation may have merged two chips into one and
 * a client showing its own input back would display a tag the database
 * does not have.
 */
import { ApiError } from '~/utils/apiError'
import {
  TAG_MAX_CHARS,
  TAG_MAX_PER_RECORDING,
  sessionTagsPath,
  tagRefusal,
  tagWriteFailed,
  tagsWith,
  tagsWithout,
  type StoredTagsResponse,
} from '~/utils/tagging'

const props = defineProps<{
  sessionId: string
  /** The labels the API returned with the session. */
  tags: string[]
}>()

const api = useApi()

const held = ref<string[]>([...props.tags])
// The session can be refetched under us — a navigation to another
// recording reuses this component. Without this the chips would be the
// previous recording's.
watch(
  () => props.tags,
  (fresh) => {
    held.value = [...fresh]
  },
)

const typed = ref('')
const saving = ref(false)
const message = ref<string | null>(null)
const input = ref<HTMLInputElement | null>(null)
// A generated id rather than a fixed one: two of these on one page would
// otherwise share an id, and a `<label for>` pointing at two elements
// points at whichever the browser finds first.
const inputId = useId()
const hintId = `${inputId}-hint`

/** Where the ceiling is announced before somebody hits it. */
const remaining = computed(() => TAG_MAX_PER_RECORDING - held.value.length)

async function store(next: string[], announcement: string) {
  const before = [...held.value]
  // Optimistic: the chip is on screen before the request leaves. A tag
  // editor that waits for a round trip per chip is one people click twice.
  held.value = next
  saving.value = true
  message.value = null
  try {
    const stored = await api<StoredTagsResponse>(sessionTagsPath(props.sessionId), {
      method: 'PUT',
      body: { tags: next },
    })
    // The server's set, not ours: it decided what these labels are.
    held.value = stored.tags
    message.value = announcement
  } catch (cause) {
    held.value = before
    message.value = tagWriteFailed(cause instanceof ApiError ? cause.status : 0)
  } finally {
    saving.value = false
  }
}

async function add() {
  const refusal = tagRefusal(held.value, typed.value)
  if (refusal !== null) {
    message.value = refusal
    return
  }
  const next = tagsWith(held.value, typed.value)
  if (next === null) return
  typed.value = ''
  await store(next, 'Tag added.')
}

async function remove(tag: string) {
  // Focus first, and to the input rather than nowhere. The button about
  // to be removed is the one holding focus, and a control that unmounts
  // itself drops the caller to the top of the document.
  input.value?.focus()
  await store(tagsWithout(held.value, tag), 'Tag removed.')
}
</script>

<template>
  <section
    class="rounded-2xl border p-5"
    :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
  >
    <h2 class="text-base font-semibold">Your tags</h2>
    <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
      Labels you put on this recording so you can find it again. Only you can see them — everybody
      else who was in this meeting has their own, and neither of you sees the other's.
    </p>

    <ul v-if="held.length > 0" class="mt-4 flex flex-wrap gap-2">
      <li
        v-for="tag in held"
        :key="tag"
        class="flex items-center gap-1 rounded-full py-1 pl-3 pr-1 text-sm"
        :style="{ background: 'var(--surface-raised)' }"
      >
        <span>{{ tag }}</span>
        <button
          type="button"
          class="rounded-full px-1.5 py-0.5 text-xs transition-colors hover:bg-[var(--surface-sunken)] disabled:opacity-60"
          :style="{ color: 'var(--text-muted)' }"
          :disabled="saving"
          :aria-label="`Remove the tag ${tag}`"
          @click="remove(tag)"
        >
          ✕
        </button>
      </li>
    </ul>
    <p v-else class="mt-4 text-sm" :style="{ color: 'var(--text-muted)' }">
      This recording has no tags yet.
    </p>

    <form class="mt-4 flex flex-wrap items-start gap-2" @submit.prevent="add()">
      <div class="min-w-48 flex-1">
        <label class="sr-only" :for="inputId">Add a tag to this recording</label>
        <input
          :id="inputId"
          ref="input"
          v-model="typed"
          type="text"
          :maxlength="TAG_MAX_CHARS"
          :disabled="saving"
          :aria-describedby="hintId"
          placeholder="retro, kunde onelitefeather"
          class="w-full rounded-lg border px-3 py-1.5 text-sm"
          :style="{
            borderColor: 'var(--control-border)',
            background: 'var(--surface-raised)',
            color: 'var(--text)',
          }"
        >
        <p :id="hintId" class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          Separate several with commas. {{ remaining }} of
          {{ TAG_MAX_PER_RECORDING }} left on this recording.
        </p>
      </div>
      <!-- Never removed while it works, only disabled: a control that
           unmounts itself when pressed drops the keyboard to the top of
           the document. -->
      <button
        type="submit"
        class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-60"
        :style="{ color: 'var(--action)' }"
        :disabled="saving || typed.trim().length === 0"
      >
        {{ saving ? 'Saving…' : 'Add' }}
      </button>
    </form>

    <!-- Always in the DOM, so that a screen reader is watching it before
         it has anything to say. A live region added at the moment of the
         announcement is a live region that announces nothing. -->
    <p
      class="mt-2 min-h-5 text-xs"
      :style="{ color: 'var(--text-muted)' }"
      role="status"
      aria-live="polite"
    >
      {{ message }}
    </p>
  </section>
</template>
