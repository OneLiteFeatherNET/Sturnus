<script setup lang="ts">
/**
 * The left navigation, in either of its two widths.
 *
 * Collapsed it shows icons alone. That is a real mode rather than a
 * narrower version of the same thing: every entry keeps its accessible
 * name through `aria-label` and its `title`, so a screen reader and a
 * hover both still say what the icon means. An icon rail whose entries
 * announce nothing is a rail only its author can navigate.
 */
import { visibleEntries } from '~/utils/navigation'

const { collapsed } = useSidebar()
const session = useSession()

// The list and the filter both live in `~/utils/navigation`, where they
// can be tested without rendering anything -- and where the note that
// hiding is a courtesy rather than a control is written down.
const visible = computed(() => visibleEntries(session.value))
</script>

<template>
  <nav
    class="flex shrink-0 flex-col gap-1 border-r p-3 transition-[width] duration-200"
    :class="collapsed ? 'w-16' : 'w-16 sm:w-56'"
    :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    aria-label="Sections"
  >
    <NuxtLink
      v-for="entry in visible"
      :key="entry.to"
      :to="entry.to"
      :title="entry.label"
      :aria-label="entry.label"
      class="flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
      active-class="bg-[var(--surface-raised)] text-[var(--action)]"
    >
      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" class="shrink-0">
        <path :d="entry.icon" />
      </svg>
      <!-- Below `sm` the rail is icons only whatever the stored
           preference says: 224 px of navigation out of a 375 px screen
           leaves 103 px for the page, which is narrower than a scrubber.
           The entries keep their `aria-label` and `title`, so this is the
           same mode the burger already produces rather than a rail that
           announces nothing. -->
      <span v-if="!collapsed" class="hidden truncate sm:inline">{{ entry.label }}</span>
    </NuxtLink>
  </nav>
</template>
