<script setup lang="ts">
/**
 * A tab bar whose tabs are places, and whose panels are not built until
 * somebody asks for them.
 *
 * **The selected tab is in the query string.** `paging.ts` already made
 * this argument for the recordings list: a state somebody navigated into
 * needs an address, or the back button leaves them somewhere they did not
 * put themselves and no link can say where they were. A tab is that with
 * fewer numbers. `~/utils/uiTabs` owns the parameter's name and the rule
 * that the first tab drops it, so the plain address of a page stays the
 * address of the page.
 *
 * `replace` rather than `push`: a tab is a view of the page somebody is
 * already on, and filling the history with four steps back through one
 * page makes the back button useless for leaving it.
 *
 * **A panel is built the first time it is shown and stays built.**
 * Rendering all four and hiding three is how a tab bar fires four
 * requests, and the expensive one is reliably the tab nobody clicks;
 * unmounting on every switch is the opposite mistake, which turns the bar
 * into a reload button and throws away whatever was half typed. Each tab
 * renders through a slot named after its id.
 */
import {
  TAB_QUERY,
  type UiTab,
  moveTab,
  panelDomId,
  panelsAfter,
  queryForTab,
  tabDomId,
  tabFromQuery,
} from '~/utils/uiTabs'

const props = withDefaults(
  defineProps<{
    tabs: readonly UiTab[]
    label?: string
  }>(),
  { label: undefined },
)

const emit = defineEmits<{ change: [string] }>()

const base = useId()
const route = useRoute()
const router = useRouter()

const current = computed(() => tabFromQuery(route.query[TAB_QUERY], props.tabs))
const mounted = ref<readonly string[]>(current.value ? [current.value] : [])

watch(
  current,
  (id) => {
    if (id) mounted.value = panelsAfter(mounted.value, id)
  },
  { immediate: true },
)

function select(id: string) {
  if (id === current.value) return
  void router.replace({ path: route.path, query: queryForTab(route.query, props.tabs, id) })
  emit('change', id)
}

function onKeydown(event: KeyboardEvent) {
  const moved = moveTab(props.tabs, current.value ?? '', event.key)
  if (moved === null) return
  event.preventDefault()
  select(moved)
  // Focus follows selection, because selection followed the key: leaving
  // focus on the tab that is no longer selected makes the next arrow press
  // start from somewhere the reader is not.
  void nextTick(() => {
    const at = props.tabs.findIndex((tab) => tab.id === moved)
    document.getElementById(tabDomId(base, at))?.focus()
  })
}
</script>

<template>
  <div>
    <div
      role="tablist"
      :aria-label="label"
      class="flex flex-wrap gap-1 border-b"
      :style="{ borderColor: 'var(--border)' }"
      @keydown="onKeydown"
    >
      <button
        v-for="(tab, index) in tabs"
        :id="tabDomId(base, index)"
        :key="tab.id"
        type="button"
        role="tab"
        :aria-selected="tab.id === current"
        :aria-controls="panelDomId(base, index)"
        :aria-disabled="tab.disabled ? 'true' : undefined"
        :disabled="tab.disabled"
        :tabindex="tab.id === current ? 0 : -1"
        class="-mb-px rounded-t-lg border-b-2 px-3 py-2 text-sm font-medium transition-colors disabled:opacity-40"
        :style="{
          borderColor: tab.id === current ? 'var(--action)' : 'transparent',
          color: tab.id === current ? 'var(--action)' : 'var(--text-muted)',
        }"
        @click="select(tab.id)"
      >
        {{ tab.label }}
      </button>
    </div>

    <template v-for="(tab, index) in tabs" :key="tab.id">
      <!-- `v-if` on ever having been shown, and `hidden` afterwards: a
           panel that has been opened keeps its scroll position, its
           half-filled form and the answer that had just arrived. -->
      <div
        v-if="mounted.includes(tab.id)"
        :id="panelDomId(base, index)"
        role="tabpanel"
        :aria-labelledby="tabDomId(base, index)"
        :hidden="tab.id !== current"
        tabindex="0"
        class="pt-4"
      >
        <slot :name="tab.id" :tab="tab" />
      </div>
    </template>
  </div>
</template>
