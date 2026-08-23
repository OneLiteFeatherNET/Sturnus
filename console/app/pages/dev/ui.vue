<script setup lang="ts">
/**
 * Every control in `components/ui`, in the states that decide whether it
 * works.
 *
 * A design system with no gallery is a design system nobody can review.
 * The alternative is reading seven components and imagining them, or
 * running the whole console and hunting for a page that happens to use
 * one — and a control's interesting states are precisely the ones no page
 * puts on screen on purpose: nothing chosen, everything disabled, a list
 * of two hundred, a value the caller has marked as wrong.
 *
 * **Nothing here is a consumer.** No existing page is migrated onto these
 * controls in this change; that is the next pull requests' job, and mixing
 * it in would make both unreviewable. This page is the only thing that
 * uses them, which also means it is the first thing that would break if
 * one of their contracts changed.
 *
 * **Development only.** The middleware below refuses the route outside
 * `nuxt dev`, so the page cannot be reached in a deployed console. It is
 * still compiled into the bundle — Nuxt has no per-page build exclusion
 * that does not involve a second build configuration — so the guard is
 * about reachability rather than about size.
 *
 * The dark theme is a state of this page rather than of a panel on it. The
 * role tokens are defined on `:root`, so a "dark preview" pane would have
 * to redefine them somewhere, and the only place this console keeps CSS is
 * `main.css`. The theme switcher at the top uses the console's own
 * preference, which is the honest way to see both.
 */
import UiChipInput from '~/components/ui/UiChipInput.vue'
import UiCombobox from '~/components/ui/UiCombobox.vue'
import UiDatePicker from '~/components/ui/UiDatePicker.vue'
import UiDisclosureList from '~/components/ui/UiDisclosureList.vue'
import UiPagination from '~/components/ui/UiPagination.vue'
import UiSelect from '~/components/ui/UiSelect.vue'
import UiTabs from '~/components/ui/UiTabs.vue'
import { EMPTY_CHIPS, type ChipValue } from '~/utils/uiChipInput'
import type { UiRow } from '~/utils/uiDisclosureList'
import type { UiOption } from '~/utils/uiOption'
import type { UiTab } from '~/utils/uiTabs'

definePageMeta({
  middleware: [
    () => {
      // Reachable in `nuxt dev` and nowhere else. A gallery of controls in
      // a production console is a page that answers questions nobody
      // outside the team asked.
      if (!import.meta.dev) {
        return abortNavigation(createError({ statusCode: 404, statusMessage: 'Not Found' }))
      }
    },
  ],
})

const { t } = useI18n()
const theme = useThemePreference()

useHead({ title: () => t('ui.gallery.title') })

const GUILDS: UiOption[] = [
  { value: '1', label: 'OneLiteFeather', detail: '100000000000000001' },
  { value: '2', label: 'Sturnus Testing', detail: '100000000000000002' },
  { value: '3', label: 'A server the bot has left', detail: '100000000000000003', disabled: true },
]

const NOTHING: UiOption[] = []

const MANY: UiOption[] = Array.from({ length: 200 }, (_, index) => ({
  value: String(index),
  label: `channel-${String(index).padStart(3, '0')}`,
  detail: `Text · ${900000000000000000 + index}`,
}))

const TAGS = ['standup', 'migration', 'incident', 'retro', 'hiring', 'planning', 'design']

const TABS: UiTab[] = [
  { id: 'overview', label: t('ui.gallery.tabOverview') },
  { id: 'queue', label: t('ui.gallery.tabQueue') },
  { id: 'consents', label: t('ui.gallery.tabConsents') },
  { id: 'locked', label: t('ui.gallery.tabLocked'), disabled: true },
]

const ROWS: UiRow[] = Array.from({ length: 5 }, (_, index) => ({
  id: `row-${index}`,
  selectable: index !== 3,
}))

const emptySelect = ref<string | null>(null)
const filledSelect = ref<string | null>('2')
const invalidSelect = ref<string | null>(null)
const noneSelect = ref<string | null>(null)
const channel = ref<string | null>(null)
const moment = ref<string | null>(null)
const boundedMoment = ref<string | null>(null)
const chips = ref<ChipValue>(EMPTY_CHIPS)
const filledChips = ref<ChipValue>({ chips: ['standup', 'migration'], text: 'about the ' })
const selected = ref<readonly string[]>(['row-0', 'not-on-this-page'])
const page = ref(3)
</script>

<template>
  <div class="mx-auto flex max-w-3xl flex-col gap-10 p-6">
    <header>
      <h1 class="text-xl font-semibold">{{ $t('ui.gallery.title') }}</h1>
      <p class="mt-2 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('ui.gallery.intro') }}
      </p>
      <div class="mt-3 flex flex-wrap items-center gap-2">
        <span class="text-xs font-medium tracking-wide uppercase" :style="{ color: 'var(--text-muted)' }">
          {{ $t('settings.appearance.title') }}
        </span>
        <button
          v-for="choice in theme.available"
          :key="choice"
          type="button"
          class="rounded-lg border px-3 py-1 text-sm transition-colors"
          :style="{
            borderColor: theme.current.value === choice ? 'var(--action)' : 'var(--control-border)',
            color: theme.current.value === choice ? 'var(--action)' : 'var(--text)',
          }"
          @click="theme.choose(choice)"
        >
          {{ $t(`settings.appearance.${choice}`) }}
        </button>
      </div>
    </header>

    <!-- UiSelect ------------------------------------------------------ -->
    <section class="flex flex-col gap-4">
      <h2 class="text-base font-semibold">{{ $t('ui.gallery.select') }}</h2>

      <div>
        <p class="mb-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.stateEmpty') }}
        </p>
        <UiSelect v-model="emptySelect" :options="GUILDS" :label="$t('ui.gallery.select')" />
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.emits') }}: {{ emptySelect ?? 'null' }}
        </p>
      </div>

      <div>
        <p class="mb-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.stateFilled') }}
        </p>
        <UiSelect v-model="filledSelect" :options="GUILDS" :label="$t('ui.gallery.select')" />
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.emits') }}: {{ filledSelect ?? 'null' }}
        </p>
      </div>

      <div>
        <p class="mb-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.stateDisabled') }}
        </p>
        <UiSelect
          :model-value="'1'"
          :options="GUILDS"
          disabled
          :label="$t('ui.gallery.select')"
        />
      </div>

      <div>
        <p class="mb-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.stateInvalid') }}
        </p>
        <UiSelect
          v-model="invalidSelect"
          :options="GUILDS"
          invalid
          :label="$t('ui.gallery.select')"
        />
      </div>

      <div>
        <p class="mb-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.stateNone') }}
        </p>
        <UiSelect v-model="noneSelect" :options="NOTHING" :label="$t('ui.gallery.select')" />
      </div>
    </section>

    <!-- UiCombobox ---------------------------------------------------- -->
    <section class="flex flex-col gap-4">
      <h2 class="text-base font-semibold">{{ $t('ui.gallery.combobox') }}</h2>
      <div>
        <p class="mb-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.stateLong') }}
        </p>
        <UiCombobox v-model="channel" :options="MANY" :label="$t('ui.gallery.combobox')" />
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.emits') }}: {{ channel ?? 'null' }}
        </p>
      </div>
      <div>
        <p class="mb-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.stateDisabled') }}
        </p>
        <UiCombobox :options="MANY" disabled :label="$t('ui.gallery.combobox')" />
      </div>
    </section>

    <!-- UiDatePicker -------------------------------------------------- -->
    <section class="flex flex-col gap-4">
      <h2 class="text-base font-semibold">{{ $t('ui.gallery.datePicker') }}</h2>
      <div>
        <p class="mb-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.stateEmpty') }}
        </p>
        <UiDatePicker v-model="moment" :label="$t('ui.gallery.datePicker')" />
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.emits') }}: {{ moment ?? 'null' }}
        </p>
      </div>
      <div>
        <p class="mb-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.stateFilled') }}
        </p>
        <UiDatePicker
          v-model="boundedMoment"
          min="2026-08-10"
          max="2026-09-15"
          :label="$t('ui.gallery.datePicker')"
        />
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.emits') }}: {{ boundedMoment ?? 'null' }}
        </p>
      </div>
      <div>
        <p class="mb-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.stateDisabled') }}
        </p>
        <UiDatePicker disabled :label="$t('ui.gallery.datePicker')" />
      </div>
    </section>

    <!-- UiChipInput --------------------------------------------------- -->
    <section class="flex flex-col gap-4">
      <h2 class="text-base font-semibold">{{ $t('ui.gallery.chipInput') }}</h2>
      <div>
        <p class="mb-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.stateEmpty') }}
        </p>
        <UiChipInput v-model="chips" :suggestions="TAGS" :label="$t('ui.gallery.chipInput')" />
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.emits') }}: {{ JSON.stringify(chips) }}
        </p>
      </div>
      <div>
        <p class="mb-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.stateFilled') }}
        </p>
        <UiChipInput
          v-model="filledChips"
          :suggestions="TAGS"
          :label="$t('ui.gallery.chipInput')"
        />
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.emits') }}: {{ JSON.stringify(filledChips) }}
        </p>
      </div>
      <div>
        <p class="mb-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('ui.gallery.stateInvalid') }}
        </p>
        <UiChipInput :suggestions="TAGS" invalid :label="$t('ui.gallery.chipInput')" />
      </div>
    </section>

    <!-- UiTabs -------------------------------------------------------- -->
    <section class="flex flex-col gap-2">
      <h2 class="text-base font-semibold">{{ $t('ui.gallery.tabs') }}</h2>
      <UiTabs :tabs="TABS" :label="$t('ui.gallery.tabs')">
        <template #overview>
          <p class="text-sm">{{ $t('ui.gallery.panelOverview') }}</p>
        </template>
        <template #queue>
          <p class="text-sm">{{ $t('ui.gallery.panelQueue') }}</p>
        </template>
        <template #consents>
          <p class="text-sm">{{ $t('ui.gallery.panelConsents') }}</p>
        </template>
      </UiTabs>
    </section>

    <!-- UiDisclosureList and UiPagination ----------------------------- -->
    <section class="flex flex-col gap-4">
      <h2 class="text-base font-semibold">{{ $t('ui.gallery.list') }}</h2>
      <UiDisclosureList
        v-model:selected="selected"
        :rows="ROWS"
        :bulk-action="$t('ui.gallery.erase')"
        :label="$t('ui.gallery.list')"
      >
        <template #row="{ index }">
          <span class="text-sm">{{ $t('ui.pagination.page', { number: String(index + 1) }) }}</span>
        </template>
        <template #actions>
          <p class="text-sm" :style="{ color: 'var(--text-muted)' }">
            {{ $t('ui.gallery.rowAction') }}
          </p>
        </template>
      </UiDisclosureList>

      <h2 class="text-base font-semibold">{{ $t('ui.gallery.pagination') }}</h2>
      <UiPagination v-model:page="page" :total="400" />
    </section>
  </div>
</template>
