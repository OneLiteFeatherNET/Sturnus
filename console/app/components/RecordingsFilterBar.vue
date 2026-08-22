<script setup lang="ts">
/**
 * Narrowing the recordings list.
 *
 * **Every control writes to the URL and nothing else.** The parent reads
 * the route and asks the API; this component never fetches the list and
 * never holds the answer. That keeps a filtered list addressable — it can
 * be bookmarked, opened in a second tab and reached with the back button
 * — and it keeps exactly one description of what is on screen, in the
 * place a reader can see it.
 *
 * **It is a form, and it submits.** Not a set of inputs that refetch on
 * every keystroke: a search box wired to a watcher issues a request per
 * character, each of them a scan of somebody's history, and the answers
 * arrive out of order. Pressing Enter or "Search" is one request, at a
 * moment the reader chose.
 *
 * **What it searches is stated on the page.** The API matches the
 * channel, the people who were in a session and the reader's own tags,
 * and never a transcript — a decision about other people's speech, made
 * in `sturnus.console.filters`. A search box that does not say what it
 * searches leaves people assuming it does more, and here the wrong
 * assumption is that Sturnus will find them a phrase somebody said.
 */
import {
  NO_FILTERS,
  activeFilterLabels,
  hasActiveFilters,
  toggledTag,
  type RecordingFilters,
} from '~/utils/recordingFilters'
import { TAGS_PATH, type TagsResponse } from '~/utils/tagging'

const props = defineProps<{
  /** What the URL currently says. The single source of truth. */
  filters: RecordingFilters
  /** How many recordings the current filter matched, for the summary. */
  total: number
}>()

const emit = defineEmits<{ apply: [filters: RecordingFilters] }>()

/** A working copy, so that typing does not navigate on every keystroke.
 *  Reset whenever the URL changes under us — a back button that left the
 *  boxes showing the previous filter would describe a list nobody is
 *  looking at. */
const draft = ref<RecordingFilters>({ ...props.filters, tags: [...props.filters.tags] })
watch(
  () => props.filters,
  (fresh) => {
    draft.value = { ...fresh, tags: [...fresh.tags] }
  },
  { deep: true },
)

const api = useApi()
/** The reader's own labels, most used first. Only ever theirs: the API
 *  keys `session_tag` by its owner, so this list cannot contain anybody
 *  else's word for a meeting. */
const { data: tagData } = await useAsyncData('recording-tags', () =>
  api<TagsResponse>(TAGS_PATH),
)
const offered = computed(() => tagData.value?.tags ?? [])

const active = computed(() => hasActiveFilters(props.filters))
const described = computed(() => activeFilterLabels(props.filters))

const searchId = useId()
const fromId = useId()
const toId = useId()
const protocolId = useId()

function submit() {
  emit('apply', { ...draft.value, tags: [...draft.value.tags] })
}

/** A chip applies immediately: it is one click and it has no other
 *  half to fill in, so making somebody press Search afterwards would be
 *  asking for a second click that says nothing new. */
function toggle(tag: string) {
  const next = toggledTag(props.filters, tag)
  draft.value = { ...next, tags: [...next.tags] }
  emit('apply', next)
}

function clear() {
  emit('apply', { ...NO_FILTERS, tags: [] })
}
</script>

<template>
  <section
    class="rounded-2xl border p-4"
    :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    aria-labelledby="recordings-filter-heading"
  >
    <h2 id="recordings-filter-heading" class="sr-only">Find a recording</h2>

    <form class="flex flex-col gap-3" @submit.prevent="submit()">
      <div class="flex flex-wrap items-end gap-3">
        <div class="min-w-56 flex-1">
          <label class="block text-xs font-medium" :for="searchId">Search</label>
          <input
            :id="searchId"
            v-model="draft.q"
            type="search"
            placeholder="channel, who was there, or one of your tags"
            class="mt-1 w-full rounded-lg border px-3 py-1.5 text-sm"
            :style="{
              borderColor: 'var(--control-border)',
              background: 'var(--surface-raised)',
              color: 'var(--text)',
            }"
          >
        </div>

        <div>
          <label class="block text-xs font-medium" :for="fromId">From</label>
          <input
            :id="fromId"
            v-model="draft.from"
            type="date"
            class="mt-1 rounded-lg border px-3 py-1.5 text-sm"
            :style="{
              borderColor: 'var(--control-border)',
              background: 'var(--surface-raised)',
              color: 'var(--text)',
            }"
          >
        </div>

        <div>
          <label class="block text-xs font-medium" :for="toId">To</label>
          <input
            :id="toId"
            v-model="draft.to"
            type="date"
            class="mt-1 rounded-lg border px-3 py-1.5 text-sm"
            :style="{
              borderColor: 'var(--control-border)',
              background: 'var(--surface-raised)',
              color: 'var(--text)',
            }"
          >
        </div>

        <div>
          <label class="block text-xs font-medium" :for="protocolId">Protocol</label>
          <select
            :id="protocolId"
            v-model="draft.protocol"
            class="mt-1 rounded-lg border px-3 py-1.5 text-sm"
            :style="{
              borderColor: 'var(--control-border)',
              background: 'var(--surface-raised)',
              color: 'var(--text)',
            }"
          >
            <option value="">Either</option>
            <option value="with">Written</option>
            <!-- How you find the meeting whose document never got
                 written, which is the reason this control exists. -->
            <option value="without">Not written</option>
          </select>
        </div>

        <button
          type="submit"
          class="rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
          :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
        >
          Search
        </button>
      </div>

      <!-- What the search actually looks at. Left unsaid, people assume
           it finds a phrase somebody said in a meeting — which it does
           not, and must not. -->
      <p class="text-xs" :style="{ color: 'var(--text-muted)' }">
        Searches the channel name, who was in the meeting, and your own tags. It does not search
        what was said: a transcript is everybody else's words, and they consented to a protocol
        being written from them, not to being searchable.
      </p>

      <ul v-if="offered.length > 0" class="flex flex-wrap items-center gap-1.5">
        <li class="text-xs" :style="{ color: 'var(--text-muted)' }">Your tags:</li>
        <li v-for="use in offered" :key="use.tag">
          <button
            type="button"
            class="rounded-full border px-2.5 py-0.5 text-xs tabular-nums transition-colors hover:bg-[var(--surface-raised)]"
            :style="{
              borderColor: filters.tags.includes(use.tag) ? 'var(--text)' : 'var(--control-border)',
              color: 'var(--text)',
            }"
            :aria-pressed="filters.tags.includes(use.tag)"
            @click="toggle(use.tag)"
          >
            {{ use.tag }} · {{ use.sessions }}
          </button>
        </li>
      </ul>
    </form>

    <!-- Why this list is shorter than the reader's history. A list that
         is filtered without saying so is one people report as having lost
         their meetings. -->
    <p v-if="active" class="mt-3 flex flex-wrap items-center gap-2 text-xs">
      <span :style="{ color: 'var(--text-muted)' }">
        Showing {{ total }} {{ total === 1 ? 'recording' : 'recordings' }}
        {{ described.join(', ') }}.
      </span>
      <button
        type="button"
        class="rounded-lg px-2 py-0.5 font-medium underline transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ color: 'var(--text)' }"
        @click="clear()"
      >
        Clear
      </button>
    </p>
  </section>
</template>
