<script setup lang="ts">
/**
 * The top bar: the product, the control that collapses the sidebar, and who
 * is signed in.
 *
 * The burger is a `button` with `aria-expanded`, not a styled `div`. It
 * changes what is on screen, so it has to be reachable by keyboard and to
 * announce its state -- a toggle that looks like a toggle and announces
 * nothing is a control half the people using it cannot see.
 *
 * The right-hand slot held a lone Sign out button and no indication of whose
 * console this was. It now holds `ProfileMenu`, which keeps signing out
 * exactly as it was and adds the three things that were missing: a name, a
 * way to reach a person's own settings, and an honest statement about the
 * two authentication features that do not exist yet. The header does not own
 * any of that -- it owns where it sits.
 */
const { collapsed, toggle } = useSidebar()
</script>

<template>
  <header
    class="flex items-center gap-3 border-b px-4 py-3"
    :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
  >
    <button
      type="button"
      class="rounded-lg p-2 transition-colors hover:bg-[var(--surface-raised)]"
      :aria-expanded="!collapsed"
      aria-controls="sidebar"
      :aria-label="$t('nav.toggle')"
      @click="toggle"
    >
      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
        <path d="M3 6h18v2H3V6Zm0 5h18v2H3v-2Zm0 5h18v2H3v-2Z" />
      </svg>
    </button>

    <NuxtLink to="/" class="flex items-center gap-2">
      <SturnusMark :size="26" />
      <span class="text-base font-semibold tracking-tight">{{ $t('common.brand') }}</span>
    </NuxtLink>

    <div class="ml-auto flex items-center gap-3">
      <ProfileMenu />
    </div>
  </header>
</template>
