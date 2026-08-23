<script setup lang="ts">
/**
 * The pager that stands beside a list and emits a page number.
 *
 * Not a replacement for `RecordingsPager`, and deliberately not built the
 * same way. That one renders links, because every page of the recordings
 * list is a place with an address; this one cannot assume its caller has
 * one, so it is a set of buttons and the caller decides whether the page
 * number ends up in a URL.
 *
 * The arithmetic is `~/utils/uiPagination`, which is itself mostly
 * `~/utils/paging` — the numbers to show, the gaps, how many pages a total
 * divides into were all decided once already and are not decided again
 * here.
 */
import { paginationView, stepPage } from '~/utils/uiPagination'

const props = withDefaults(
  defineProps<{
    page: number
    total: number
    size?: number
    label?: string
  }>(),
  { size: undefined, label: undefined },
)

const emit = defineEmits<{ 'update:page': [number] }>()

const say = useSay()

const view = computed(() => paginationView(props.page, props.total, props.size))

function go(page: number) {
  if (page !== view.value.page) emit('update:page', page)
}
</script>

<template>
  <nav
    :aria-label="label ?? $t('ui.pagination.nav')"
    class="flex flex-wrap items-center justify-center gap-1"
  >
    <button
      type="button"
      rel="prev"
      class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-40"
      :style="{ color: 'var(--text)' }"
      :disabled="!view.hasPrevious"
      @click="go(stepPage(view.page, view.count, -1))"
    >
      {{ $t('ui.pagination.previous') }}
    </button>

    <template v-for="(number, index) in view.numbers">
      <span
        v-if="number === null"
        :key="`gap-${index}`"
        class="px-2 text-sm"
        :style="{ color: 'var(--text-muted)' }"
        aria-hidden="true"
      >
        …
      </span>
      <!-- The page you are on is marked with `aria-current` as well as with
           a border: a number distinguished only by its colour is not
           distinguished at all for some readers. -->
      <button
        v-else
        :key="`page-${number}`"
        type="button"
        class="rounded-lg border px-3 py-1.5 text-sm tabular-nums transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ borderColor: number === view.page ? 'var(--text)' : 'transparent' }"
        :aria-current="number === view.page ? 'page' : undefined"
        :aria-label="$t('ui.pagination.page', { number: String(number) })"
        @click="go(number)"
      >
        {{ number }}
      </button>
    </template>

    <button
      type="button"
      rel="next"
      class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-40"
      :style="{ color: 'var(--text)' }"
      :disabled="!view.hasNext"
      @click="go(stepPage(view.page, view.count, 1))"
    >
      {{ $t('ui.pagination.next') }}
    </button>

    <!-- A control with no address bar has to say where it is somewhere. -->
    <span class="ml-2 text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ say(view.position) }}
    </span>
  </nav>
</template>
