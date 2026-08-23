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
 * **Tags and words are one field now.** They were two mechanisms for one
 * question — a row of toggle buttons, and a box beside it — and the
 * question people actually ask is `#standup #migration the bit where the
 * database fell over`, which everybody typed into the box. `UiChipInput`
 * holds both, and keeps the line between them in the value rather than
 * only on screen, so the chips go out as `?tag=` and the words as `?q=`
 * without anything here parsing a tag back out of a sentence. The
 * translation is `~/utils/recordingFilters`.
 *
 * **What it searches is stated on the page, and stated so it stays true.**
 * The API matches the channel, the people who were in a session and the
 * reader's own tags, and never a transcript — a decision about other
 * people's speech, made in `sturnus.console.filters`. The note below
 * names the boundary rather than enumerating the columns, because the
 * columns are due to grow and a sentence that lists three of them becomes
 * quietly wrong the day a fourth is added. The half that must never
 * change is stated hardest.
 */
import {
  NO_FILTERS,
  activeFilterLabels,
  chipsFromFilters,
  filtersFromChips,
  hasActiveFilters,
  type ProtocolFilter,
  type RecordingFilters,
} from '~/utils/recordingFilters'
import { TAGS_PATH, type TagsResponse } from '~/utils/tagging'
import type { ChipValue } from '~/utils/uiChipInput'
import type { UiOption } from '~/utils/uiOption'

const props = defineProps<{
  /** What the URL currently says. The single source of truth. */
  filters: RecordingFilters
  /** How many recordings the current filter matched, for the summary. */
  total: number
}>()

const emit = defineEmits<{ apply: [filters: RecordingFilters] }>()

const { t } = useI18n()
const say = useSay()

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

/** The one field, both halves of it. Written back through the module so
 *  that the chips and the free text land in the fields the API reads. */
const chips = computed<ChipValue>({
  get: () => chipsFromFilters(draft.value),
  set: (value) => {
    draft.value = filtersFromChips(draft.value, value)
  },
})

/**
 * One end of the date range, as the control speaks it.
 *
 * The filter writes an absent bound as the empty string, because that is
 * what an untouched field holds and what `filtersToRouteQuery` drops from
 * the URL. `UiDatePicker` writes it as `null`, because a control that
 * emits `''` for "nothing chosen" is one every caller has to remember to
 * test twice. One adapter, in one place, rather than the two branches
 * that would otherwise appear at both ends of the range.
 */
function dayBound(end: 'from' | 'to') {
  return computed<string | null>({
    get: () => draft.value[end] || null,
    set: (value) => {
      draft.value = { ...draft.value, [end]: value ?? '' }
    },
  })
}

const from = dayBound('from')
const to = dayBound('to')

/**
 * The third control, which does *not* get that adapter.
 *
 * "Either" is a choice this filter can express and the API reads, so it
 * is an option with a value rather than the absence of one. Mapping it to
 * `null` would leave the trigger reading "Choose an option" for a filter
 * that is perfectly well chosen — and the reason this control exists is
 * to be able to say "the ones whose document never got written", which is
 * only legible next to a stated "either".
 */
const protocol = computed<string | null>({
  get: () => draft.value.protocol,
  set: (value) => {
    draft.value = { ...draft.value, protocol: (value ?? '') as ProtocolFilter }
  },
})

const api = useApi()
/** The reader's own labels, most used first. Only ever theirs: the API
 *  keys `session_tag` by its owner, so this list cannot contain anybody
 *  else's word for a meeting. */
const { data: tagData } = await useAsyncData('recording-tags', () =>
  api<TagsResponse>(TAGS_PATH),
)
/** Offered as suggestions rather than laid out as a row of buttons. The
 *  row was a permanent inventory of somebody's whole vocabulary sitting
 *  above a list they were trying to read; the suggestions appear under
 *  the caret, when a tag is what is being typed. */
const offered = computed(() => tagData.value?.tags.map((use) => use.tag) ?? [])

const protocols = computed<UiOption[]>(() => [
  { value: '', label: t('recordings.protocolEither') },
  { value: 'with', label: t('recordings.protocolWritten') },
  // How you find the meeting whose document never got written, which is
  // the reason this control exists.
  { value: 'without', label: t('recordings.protocolNotWritten') },
])

const active = computed(() => hasActiveFilters(props.filters))

/**
 * Why this list is shorter than the reader's history.
 *
 * One sentence with two holes in it -- how many, and what was narrowed --
 * rather than a count with phrases stuck on the end. The phrases are joined
 * with commas here because a list of phrases is a list in both languages;
 * where they go in the sentence is the locale file's to say, and German
 * puts them after the verb where English does not.
 */
const summary = computed(() => ({
  key: 'recordings.filterSummary',
  params: {
    count: props.total,
    what: activeFilterLabels(props.filters).map(say).join(', '),
  },
}))

function submit() {
  emit('apply', { ...draft.value, tags: [...draft.value.tags] })
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
    <h2 id="recordings-filter-heading" class="sr-only">{{ $t('recordings.filterHeading') }}</h2>

    <!-- Enter in the chip field commits a chip rather than submitting, so
         the button is not a convenience: it is how the form is sent once
         a tag has been added. `UiChipInput` leaves an Enter it did not use
         to the form around it, which is what makes both work. -->
    <form class="flex flex-col gap-3" @submit.prevent="submit()">
      <div class="flex flex-wrap items-end gap-3">
        <div class="min-w-56 flex-1">
          <!-- The visible label and the control's accessible name are the
               same words, so the visible one is hidden from assistive
               technology rather than read out twice ahead of it. -->
          <span aria-hidden="true" class="block text-xs font-medium">
            {{ $t('recordings.searchLabel') }}
          </span>
          <div class="mt-1">
            <UiChipInput
              v-model="chips"
              :suggestions="offered"
              :label="$t('recordings.searchLabel')"
              :placeholder="$t('recordings.searchPlaceholder')"
            />
          </div>
        </div>

        <button
          type="submit"
          class="rounded-lg border px-3 py-2 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
          :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
        >
          {{ $t('recordings.searchButton') }}
        </button>
      </div>

      <!-- The three narrower controls, side by side where there is room
           and stacked where there is not. They wrap rather than scroll,
           for the same reason a tab strip does: whatever is pushed off the
           edge is the control nobody finds again. -->
      <div class="grid gap-3 sm:grid-cols-3">
        <div>
          <span aria-hidden="true" class="block text-xs font-medium">
            {{ $t('recordings.fromLabel') }}
          </span>
          <div class="mt-1">
            <UiDatePicker v-model="from" granularity="day" :label="$t('recordings.fromLabel')" />
          </div>
        </div>

        <div>
          <span aria-hidden="true" class="block text-xs font-medium">
            {{ $t('recordings.toLabel') }}
          </span>
          <div class="mt-1">
            <UiDatePicker v-model="to" granularity="day" :label="$t('recordings.toLabel')" />
          </div>
        </div>

        <div>
          <span aria-hidden="true" class="block text-xs font-medium">
            {{ $t('recordings.protocolLabel') }}
          </span>
          <div class="mt-1">
            <UiSelect
              v-model="protocol"
              :options="protocols"
              :label="$t('recordings.protocolLabel')"
            />
          </div>
        </div>
      </div>

      <!-- What the search actually looks at. Left unsaid, people assume
           it finds a phrase somebody said in a meeting — which it does
           not, and must not. -->
      <p class="text-xs" :style="{ color: 'var(--text-muted)' }">
        {{ $t('recordings.searchScopeNote') }}
      </p>
    </form>

    <!-- Why this list is shorter than the reader's history. A list that
         is filtered without saying so is one people report as having lost
         their meetings. -->
    <p v-if="active" class="mt-3 flex flex-wrap items-center gap-2 text-xs">
      <span :style="{ color: 'var(--text-muted)' }">{{ say(summary) }}</span>
      <button
        type="button"
        class="rounded-lg px-2 py-0.5 font-medium underline transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ color: 'var(--text)' }"
        @click="clear()"
      >
        {{ $t('recordings.clearFilters') }}
      </button>
    </p>
  </section>
</template>
