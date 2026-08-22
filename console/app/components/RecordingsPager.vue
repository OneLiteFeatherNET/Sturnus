<script setup lang="ts">
/**
 * Moving between pages of the recordings list.
 *
 * Every control is a link and not a button, because every page of the
 * list is a place: it can be opened in a new tab, bookmarked, and reached
 * with the back button. A pager built from click handlers is a pager
 * whose third page has no address.
 *
 * The arithmetic — which numbers to show, where the gaps go, whether
 * there is a next page at all — is `~/utils/paging`, tested without
 * rendering anything. What is left here is the shape and the labels.
 *
 * The whole thing is a `<nav>` with a name, so that a screen reader can
 * skip it and find it again; the current page is marked with
 * `aria-current="page"` rather than by colour alone.
 */
import { pageNumbers } from '~/utils/paging'

const props = defineProps<{
  page: number
  count: number
}>()

/** A link to a page, keeping the rest of the query string. The first page
 *  drops the parameter entirely, so `/recordings` stays the address of the
 *  list rather than becoming a synonym for `/recordings?page=1`. */
const route = useRoute()
function linkTo(page: number) {
  const query = { ...route.query }
  if (page <= 1) delete query.page
  else query.page = String(page)
  return { path: route.path, query }
}

const numbers = computed(() => pageNumbers(props.page, props.count))
const hasPrevious = computed(() => props.page > 1)
const hasNext = computed(() => props.page < props.count)
</script>

<template>
  <nav
    v-if="count > 1"
    class="flex flex-wrap items-center justify-center gap-1"
    aria-label="Recordings pages"
  >
    <NuxtLink
      v-if="hasPrevious"
      :to="linkTo(page - 1)"
      rel="prev"
      class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
      :style="{ color: 'var(--text)' }"
    >
      ← Newer
    </NuxtLink>
    <!-- Kept in the flow as inert text rather than removed, so the number
         strip does not slide sideways on the first and last pages. -->
    <span v-else class="px-3 py-1.5 text-sm" :style="{ color: 'var(--text-muted)' }">← Newer</span>

    <template v-for="(number, index) in numbers">
      <span
        v-if="number === null"
        :key="`gap-${index}`"
        class="px-2 text-sm"
        :style="{ color: 'var(--text-muted)' }"
        aria-hidden="true"
      >
        …
      </span>
      <NuxtLink
        v-else-if="number !== page"
        :key="`page-${number}`"
        :to="linkTo(number)"
        class="rounded-lg px-3 py-1.5 text-sm tabular-nums transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ color: 'var(--text)' }"
        :aria-label="`Page ${number}`"
      >
        {{ number }}
      </NuxtLink>
      <!-- The page you are on is not a link to itself. Marked with
           `aria-current` as well as with a border, because a page number
           distinguished only by its colour is not distinguished at all
           for a reader who cannot see the difference. -->
      <span
        v-else
        :key="`current-${number}`"
        class="rounded-lg border px-3 py-1.5 text-sm font-semibold tabular-nums"
        :style="{ borderColor: 'var(--text)' }"
        aria-current="page"
      >
        {{ number }}
      </span>
    </template>

    <NuxtLink
      v-if="hasNext"
      :to="linkTo(page + 1)"
      rel="next"
      class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
      :style="{ color: 'var(--text)' }"
    >
      Older →
    </NuxtLink>
    <span v-else class="px-3 py-1.5 text-sm" :style="{ color: 'var(--text-muted)' }">Older →</span>
  </nav>
</template>
