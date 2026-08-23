<script setup lang="ts">
/**
 * The left navigation, in either of its two widths.
 *
 * Collapsed it shows icons alone. That is a real mode rather than a
 * narrower version of the same thing: every entry keeps its accessible
 * name through `aria-label` and its `title`, so a screen reader and a
 * hover both still say what the icon means. An icon rail whose entries
 * announce nothing is a rail only its author can navigate.
 *
 * The same rule governs the two groups. Expanded, "User View" and "Admin
 * View" are visible headings; collapsed, they are a rule between two runs
 * of icons -- but each group carries its name on the group element itself
 * in both modes, so the boundary is announced rather than merely drawn.
 * Somebody who cannot see the rule is exactly the person who most needs to
 * be told that the next icon acts on the whole guild.
 */
import { visibleSections } from '~/utils/navigation'

const { collapsed } = useSidebar()
const session = useSession()

// The list and the filter both live in `~/utils/navigation`, where they
// can be tested without rendering anything -- and where the note that
// hiding is a courtesy rather than a control is written down.
const visible = computed(() => visibleSections(session.value))
</script>

<template>
  <nav
    class="flex shrink-0 flex-col gap-1 border-r p-3 transition-[width] duration-200"
    :class="collapsed ? 'w-16' : 'w-16 sm:w-56'"
    :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    :aria-label="$t('nav.sections')"
  >
    <div
      v-for="(section, index) in visible"
      :key="section.labelKey"
      role="group"
      :aria-label="$t(section.labelKey)"
      class="flex flex-col gap-1"
    >
      <!-- A rule instead of a heading when collapsed. The heading text
           would not fit the rail, and shrinking it to an abbreviation
           would be a second label to keep in step with the first. -->
      <hr
        v-if="collapsed && index > 0"
        class="my-2"
        :style="{ borderColor: 'var(--border)' }"
      >
      <h2
        v-if="!collapsed"
        class="px-3 pt-3 pb-1 text-xs font-semibold uppercase tracking-wider"
        :style="{ color: 'var(--text-muted)' }"
      >
        {{ $t(section.labelKey) }}
      </h2>

      <NuxtLink
        v-for="entry in section.entries"
        :key="entry.to"
        :to="entry.to"
        :title="$t(entry.labelKey)"
        :aria-label="$t(entry.labelKey)"
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
        <span v-if="!collapsed" class="hidden truncate sm:inline">{{ $t(entry.labelKey) }}</span>
      </NuxtLink>
    </div>
  </nav>
</template>
